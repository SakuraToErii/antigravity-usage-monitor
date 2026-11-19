#!/usr/bin/env python3
"""Usage & official API-equivalent cost reporter for local-usage-monitor.

Window model aligned with Antigravity's real quota mechanics:
1. 5-Hour Window (Activity-Triggered Bucket):
   Antigravity's 5-hour countdown starts on your first request after the previous 5-hour
   bucket expires (e.g., 13:15 -> 18:15), NOT a continuous sliding window.
   - Automatically detected from local transcript timestamps (no manual calibration needed!).
   - If a fresh /usage 5h countdown is pasted and still active (end > now), uses that exact
     server end time; once it expires, automatically resumes activity-triggered bucket tracking.
2. Weekly (168-Hour) Window:
   - Uses calibrated weekly reset anchor (rolling forward by 168h cycles automatically) when set.
   - Supports one-click parsing of pasted `/usage` output (extracts both remaining % and
     `Refreshes in Xh Ym`, and can suggest/calibrate implied total pool USD budgets).
   - Falls back to rolling 7-day window if no weekly anchor has ever been set.

Supports plan presets (Pro / Ultra 5x / Ultra 20x / Custom) and Family Group size (1-6)
to display per-member fair-share utilization %.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pricing import DATA_DIR, OVERRIDES_PATH
from remote_sync import (
    add_ssh_host,
    get_remote_status,
    remove_ssh_host,
    sync_all_ssh_hosts,
)
from tracker import (
    CONFIG_PATH,
    DB_PATH,
    get_connection,
    load_config,
    reprice_db,
    save_config,
    scan_all_transcripts,
)

FIVE_HOURS = 5 * 3600
SEVEN_DAYS = 7 * 86400
LOCAL_TZ = datetime.now().astimezone().tzinfo

# Community reference estimates for total Antigravity pool API-equivalent USD budgets.
# Google does not publish fixed USD caps; users can calibrate from /usage or edit freely.
PLAN_PRESETS: Dict[str, Dict[str, Any]] = {
    "pro": {
        "label": "Pro",
        "budget_5h_usd": 15.0,
        "budget_weekly_usd": 70.0,
    },
    "ultra_5x": {
        "label": "Ultra 5x",
        "budget_5h_usd": 80.0,
        "budget_weekly_usd": 350.0,
    },
    "ultra_20x": {
        "label": "Ultra 20x",
        "budget_5h_usd": 180.0,
        "budget_weekly_usd": 750.0,
    },
    "custom": {
        "label": "自定义",
        "budget_5h_usd": 80.0,
        "budget_weekly_usd": 350.0,
    },
}


def get_effective_budget_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    plan = str(cfg.get("plan") or "ultra_5x")
    if plan not in PLAN_PRESETS:
        plan = "ultra_5x"
    preset = PLAN_PRESETS[plan]

    try:
        family_size = max(1, min(6, int(cfg.get("family_size") or 1)))
    except Exception:
        family_size = 1

    try:
        b5 = float(cfg.get("budget_5h_usd")) if cfg.get("budget_5h_usd") is not None else float(preset["budget_5h_usd"])
    except Exception:
        b5 = float(preset["budget_5h_usd"])

    try:
        b7 = float(cfg.get("budget_weekly_usd")) if cfg.get("budget_weekly_usd") is not None else float(preset["budget_weekly_usd"])
    except Exception:
        b7 = float(preset["budget_weekly_usd"])

    b5 = max(0.01, b5)
    b7 = max(0.01, b7)

    lang = str(cfg.get("lang") or "auto").lower()
    if lang not in ("auto", "zh", "en"):
        lang = "auto"

    return {
        "plan": plan,
        "plan_label": preset["label"],
        "family_size": family_size,
        "budget_5h_usd": b5,
        "budget_weekly_usd": b7,
        "per_member_5h_usd": b5 / family_size,
        "per_member_weekly_usd": b7 / family_size,
        "lang": lang,
    }


def attach_quota_share(window: Dict[str, Any], total_budget_usd: float, family_size: int) -> Dict[str, Any]:
    cost = float(window["totals"]["total_cost"])
    per_member_budget = total_budget_usd / max(1, family_size)
    personal_pct = (cost / per_member_budget * 100.0) if per_member_budget > 0 else 0.0
    total_pool_pct = (cost / total_budget_usd * 100.0) if total_budget_usd > 0 else 0.0
    window["quota"] = {
        "total_budget_usd": total_budget_usd,
        "family_size": family_size,
        "per_member_budget_usd": per_member_budget,
        "personal_share_pct": personal_pct,
        "total_pool_pct": total_pool_pct,
        "remaining_personal_usd": per_member_budget - cost,
    }
    return window


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_usd(v: float) -> str:
    if v >= 100:
        return f"${v:,.2f}"
    if v >= 1:
        return f"${v:.3f}"
    return f"${v:.4f}"


def fmt_duration(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return "".join(parts)


def parse_relative_duration(spec: str) -> int:
    matches = re.findall(r"(\d+)\s*([dhm])", spec.strip().lower())
    if not matches:
        raise ValueError(f"Invalid duration '{spec}'. Use format like '3h 30m' or '142h 25m'.")
    total = 0
    for val, unit in matches:
        n = int(val)
        if unit == "d":
            total += n * 86400
        elif unit == "h":
            total += n * 3600
        elif unit == "m":
            total += n * 60
    return total


def parse_local_datetime(spec: str) -> float:
    s = spec.strip()
    if s.endswith("Z"):
        dt = datetime.fromisoformat(s[:-1] + "+00:00")
        return dt.timestamp()
    dt = datetime.fromisoformat(s.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.timestamp()


def parse_usage_paste(text: str) -> Dict[str, Any]:
    """Parse pasted Antigravity `/usage` text block.

    Example input:
      Weekly Limit Remaining
        [████████...] 52.05%
        Refreshes in 142h 25m
      Five Hour Limit Remaining
        [████████...] 66.95%
        Refreshes in 3h 30m
    """
    result: Dict[str, Any] = {}

    def extract_section(pattern: str, prefix: str) -> None:
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not m:
            return
        chunk = m.group(1)
        pct_m = re.search(r"(\d+(?:\.\d+)?)\s*%", chunk)
        if pct_m:
            result[f"{prefix}_remaining_pct"] = float(pct_m.group(1))
        ref_m = re.search(r"Refreshes\s+in\s+([^\n\r]+)", chunk, re.IGNORECASE)
        if ref_m:
            try:
                result[f"{prefix}_refreshes_in_sec"] = parse_relative_duration(ref_m.group(1))
            except ValueError:
                pass

    extract_section(r"Weekly\s+Limit\s+Remaining(.*?)(?:Five\s+Hour|5\s*-?\s*Hour|$)", "weekly")
    extract_section(r"(?:Five\s+Hour|5\s*-?\s*Hour)\s+Limit\s+Remaining(.*?)(?:Weekly\s+Limit|$)", "five_hour")
    return result


def compute_cycle_window(anchor_epoch: float, period_sec: int, now_epoch: float) -> Tuple[float, float]:
    if anchor_epoch <= now_epoch:
        steps = math.floor((now_epoch - anchor_epoch) / period_sec) + 1
        end = anchor_epoch + steps * period_sec
    else:
        steps = math.floor((anchor_epoch - now_epoch) / period_sec)
        end = anchor_epoch - steps * period_sec
        if end <= now_epoch:
            end += period_sec
    start = end - period_sec
    return start, end


def resolve_active_5h_window(conn, cfg: Dict[str, Any], now_epoch: float) -> Tuple[float, float, str]:
    """Determine the current 5-hour quota bucket [start, end) and mode label.

    1. If a calibrated /usage 5h reset timestamp is still in the future (now < anchor <= now + 5h),
       use that exact server-reported window [anchor - 5h, anchor).
    2. Otherwise, simulate Antigravity's activity-triggered 5-hour buckets forward from local
       usage_steps (starting after any expired anchor, or over the last 72h).
       - If the latest bucket is still active (now < bucket_end), return [bucket_start, bucket_end).
       - If idle (no request in the past 5h), return [now, now + 5h) with mode='auto_idle'.
    """
    anchor = cfg.get("reset_5h_anchor_epoch")
    if anchor is not None:
        anchor_f = float(anchor)
        if now_epoch < anchor_f <= now_epoch + FIVE_HOURS + 600:
            return anchor_f - FIVE_HOURS, anchor_f, "calibrated"

    # Forward-simulate activity-triggered 5h buckets
    scan_from = float(anchor) if (anchor is not None and float(anchor) <= now_epoch) else (now_epoch - 72 * 3600)
    rows = conn.execute(
        "SELECT ts_epoch FROM usage_steps WHERE ts_epoch >= ? AND ts_epoch <= ? ORDER BY ts_epoch ASC",
        (scan_from, now_epoch),
    ).fetchall()

    b_start = None
    b_end = None
    for (ts,) in rows:
        if b_end is None or ts >= b_end:
            b_start = ts
            b_end = ts + FIVE_HOURS

    if b_start is not None and b_end is not None and now_epoch < b_end:
        return b_start, b_end, "auto_bucket"

    return now_epoch, now_epoch + FIVE_HOURS, "auto_idle"


def resolve_active_weekly_window(cfg: Dict[str, Any], now_epoch: float) -> Tuple[float, float, str]:
    """Determine the current weekly (168h) quota window [start, end) and mode label."""
    anchor = cfg.get("reset_weekly_anchor_epoch")
    if anchor is not None:
        s, e = compute_cycle_window(float(anchor), SEVEN_DAYS, now_epoch)
        return s, e, "calibrated"
    return now_epoch - SEVEN_DAYS, now_epoch, "rolling_7d"


def query_window(
    conn,
    start_epoch: float,
    query_end_epoch: float,
    cycle_end_epoch: float,
    mode: str,
    window_sec: int,
) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            surface, host, canonical_model, display_model, pool,
            ts_epoch, input_tokens, cache_read_tokens, output_tokens,
            input_cost, cache_read_cost, output_cost, total_cost, cache_savings, priced
        FROM usage_steps
        WHERE ts_epoch >= ? AND ts_epoch <= ?
        ORDER BY ts_epoch ASC
        """,
        (start_epoch, query_end_epoch),
    ).fetchall()

    totals = {
        "calls": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "output_tokens": 0,
        "input_cost": 0.0,
        "cache_read_cost": 0.0,
        "output_cost": 0.0,
        "total_cost": 0.0,
        "cache_savings": 0.0,
        "unpriced_calls": 0,
    }
    by_pool: Dict[str, Dict[str, Any]] = {}
    by_model: Dict[str, Dict[str, Any]] = {}
    by_surface: Dict[str, Dict[str, Any]] = {}
    by_host: Dict[str, Dict[str, Any]] = {}

    earliest_ts: Optional[float] = None
    for r in rows:
        (
            surface,
            host,
            canonical_model,
            display_model,
            pool,
            ts_epoch,
            in_tok,
            cache_tok,
            out_tok,
            in_cost,
            cache_cost,
            out_cost,
            tot_cost,
            savings,
            priced,
        ) = r
        host_key = host or "local"

        if earliest_ts is None:
            earliest_ts = ts_epoch

        for bucket in (
            totals,
            by_pool.setdefault(
                pool,
                {
                    "pool": pool,
                    "calls": 0,
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "input_cost": 0.0,
                    "cache_read_cost": 0.0,
                    "output_cost": 0.0,
                    "total_cost": 0.0,
                    "cache_savings": 0.0,
                    "unpriced_calls": 0,
                },
            ),
            by_model.setdefault(
                canonical_model,
                {
                    "canonical_model": canonical_model,
                    "display_model": display_model,
                    "pool": pool,
                    "calls": 0,
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "input_cost": 0.0,
                    "cache_read_cost": 0.0,
                    "output_cost": 0.0,
                    "total_cost": 0.0,
                    "cache_savings": 0.0,
                    "unpriced_calls": 0,
                },
            ),
            by_surface.setdefault(
                surface,
                {
                    "surface": surface,
                    "calls": 0,
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "total_cost": 0.0,
                    "cache_savings": 0.0,
                },
            ),
            by_host.setdefault(
                host_key,
                {
                    "host": host_key,
                    "label": "本机" if host_key == "local" else host_key,
                    "calls": 0,
                    "input_tokens": 0,
                    "cache_read_tokens": 0,
                    "output_tokens": 0,
                    "total_cost": 0.0,
                    "cache_savings": 0.0,
                },
            ),
        ):
            bucket["calls"] += 1
            bucket["input_tokens"] += in_tok
            bucket["cache_read_tokens"] += cache_tok
            bucket["output_tokens"] += out_tok
            bucket["total_cost"] += tot_cost
            bucket["cache_savings"] += savings
            if "input_cost" in bucket:
                bucket["input_cost"] += in_cost
                bucket["cache_read_cost"] += cache_cost
                bucket["output_cost"] += out_cost
                if not priced:
                    bucket["unpriced_calls"] += 1

    def add_hit_rate(b: Dict[str, Any]) -> Dict[str, Any]:
        denom = b["input_tokens"] + b["cache_read_tokens"]
        b["cache_hit_rate"] = (b["cache_read_tokens"] / denom) if denom > 0 else 0.0
        return b

    add_hit_rate(totals)
    pools_list = [add_hit_rate(v) for v in sorted(by_pool.values(), key=lambda x: -x["total_cost"])]
    models_list = [add_hit_rate(v) for v in sorted(by_model.values(), key=lambda x: -x["total_cost"])]
    surfaces_list = [add_hit_rate(v) for v in sorted(by_surface.values(), key=lambda x: -x["total_cost"])]
    hosts_list = [add_hit_rate(v) for v in sorted(by_host.values(), key=lambda x: -x["total_cost"])]

    is_fixed_bucket = mode in ("calibrated", "auto_bucket")
    resets_in_sec = max(0.0, cycle_end_epoch - query_end_epoch) if is_fixed_bucket else None
    next_rolloff_sec = None
    next_rolloff_at = None
    if mode == "rolling_7d" and earliest_ts is not None:
        rolloff_epoch = earliest_ts + window_sec
        next_rolloff_sec = max(0.0, rolloff_epoch - query_end_epoch)
        next_rolloff_at = datetime.fromtimestamp(rolloff_epoch, tz=LOCAL_TZ).strftime("%m-%d %H:%M")

    return {
        "mode": mode,
        "start_epoch": start_epoch,
        "end_epoch": query_end_epoch,
        "cycle_end_epoch": cycle_end_epoch,
        "start_local": datetime.fromtimestamp(start_epoch, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
        "end_local": datetime.fromtimestamp(query_end_epoch, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
        "cycle_end_local": datetime.fromtimestamp(cycle_end_epoch, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        if is_fixed_bucket
        else None,
        "resets_in_sec": resets_in_sec,
        "is_rolling": mode == "rolling_7d",
        "next_rolloff_sec": next_rolloff_sec,
        "next_rolloff_at": next_rolloff_at,
        "totals": totals,
        "by_pool": pools_list,
        "by_model": models_list,
        "by_surface": surfaces_list,
        "by_host": hosts_list,
    }


def apply_usage_calibration(
    usage_text: str,
    calibrate_budgets: bool = False,
    now_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    """Parse pasted /usage text, update window anchors, and optionally calibrate USD budgets."""
    parsed = parse_usage_paste(usage_text)
    if not parsed:
        raise ValueError("未能从文本中识别 Weekly / Five Hour Limit Remaining 信息，请完整粘贴 /usage 输出。")

    now = now_epoch if now_epoch is not None else time.time()
    cfg = load_config()

    if "five_hour_refreshes_in_sec" in parsed:
        cfg["reset_5h_anchor_epoch"] = now + parsed["five_hour_refreshes_in_sec"]
    if "weekly_refreshes_in_sec" in parsed:
        cfg["reset_weekly_anchor_epoch"] = now + parsed["weekly_refreshes_in_sec"]

    # Record last observed official percentages for transparency
    snapshot: Dict[str, Any] = {"captured_at": now}
    if "five_hour_remaining_pct" in parsed:
        snapshot["five_hour_remaining_pct"] = parsed["five_hour_remaining_pct"]
        snapshot["five_hour_used_pct"] = round(max(0.0, 100.0 - parsed["five_hour_remaining_pct"]), 2)
    if "weekly_remaining_pct" in parsed:
        snapshot["weekly_remaining_pct"] = parsed["weekly_remaining_pct"]
        snapshot["weekly_used_pct"] = round(max(0.0, 100.0 - parsed["weekly_remaining_pct"]), 2)
    cfg["last_official_snapshot"] = snapshot

    save_config(cfg)

    # Compute local costs inside the newly aligned windows to estimate implied total pool USD
    scan_all_transcripts(force=False)
    conn = get_connection()
    implied: Dict[str, Optional[float]] = {"implied_5h_usd": None, "implied_weekly_usd": None}
    try:
        s5, e5, m5 = resolve_active_5h_window(conn, cfg, now)
        w5 = query_window(conn, s5, now, e5, m5, FIVE_HOURS)
        s7, e7, m7 = resolve_active_weekly_window(cfg, now)
        w7 = query_window(conn, s7, now, e7, m7, SEVEN_DAYS)

        used5 = snapshot.get("five_hour_used_pct")
        if used5 and used5 >= 1.0 and w5["totals"]["total_cost"] > 0:
            implied["implied_5h_usd"] = round(w5["totals"]["total_cost"] / (used5 / 100.0), 2)
        used7 = snapshot.get("weekly_used_pct")
        if used7 and used7 >= 1.0 and w7["totals"]["total_cost"] > 0:
            implied["implied_weekly_usd"] = round(w7["totals"]["total_cost"] / (used7 / 100.0), 2)
    finally:
        conn.close()

    if calibrate_budgets:
        if implied["implied_5h_usd"]:
            cfg["budget_5h_usd"] = implied["implied_5h_usd"]
            cfg["plan"] = "custom"
        if implied["implied_weekly_usd"]:
            cfg["budget_weekly_usd"] = implied["implied_weekly_usd"]
            cfg["plan"] = "custom"
        save_config(cfg)

    return {"parsed": parsed, "snapshot": snapshot, "implied": implied}


def build_report_data(
    scan: bool = True,
    force_reprice: bool = False,
    sync_remote: bool = False,
    syncing_hosts: Optional[set] = None,
) -> Dict[str, Any]:
    scan_info = None
    remote_sync_results = None
    if scan:
        fcount, scount = scan_all_transcripts(force=force_reprice)
        scan_info = {"files_scanned": fcount, "steps_synced": scount}
    if sync_remote:
        remote_sync_results = sync_all_ssh_hosts(force=force_reprice)
    if force_reprice:
        reprice_db()

    now_epoch = time.time()
    cfg = load_config()
    settings = get_effective_budget_settings(cfg)
    conn = get_connection()
    try:
        s5, e5, m5 = resolve_active_5h_window(conn, cfg, now_epoch)
        w5 = attach_quota_share(
            query_window(conn, s5, now_epoch, e5, m5, FIVE_HOURS),
            settings["budget_5h_usd"],
            settings["family_size"],
        )

        s7, e7, m7 = resolve_active_weekly_window(cfg, now_epoch)
        w7 = attach_quota_share(
            query_window(conn, s7, now_epoch, e7, m7, SEVEN_DAYS),
            settings["budget_weekly_usd"],
            settings["family_size"],
        )

        all_time = query_window(conn, 0.0, now_epoch, now_epoch, "all_time", 0)
    finally:
        conn.close()

    remote_status = get_remote_status(cfg, syncing_hosts=syncing_hosts)
    if remote_sync_results is not None:
        remote_status["last_sync_results"] = remote_sync_results

    return {
        "generated_at": datetime.fromtimestamp(now_epoch, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "db_path": str(DB_PATH),
        "scan": scan_info,
        "settings": settings,
        "presets": PLAN_PRESETS,
        "remote": remote_status,
        "anchors": {
            "reset_5h_anchor_epoch": cfg.get("reset_5h_anchor_epoch"),
            "reset_weekly_anchor_epoch": cfg.get("reset_weekly_anchor_epoch"),
        },
        "last_official_snapshot": cfg.get("last_official_snapshot"),
        "window_5h": w5,
        "window_weekly": w7,
        # Backwards-compatible aliases
        "rolling_5h": w5,
        "rolling_7d": w7,
        "all_time": all_time,
    }


def _pool_label(pool: str) -> str:
    return {"gemini": "Gemini 池", "claude_gpt": "Claude/GPT 池", "other": "其他"}.get(pool, pool)


def _surface_label(surface: str) -> str:
    return {"app": "Antigravity App", "cli": "agy CLI", "ide": "Antigravity IDE", "other": "其他"}.get(
        surface, surface
    )


def _mode_label(mode: str) -> str:
    return {
        "calibrated": "已按 /usage 同步官方周期",
        "auto_bucket": "自动识别首笔请求 5h 周期桶",
        "auto_idle": "当前空闲（下次请求起计 5h）",
        "rolling_7d": "近 7 天滑动窗口（粘贴 /usage 可对齐每周周期）",
        "all_time": "本机全部历史",
    }.get(mode, mode)


def print_window_block(title: str, w: Dict[str, Any]) -> None:
    t = w["totals"]
    q = w.get("quota")
    print(f"\n==================== {title} ====================")
    if w.get("cycle_end_local"):
        print(
            f"窗口区间: {w['start_local']} ~ {w['cycle_end_local']} "
            f"(距刷新 {fmt_duration(w['resets_in_sec'])} | {_mode_label(w['mode'])})"
        )
    else:
        print(f"窗口区间: {w['start_local']} ~ {w['end_local']} ({_mode_label(w['mode'])})")

    if q:
        print(
            f"个人份额占比: {q['personal_share_pct']:.1f}%  "
            f"(本机 {fmt_usd(t['total_cost'])} / 人均 {fmt_usd(q['per_member_budget_usd'])} "
            f"| 总池 {fmt_usd(q['total_budget_usd'])} ÷ {q['family_size']}人, 占全家总池 {q['total_pool_pct']:.1f}%)"
        )

    hit_pct = t["cache_hit_rate"] * 100.0
    print(
        f"总计等效 API 费用: {fmt_usd(t['total_cost'])}  |  缓存节省: {fmt_usd(t['cache_savings'])}  |  "
        f"请求轮次: {t['calls']}"
    )
    print(
        f"Token 汇总: 未缓存输入 {fmt_tokens(t['input_tokens'])}  |  缓存命中 {fmt_tokens(t['cache_read_tokens'])} "
        f"(命中率 {hit_pct:.1f}%)  |  输出 {fmt_tokens(t['output_tokens'])}"
    )

    if w["by_pool"]:
        pool_parts = [
            f"{_pool_label(p['pool'])}: {fmt_usd(p['total_cost'])} ({p['calls']}次, 命中{p['cache_hit_rate']*100:.0f}%)"
            for p in w["by_pool"]
        ]
        print("按官方额度池: " + "  /  ".join(pool_parts))

    if w["by_surface"]:
        surf_parts = [
            f"{_surface_label(s['surface'])}: {fmt_usd(s['total_cost'])} ({s['calls']}次)"
            for s in w["by_surface"]
        ]
        print("按客户端来源: " + "  /  ".join(surf_parts))

    hosts = w.get("by_host") or []
    if len(hosts) > 1 or any(h["host"] != "local" for h in hosts):
        host_parts = [
            f"{h['label']}: {fmt_usd(h['total_cost'])} ({h['calls']}次)"
            for h in hosts
        ]
        print("按主机来源: " + "  /  ".join(host_parts))

    if not w["by_model"]:
        print("  (当前窗口内暂无本机记录)")
        return

    print("")
    header = (
        f"{'模型':<24} {'额度池':<10} {'轮次':>5} {'未缓存In':>9} {'缓存读':>9} "
        f"{'命中率':>7} {'输出Out':>9} {'缓存省':>9} {'等效费用':>10}"
    )
    print(header)
    print("-" * len(header))
    for m in w["by_model"]:
        name = m["display_model"]
        if m["unpriced_calls"] > 0:
            name += "*"
        print(
            f"{name:<24} {_pool_label(m['pool']):<10} {m['calls']:>5} "
            f"{fmt_tokens(m['input_tokens']):>9} {fmt_tokens(m['cache_read_tokens']):>9} "
            f"{m['cache_hit_rate']*100:>6.1f}% {fmt_tokens(m['output_tokens']):>9} "
            f"{fmt_usd(m['cache_savings']):>9} {fmt_usd(m['total_cost']):>10}"
        )


def render_html(data: Dict[str, Any]) -> str:
    s = data["settings"]

    def render_section(sec_id: str, title: str, w: Dict[str, Any]) -> str:
        t = w["totals"]
        q = w.get("quota")
        if w.get("cycle_end_local"):
            subtitle = (
                f"{w['start_local']} ~ {w['cycle_end_local']} · "
                f"距刷新 {fmt_duration(w['resets_in_sec'])} · {_mode_label(w['mode'])}"
            )
        else:
            subtitle = f"{w['start_local']} ~ {w['end_local']} · {_mode_label(w['mode'])}"

        quota_bar_html = ""
        if q:
            pct = q["personal_share_pct"]
            clamped = min(100.0, max(0.0, pct))
            color = "#f85149" if pct >= 95 else ("#d29922" if pct >= 70 else "#3fb950")
            quota_bar_html = f"""
            <div class="quota-box">
              <div class="quota-top">
                <span>个人份额占比（总池 {fmt_usd(q['total_budget_usd'])} ÷ {q['family_size']}人 = 人均 {fmt_usd(q['per_member_budget_usd'])}）</span>
                <b style="color:{color}">{pct:.1f}%</b>
              </div>
              <div class="qbar-bg"><div class="qbar-fill" style="width:{clamped:.1f}%;background:{color}"></div></div>
              <div class="quota-sub">本机已用 {fmt_usd(t['total_cost'])} · 占全家总池 {q['total_pool_pct']:.1f}%</div>
            </div>
            """

        pool_badges = "".join(
            f"<span class='pill'>{html.escape(_pool_label(p['pool']))}: <b>{fmt_usd(p['total_cost'])}</b> ({p['calls']}次)</span>"
            for p in w["by_pool"]
        )
        surf_badges = "".join(
            f"<span class='pill muted'>{html.escape(_surface_label(sf['surface']))}: {fmt_usd(sf['total_cost'])}</span>"
            for sf in w["by_surface"]
        )
        hosts = w.get("by_host") or []
        host_badges = ""
        if len(hosts) > 1 or any(h["host"] != "local" for h in hosts):
            host_badges = "".join(
                f"<span class='pill muted'>{html.escape(h['label'])}: {fmt_usd(h['total_cost'])}</span>"
                for h in hosts
            )

        rows_html = []
        max_cost = max((m["total_cost"] for m in w["by_model"]), default=1.0) or 1.0
        for m in w["by_model"]:
            bar_pct = min(100.0, (m["total_cost"] / max_cost) * 100.0)
            rows_html.append(
                f"""
                <tr>
                  <td><b>{html.escape(m['display_model'])}</b><div class="bar-bg"><div class="bar-fill" style="width:{bar_pct:.1f}%"></div></div></td>
                  <td>{html.escape(_pool_label(m['pool']))}</td>
                  <td class="num">{m['calls']}</td>
                  <td class="num">{fmt_tokens(m['input_tokens'])}</td>
                  <td class="num">{fmt_tokens(m['cache_read_tokens'])}</td>
                  <td class="num">{m['cache_hit_rate']*100:.1f}%</td>
                  <td class="num">{fmt_tokens(m['output_tokens'])}</td>
                  <td class="num save">{fmt_usd(m['cache_savings'])}</td>
                  <td class="num cost">{fmt_usd(m['total_cost'])}</td>
                </tr>
                """
            )
        table_body = "".join(rows_html) or "<tr><td colspan='9' class='empty'>当前窗口暂无本机用量记录</td></tr>"

        return f"""
        <section class="card" id="{sec_id}">
          <div class="sec-head">
            <div>
              <h2>{html.escape(title)}</h2>
              <div class="sub">{html.escape(subtitle)}</div>
            </div>
            <div class="kpi-row">
              <div class="kpi"><div class="kpi-label">等效 API 费用</div><div class="kpi-val">{fmt_usd(t['total_cost'])}</div></div>
              <div class="kpi"><div class="kpi-label">缓存命中率</div><div class="kpi-val">{t['cache_hit_rate']*100:.1f}%</div></div>
              <div class="kpi"><div class="kpi-label">缓存节省</div><div class="kpi-val save">{fmt_usd(t['cache_savings'])}</div></div>
              <div class="kpi"><div class="kpi-label">请求轮次</div><div class="kpi-val">{t['calls']}</div></div>
            </div>
          </div>
          {quota_bar_html}
          <div class="pills">{pool_badges}{surf_badges}{host_badges}</div>
          <table>
            <thead>
              <tr>
                <th>模型</th><th>额度池</th><th class="num">轮次</th>
                <th class="num">未缓存输入</th><th class="num">缓存读</th><th class="num">命中率</th>
                <th class="num">输出</th><th class="num">缓存省</th><th class="num">等效费用</th>
              </tr>
            </thead>
            <tbody>{table_body}</tbody>
          </table>
        </section>
        """

    sections = [
        render_section("w5h", "当前 5 小时窗口", data["window_5h"]),
        render_section("w7d", "当前每周 (168h) 窗口", data["window_weekly"]),
        render_section("all", "本机历史总计 (All Time)", data["all_time"]),
    ]

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Antigravity Local Usage Monitor</title>
<style>
  :root {{ --bg:#0d1117; --card:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --green:#3fb950; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:24px; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:var(--bg); color:var(--text); }}
  .wrap {{ max-width:1100px; margin:0 auto; }}
  header {{ display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:18px; border-bottom:1px solid var(--border); padding-bottom:12px; }}
  h1 {{ margin:0; font-size:20px; }}
  .meta {{ color:var(--muted); font-size:12px; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; margin-bottom:16px; }}
  .sec-head {{ display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:12px; }}
  h2 {{ margin:0 0 4px 0; font-size:16px; }}
  .sub {{ color:var(--muted); font-size:12px; }}
  .kpi-row {{ display:flex; gap:10px; flex-wrap:wrap; }}
  .kpi {{ background:#0d1117; border:1px solid var(--border); border-radius:8px; padding:7px 12px; min-width:105px; }}
  .kpi-label {{ color:var(--muted); font-size:11px; margin-bottom:2px; }}
  .kpi-val {{ font-size:17px; font-weight:700; color:var(--accent); }}
  .kpi-val.save, td.save {{ color:var(--green); }}
  .quota-box {{ background:#0d1117; border:1px solid var(--border); border-radius:8px; padding:10px 12px; margin-bottom:12px; }}
  .quota-top {{ display:flex; justify-content:space-between; font-size:13px; margin-bottom:6px; }}
  .qbar-bg {{ height:8px; background:#21262d; border-radius:4px; overflow:hidden; margin-bottom:5px; }}
  .qbar-fill {{ height:100%; border-radius:4px; }}
  .quota-sub {{ font-size:11px; color:var(--muted); }}
  .pills {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }}
  .pill {{ background:#1f2937; border:1px solid var(--border); border-radius:999px; padding:3px 9px; font-size:12px; }}
  .pill.muted {{ color:var(--muted); background:#11161d; }}
  table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
  th, td {{ padding:8px 6px; border-bottom:1px solid var(--border); text-align:left; }}
  th {{ color:var(--muted); font-weight:600; font-size:11.5px; }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  td.cost {{ font-weight:700; color:var(--accent); }}
  .empty {{ text-align:center; color:var(--muted); padding:18px; }}
  .bar-bg {{ height:4px; background:#21262d; border-radius:2px; margin-top:4px; overflow:hidden; }}
  .bar-fill {{ height:100%; background:linear-gradient(90deg,#58a6ff,#3fb950); }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Antigravity 本地个人用量监控</h1>
      <div class="meta">当前配置: {html.escape(s['plan_label'])} · 家庭组 {s['family_size']} 人 · 5h 总池 {fmt_usd(s['budget_5h_usd'])} / 周总池 {fmt_usd(s['budget_weekly_usd'])}</div>
    </div>
    <div class="meta">生成时间: {html.escape(data['generated_at'])}</div>
  </header>
  {''.join(sections)}
</div>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity local usage & cost report (5h & weekly windows)")
    parser.add_argument("--no-scan", action="store_true", help="Read SQLite only without scanning transcript files")
    parser.add_argument("--reprice", action="store_true", help="Force re-parse and re-price all local transcripts")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text tables")
    parser.add_argument("--html", nargs="?", const=str(DATA_DIR / "dashboard.html"), help="Write HTML dashboard")
    parser.add_argument("--set-plan", choices=list(PLAN_PRESETS.keys()), help="Set plan preset")
    parser.add_argument("--set-family-size", type=int, help="Set family group size (1-6)")
    parser.add_argument("--set-5h-budget", type=float, help="Set total 5-hour pool USD budget")
    parser.add_argument("--set-weekly-budget", type=float, help="Set total weekly pool USD budget")
    parser.add_argument("--set-5h-reset", type=str, help="Set 5h reset anchor time")
    parser.add_argument("--set-5h-in", type=str, help="Set 5h reset anchor relative to now (e.g. '3h30m')")
    parser.add_argument("--set-weekly-reset", type=str, help="Set weekly reset anchor time")
    parser.add_argument("--set-weekly-in", type=str, help="Set weekly reset anchor relative to now (e.g. '142h25m')")
    parser.add_argument("--paste-usage", type=str, help="Paste raw /usage output text to calibrate windows")
    parser.add_argument("--calibrate-budgets", action="store_true", help="With --paste-usage, also update USD budgets from implied spend/%")
    parser.add_argument("--clear-anchors", action="store_true", help="Clear calibrated reset anchors")
    parser.add_argument("--sync-remote", action="store_true", help="Sync enabled SSH remote hosts before generating report")
    parser.add_argument("--add-ssh-host", action="append", help="Add and enable an SSH host (repeatable)")
    parser.add_argument("--remove-ssh-host", action="append", help="Remove an SSH host (repeatable)")
    parser.add_argument("--list-ssh-hosts", action="store_true", help="List configured and discovered SSH hosts")
    args = parser.parse_args()

    if args.add_ssh_host:
        for h in args.add_ssh_host:
            add_ssh_host(h, enabled=True)
    if args.remove_ssh_host:
        for h in args.remove_ssh_host:
            remove_ssh_host(h)
    if args.list_ssh_hosts:
        rem = get_remote_status()
        if args.json:
            print(json.dumps(rem, ensure_ascii=False, indent=2))
            return
        print("已配置 SSH 远程主机:")
        if not rem["hosts"]:
            print("  (无)")
        for item in rem["hosts"]:
            flag = "启用" if item["enabled"] else "停用"
            err = f" · 错误: {item['error']}" if item.get("error") else ""
            print(f"  - {item['host']} [{flag}] 状态={item['status']} 文件={item['files_found']}{err}")
        print("~/.ssh/config 发现的主机:", ", ".join(rem["discovered_ssh_hosts"]) or "(无)")
        return

    if args.paste_usage:
        res = apply_usage_calibration(args.paste_usage, calibrate_budgets=args.calibrate_budgets)
        if not args.json:
            imp = res["implied"]
            print(
                f"已解析 /usage 校准窗口 | 反推参考总池: 5h ≈ {fmt_usd(imp['implied_5h_usd']) if imp['implied_5h_usd'] else 'N/A'}, "
                f"周 ≈ {fmt_usd(imp['implied_weekly_usd']) if imp['implied_weekly_usd'] else 'N/A'}"
            )

    cfg = load_config()
    cfg_changed = False
    now = time.time()

    if args.set_plan:
        cfg["plan"] = args.set_plan
        if args.set_plan != "custom":
            if args.set_5h_budget is None:
                cfg["budget_5h_usd"] = PLAN_PRESETS[args.set_plan]["budget_5h_usd"]
            if args.set_weekly_budget is None:
                cfg["budget_weekly_usd"] = PLAN_PRESETS[args.set_plan]["budget_weekly_usd"]
        cfg_changed = True
    if args.set_family_size is not None:
        cfg["family_size"] = max(1, min(6, args.set_family_size))
        cfg_changed = True
    if args.set_5h_budget is not None:
        cfg["budget_5h_usd"] = max(0.01, args.set_5h_budget)
        cfg_changed = True
    if args.set_weekly_budget is not None:
        cfg["budget_weekly_usd"] = max(0.01, args.set_weekly_budget)
        cfg_changed = True

    if args.clear_anchors:
        cfg.pop("reset_5h_anchor_epoch", None)
        cfg.pop("reset_weekly_anchor_epoch", None)
        cfg.pop("last_official_snapshot", None)
        cfg_changed = True
    if args.set_5h_reset:
        cfg["reset_5h_anchor_epoch"] = parse_local_datetime(args.set_5h_reset)
        cfg_changed = True
    if args.set_5h_in:
        cfg["reset_5h_anchor_epoch"] = now + parse_relative_duration(args.set_5h_in)
        cfg_changed = True
    if args.set_weekly_reset:
        cfg["reset_weekly_anchor_epoch"] = parse_local_datetime(args.set_weekly_reset)
        cfg_changed = True
    if args.set_weekly_in:
        cfg["reset_weekly_anchor_epoch"] = now + parse_relative_duration(args.set_weekly_in)
        cfg_changed = True

    if cfg_changed:
        save_config(cfg)

    data = build_report_data(
        scan=not args.no_scan,
        force_reprice=args.reprice,
        sync_remote=args.sync_remote,
    )

    if args.html:
        out_path = Path(args.html).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_html(data), encoding="utf-8")
        if not args.json:
            print(f"HTML 仪表盘已生成: {out_path}")

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    st = data["settings"]
    print(f"Antigravity 本地用量统计 ({data['generated_at']})")
    print(
        f"套餐配置: {st['plan_label']} | 家庭组 {st['family_size']} 人 | "
        f"5h 人均 {fmt_usd(st['per_member_5h_usd'])} (总 {fmt_usd(st['budget_5h_usd'])}) | "
        f"周人均 {fmt_usd(st['per_member_weekly_usd'])} (总 {fmt_usd(st['budget_weekly_usd'])})"
    )
    print(f"数据库: {data['db_path']}  |  自定义单价覆盖: {OVERRIDES_PATH}")
    print_window_block("当前 5 小时窗口", data["window_5h"])
    print_window_block("当前每周 (168h) 窗口", data["window_weekly"])


if __name__ == "__main__":
    main()

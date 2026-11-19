#!/usr/bin/env python3
"""Antigravity CLI (`agy`) /statusline script for local-usage-monitor.

Enable inside `agy` CLI with:
  /statusline ~/.gemini/config/plugins/local-usage-monitor/scripts/statusline.py

Uses the exact same activity-triggered 5h bucket & calibrated weekly cycle logic
as report.py and the App Sidecar UI panel.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from report import (
    fmt_usd,
    get_effective_budget_settings,
    resolve_active_5h_window,
    resolve_active_weekly_window,
)
from tracker import DB_PATH, load_config


def query_fast_totals(conn: sqlite3.Connection, start_epoch: float, end_epoch: float):
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(total_cost), 0.0),
            COALESCE(SUM(input_tokens), 0),
            COALESCE(SUM(cache_read_tokens), 0)
        FROM usage_steps
        WHERE ts_epoch >= ? AND ts_epoch <= ?
        """,
        (start_epoch, end_epoch),
    ).fetchone()
    return float(row[0]), int(row[1]), int(row[2])


def _resolve_lang(cfg: dict) -> str:
    lang = str(cfg.get("lang") or "auto").lower()
    if lang in ("zh", "en"):
        return lang
    import os
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = (os.environ.get(key) or "").lower()
        if val.startswith("zh"):
            return "zh"
        if val:
            return "en"
    return "en"


def main() -> None:
    plain = "--plain" in sys.argv
    payload = {}
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            if raw.strip():
                payload = json.loads(raw)
    except Exception:
        payload = {}

    model_info = payload.get("model") or {}
    model_name = model_info.get("display_name") or model_info.get("id") or ""
    ctx_info = payload.get("context_window") or {}
    used_pct = ctx_info.get("used_percentage")

    now = time.time()
    cfg = load_config()
    st = get_effective_budget_settings(cfg)
    lang = _resolve_lang(cfg)

    cost_5h = cost_7d = 0.0
    in_5h = cache_5h = 0

    if DB_PATH.exists():
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=0.5)
        try:
            s5, _, _ = resolve_active_5h_window(conn, cfg, now)
            s7, _, _ = resolve_active_weekly_window(cfg, now)
            cost_5h, in_5h, cache_5h = query_fast_totals(conn, s5, now)
            cost_7d, _, _ = query_fast_totals(conn, s7, now)
        except Exception:
            pass
        finally:
            conn.close()

    denom_5h = in_5h + cache_5h
    hit_5h = (cache_5h / denom_5h * 100.0) if denom_5h > 0 else 0.0

    pct_5h = (cost_5h / st["per_member_5h_usd"] * 100.0) if st["per_member_5h_usd"] > 0 else 0.0
    pct_7d = (cost_7d / st["per_member_weekly_usd"] * 100.0) if st["per_member_weekly_usd"] > 0 else 0.0
    unit = "人" if lang == "zh" else "p"
    fam_tag = f"/{st['family_size']}{unit}" if st["family_size"] > 1 else ""

    parts = []
    if model_name:
        parts.append(str(model_name))
    if used_pct is not None:
        try:
            parts.append(f"ctx {float(used_pct):.0f}%")
        except Exception:
            pass

    if plain:
        parts.append(f"5h: {fmt_usd(cost_5h)} ({pct_5h:.0f}%{fam_tag}, hit {hit_5h:.0f}%)")
        parts.append(f"7d: {fmt_usd(cost_7d)} ({pct_7d:.0f}%{fam_tag})")
        print(" | ".join(parts))
    else:
        c_cyan = "\033[36m"
        c_green = "\033[32m"
        c_yellow = "\033[33m"
        c_red = "\033[31m"
        c_reset = "\033[0m"
        c5 = c_red if pct_5h >= 95 else (c_yellow if pct_5h >= 70 else c_green)
        c7 = c_red if pct_7d >= 95 else (c_yellow if pct_7d >= 70 else c_green)
        left = f"{c_cyan}{' · '.join(parts)}{c_reset} | " if parts else ""
        print(
            f"{left}{c5}5h: {fmt_usd(cost_5h)} ({pct_5h:.0f}%{fam_tag}){c_reset} "
            f"{c_green}hit {hit_5h:.0f}%{c_reset} | "
            f"{c7}7d: {fmt_usd(cost_7d)} ({pct_7d:.0f}%{fam_tag}){c_reset}"
        )


if __name__ == "__main__":
    main()

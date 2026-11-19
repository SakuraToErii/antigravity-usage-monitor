#!/usr/bin/env python3
"""Incremental OpenSSH remote transcript sync for Antigravity App (SSH) & remote agy CLI.

Zero remote installation required:
- Connects via standard OpenSSH (`BatchMode=yes`, respects `~/.ssh/config` & `ProxyJump`).
- Runs a compact inline Python 3 reader over stdin (`python3 -`) that checks
  `~/.gemini/*/brain/*/{.system_generated/logs/transcript.jsonl,transcript.jsonl}`
  on the remote Linux machine.
- Skips unchanged files using cached `(mtime, size)` in local `file_state`.
- Streams back ONLY compact token-count tuples (~120 bytes/step) instead of full transcripts.
- Upserts into local `~/.gemini/local-usage-monitor/usage.db` with `host=<ssh_host>` and
  deduplicates on `(surface, conversation_id, step_index)`.
"""

from __future__ import annotations

import concurrent.futures
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker import (
    get_connection,
    load_config,
    save_config,
    upsert_remote_file_steps,
)

SENTINEL = "__LUM_JSON__"
DEFAULT_AUTO_SYNC_SEC = 60

_HOST_VALID_RE = re.compile(r"^[A-Za-z0-9._@:\[\]-]+$")
_HOST_PORT_RE = re.compile(r"^(?P<target>(?:[A-Za-z0-9._-]+@)?(?:\[[0-9a-fA-F:]+\]|[A-Za-z0-9._-]+)):(?P<port>\d{1,5})$")

_REMOTE_EXTRACTOR_BODY = r"""
import json, os, re
from pathlib import Path

MODEL_RE = re.compile(r"The user changed setting `Model Selection` from .*? to ([^.\n]+)\.", re.IGNORECASE)

def surface_for(name):
    low = name.lower()
    if low == "antigravity-cli":
        return "cli"
    if low == "antigravity-ide":
        return "ide"
    if low in ("antigravity", ".antigravity-hub-server"):
        return "app"
    return "other"

roots = []
home = Path.home()
gemini_dir = home / ".gemini"
if gemini_dir.is_dir():
    try:
        for child in sorted(gemini_dir.iterdir()):
            if child.is_dir():
                b = child / "brain"
                if b.is_dir():
                    roots.append((surface_for(child.name), b))
    except PermissionError:
        pass

hub_brain = home / ".antigravity-hub-server" / "brain"
if hub_brain.is_dir():
    roots.append(("app", hub_brain))

files_found = 0
updated = []

for surface, root in roots:
    try:
        conv_dirs = list(root.iterdir())
    except PermissionError:
        continue
    for conv_dir in conv_dirs:
        if not conv_dir.is_dir():
            continue
        cand = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
        if not cand.is_file():
            cand = conv_dir / "transcript.jsonl"
            if not cand.is_file():
                continue
        files_found += 1
        try:
            st = cand.stat()
            abs_p = str(cand.resolve())
        except OSError:
            continue

        prev = KNOWN.get(abs_p)
        if prev and abs(prev[0] - st.st_mtime) < 1e-6 and int(prev[1]) == int(st.st_size):
            continue

        cur_model = ""
        steps = []
        try:
            with cand.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    t = entry.get("type", "")
                    if t == "USER_INPUT":
                        c = entry.get("content", "")
                        if isinstance(c, str) and "Model Selection" in c:
                            ms = MODEL_RE.findall(c)
                            if ms:
                                cur_model = ms[-1].strip()
                        continue
                    if t != "PLANNER_RESPONSE":
                        continue
                    in_tok = int(entry.get("input_tokens") or 0)
                    cache_tok = int(entry.get("cache_read_tokens") or 0)
                    out_tok = int(entry.get("output_tokens") or 0)
                    if in_tok == 0 and cache_tok == 0 and out_tok == 0:
                        continue
                    step_idx = int(entry.get("step_index") or 0)
                    created_at = str(entry.get("created_at") or "")
                    step_model = str(entry.get("model") or "").strip()
                    raw_model = step_model or cur_model or "unknown"
                    if step_model:
                        cur_model = step_model
                    steps.append([step_idx, created_at, raw_model, in_tok, cache_tok, out_tok])
        except OSError:
            continue

        updated.append({
            "surface": surface,
            "conv_id": conv_dir.name,
            "path": abs_p,
            "mtime": st.st_mtime,
            "size": st.st_size,
            "last_model": cur_model,
            "steps": steps,
        })

print("\n" + SENTINEL)
print(json.dumps({"files_found": files_found, "updated": updated}, separators=(",", ":")))
"""


def build_remote_script(known_files: Dict[str, List[float]]) -> str:
    header = (
        f"SENTINEL = {json.dumps(SENTINEL)}\n"
        f"KNOWN = json.loads({json.dumps(json.dumps(known_files, separators=(',', ':')))})\n"
    )
    return "import json\n" + header + _REMOTE_EXTRACTOR_BODY


def validate_host_spec(host: str) -> str:
    h = (host or "").strip()
    if not h or h.startswith("-") or not _HOST_VALID_RE.match(h):
        raise ValueError("无效的 SSH 主机名（支持 ~/.ssh/config 别名、user@host 或 user@host:port）")
    return h


def build_ssh_command(host_spec: str) -> List[str]:
    h = validate_host_spec(host_spec)
    ssh_bin = os.environ.get("ANTIGRAVITY_SSH_BIN") or "ssh"
    cmd = [
        ssh_bin,
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=6",
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
    ]
    m = _HOST_PORT_RE.match(h)
    if m:
        port = int(m.group("port"))
        if 1 <= port <= 65535:
            cmd.extend(["-p", str(port), m.group("target")])
        else:
            raise ValueError(f"无效端口号: {port}")
    else:
        cmd.append(h)
    cmd.extend(["python3", "-"])
    return cmd


def discover_ssh_config_hosts(ssh_config_path: Optional[Path] = None) -> List[str]:
    """Safely parse concrete Host aliases from ~/.ssh/config (read-only, skips wildcards)."""
    root_path = ssh_config_path or (Path.home() / ".ssh" / "config")
    seen: Set[str] = set()
    ordered: List[str] = []
    visited_files: Set[str] = set()

    def parse_file(p: Path) -> None:
        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key in visited_files or not p.is_file():
            return
        visited_files.add(key)
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return
        for raw_line in lines:
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            directive = parts[0].lower()
            if directive == "include":
                for pattern in parts[1:]:
                    pat_path = Path(pattern).expanduser()
                    if not pat_path.is_absolute():
                        pat_path = (Path.home() / ".ssh" / pattern).expanduser()
                    for match in sorted(glob.glob(str(pat_path))):
                        parse_file(Path(match))
            elif directive == "host":
                for token in parts[1:]:
                    if any(ch in token for ch in ("*", "?", "!")) or token.startswith("-"):
                        continue
                    if token not in seen:
                        seen.add(token)
                        ordered.append(token)

    parse_file(root_path)
    return ordered


def normalize_ssh_hosts_config(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = cfg.get("ssh_hosts")
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in raw:
        if isinstance(item, str):
            h = item.strip()
            enabled = True
        elif isinstance(item, dict):
            h = str(item.get("host") or "").strip()
            enabled = bool(item.get("enabled", True))
        else:
            continue
        if not h or h in seen:
            continue
        seen.add(h)
        out.append({"host": h, "enabled": enabled})
    return out


def add_ssh_host(host: str, enabled: bool = True) -> List[Dict[str, Any]]:
    h = validate_host_spec(host)
    cfg = load_config()
    hosts = normalize_ssh_hosts_config(cfg)
    for item in hosts:
        if item["host"] == h:
            item["enabled"] = enabled
            break
    else:
        hosts.append({"host": h, "enabled": enabled})
    cfg["ssh_hosts"] = hosts
    save_config(cfg)
    return hosts


def set_ssh_host_enabled(host: str, enabled: bool) -> List[Dict[str, Any]]:
    h = validate_host_spec(host)
    cfg = load_config()
    hosts = normalize_ssh_hosts_config(cfg)
    found = False
    for item in hosts:
        if item["host"] == h:
            item["enabled"] = bool(enabled)
            found = True
            break
    if not found:
        hosts.append({"host": h, "enabled": bool(enabled)})
    cfg["ssh_hosts"] = hosts
    save_config(cfg)
    return hosts


def remove_ssh_host(host: str, purge_data: bool = False) -> List[Dict[str, Any]]:
    h = (host or "").strip()
    cfg = load_config()
    hosts = [item for item in normalize_ssh_hosts_config(cfg) if item["host"] != h]
    cfg["ssh_hosts"] = hosts
    save_config(cfg)
    conn = get_connection()
    try:
        conn.execute("DELETE FROM remote_sync_state WHERE host = ?", (h,))
        if purge_data:
            conn.execute("DELETE FROM usage_steps WHERE host = ?", (h,))
            conn.execute("DELETE FROM file_state WHERE file_path LIKE ?", (f"ssh://{h}/%",))
        conn.commit()
    finally:
        conn.close()
    return hosts


def _summarize_ssh_error(stderr: str, returncode: int) -> str:
    low = (stderr or "").lower()
    if "permission denied" in low or "publickey" in low:
        return "SSH 免密认证失败（请先配置公钥或 ssh-agent）"
    if "could not resolve hostname" in low or "nodename nor servname" in low:
        return "主机名无法解析"
    if "connection refused" in low:
        return "SSH 端口连接被拒绝"
    if "operation timed out" in low or "connection timed out" in low:
        return "SSH 连接超时"
    if "host key verification failed" in low:
        return "Host Key 未信任（请先手动 ssh 登录一次）"
    if "python3: command not found" in low or "python3: not found" in low:
        return "远端未安装 python3"
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if lines:
        return lines[-1][:140]
    return f"SSH 退出码 {returncode}"


def parse_extractor_output(stdout: str) -> Dict[str, Any]:
    idx = stdout.rfind(SENTINEL)
    if idx < 0:
        raise ValueError("远端返回格式异常（未找到数据标记）")
    payload_str = stdout[idx + len(SENTINEL):].strip()
    data = json.loads(payload_str)
    if not isinstance(data, dict):
        raise ValueError("远端返回非 JSON 对象")
    return data


def _update_sync_state(
    conn,
    host: str,
    *,
    last_attempt_epoch: float,
    last_success_epoch: Optional[float] = None,
    status: str,
    error: str = "",
    files_found: int = 0,
    files_updated: int = 0,
    steps_synced: int = 0,
    duration_ms: int = 0,
) -> None:
    if last_success_epoch is not None:
        conn.execute(
            """
            INSERT INTO remote_sync_state (
                host, last_attempt_epoch, last_success_epoch, status, error,
                files_found, files_updated, steps_synced, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(host) DO UPDATE SET
                last_attempt_epoch=excluded.last_attempt_epoch,
                last_success_epoch=excluded.last_success_epoch,
                status=excluded.status,
                error=excluded.error,
                files_found=excluded.files_found,
                files_updated=excluded.files_updated,
                steps_synced=excluded.steps_synced,
                duration_ms=excluded.duration_ms
            """,
            (
                host,
                last_attempt_epoch,
                last_success_epoch,
                status,
                error,
                files_found,
                files_updated,
                steps_synced,
                duration_ms,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO remote_sync_state (
                host, last_attempt_epoch, last_success_epoch, status, error,
                files_found, files_updated, steps_synced, duration_ms
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(host) DO UPDATE SET
                last_attempt_epoch=excluded.last_attempt_epoch,
                status=excluded.status,
                error=excluded.error,
                duration_ms=excluded.duration_ms
            """,
            (
                host,
                last_attempt_epoch,
                status,
                error,
                files_found,
                files_updated,
                steps_synced,
                duration_ms,
            ),
        )
    conn.commit()


def ingest_remote_payload(host: str, payload: Dict[str, Any], conn=None) -> Tuple[int, int, int]:
    """Ingest a parsed remote extractor JSON payload into SQLite.

    Returns (files_found, files_updated, steps_synced).
    """
    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        files_found = int(payload.get("files_found") or 0)
        updated_files = payload.get("updated") or []
        steps_synced = 0
        for item in updated_files:
            steps_synced += upsert_remote_file_steps(
                conn=conn,
                host=host,
                surface=str(item.get("surface") or "other"),
                conv_id=str(item.get("conv_id") or ""),
                remote_path=str(item.get("path") or ""),
                mtime=float(item.get("mtime") or 0.0),
                size=int(item.get("size") or 0),
                last_model=str(item.get("last_model") or ""),
                steps=item.get("steps") or [],
            )
        conn.commit()
        return files_found, len(updated_files), steps_synced
    finally:
        if own_conn:
            conn.close()


def sync_ssh_host(host: str, force: bool = False, timeout: int = 20) -> Dict[str, Any]:
    """Incrementally sync Antigravity App/CLI/IDE transcript steps from a single SSH host."""
    h = validate_host_spec(host)
    t0 = time.time()
    prefix = f"ssh://{h}"

    conn = get_connection()
    try:
        known_files: Dict[str, List[float]] = {}
        if not force:
            rows = conn.execute(
                "SELECT file_path, mtime, size FROM file_state WHERE file_path LIKE ?",
                (prefix + "/%",),
            ).fetchall()
            for fp, mtime, size in rows:
                remote_abs = fp[len(prefix):]
                known_files[remote_abs] = [float(mtime), int(size)]

        _update_sync_state(conn, h, last_attempt_epoch=t0, status="syncing", error="")
    finally:
        conn.close()

    cmd = build_ssh_command(h)
    script = build_remote_script(known_files)

    try:
        proc = subprocess.run(
            cmd,
            input=script,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        dur_ms = int((time.time() - t0) * 1000)
        err = f"同步超时（>{timeout}s）"
        conn = get_connection()
        try:
            _update_sync_state(conn, h, last_attempt_epoch=t0, status="error", error=err, duration_ms=dur_ms)
        finally:
            conn.close()
        return {"host": h, "ok": False, "error": err, "duration_ms": dur_ms}
    except FileNotFoundError:
        dur_ms = int((time.time() - t0) * 1000)
        err = "未找到 OpenSSH 客户端 (ssh)"
        conn = get_connection()
        try:
            _update_sync_state(conn, h, last_attempt_epoch=t0, status="error", error=err, duration_ms=dur_ms)
        finally:
            conn.close()
        return {"host": h, "ok": False, "error": err, "duration_ms": dur_ms}
    except Exception as exc:
        dur_ms = int((time.time() - t0) * 1000)
        err = str(exc)[:140]
        conn = get_connection()
        try:
            _update_sync_state(conn, h, last_attempt_epoch=t0, status="error", error=err, duration_ms=dur_ms)
        finally:
            conn.close()
        return {"host": h, "ok": False, "error": err, "duration_ms": dur_ms}

    dur_ms = int((time.time() - t0) * 1000)
    if proc.returncode != 0:
        err = _summarize_ssh_error(proc.stderr, proc.returncode)
        conn = get_connection()
        try:
            _update_sync_state(conn, h, last_attempt_epoch=t0, status="error", error=err, duration_ms=dur_ms)
        finally:
            conn.close()
        return {"host": h, "ok": False, "error": err, "duration_ms": dur_ms}

    try:
        payload = parse_extractor_output(proc.stdout)
    except Exception as exc:
        err = str(exc)[:140]
        conn = get_connection()
        try:
            _update_sync_state(conn, h, last_attempt_epoch=t0, status="error", error=err, duration_ms=dur_ms)
        finally:
            conn.close()
        return {"host": h, "ok": False, "error": err, "duration_ms": dur_ms}

    now = time.time()
    conn = get_connection()
    try:
        files_found, files_updated, steps_synced = ingest_remote_payload(h, payload, conn=conn)
        _update_sync_state(
            conn,
            h,
            last_attempt_epoch=t0,
            last_success_epoch=now,
            status="ok",
            error="",
            files_found=files_found,
            files_updated=files_updated,
            steps_synced=steps_synced,
            duration_ms=dur_ms,
        )
    finally:
        conn.close()

    return {
        "host": h,
        "ok": True,
        "files_found": files_found,
        "files_updated": files_updated,
        "steps_synced": steps_synced,
        "duration_ms": dur_ms,
    }


def sync_all_ssh_hosts(
    force: bool = False,
    hosts_override: Optional[List[str]] = None,
    timeout: int = 20,
    max_workers: int = 4,
) -> List[Dict[str, Any]]:
    if hosts_override is not None:
        targets = [validate_host_spec(h) for h in hosts_override if h and h.strip()]
    else:
        cfg = load_config()
        targets = [item["host"] for item in normalize_ssh_hosts_config(cfg) if item.get("enabled")]

    if not targets:
        return []

    if len(targets) == 1:
        return [sync_ssh_host(targets[0], force=force, timeout=timeout)]

    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(targets))) as pool:
        futs = {pool.submit(sync_ssh_host, h, force, timeout): h for h in targets}
        for fut in concurrent.futures.as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({"host": futs[fut], "ok": False, "error": str(exc)[:140]})

    order = {h: i for i, h in enumerate(targets)}
    results.sort(key=lambda r: order.get(r["host"], 999))
    return results


def get_remote_status(cfg: Optional[Dict[str, Any]] = None, syncing_hosts: Optional[Set[str]] = None) -> Dict[str, Any]:
    if cfg is None:
        cfg = load_config()
    configured = normalize_ssh_hosts_config(cfg)
    discovered = discover_ssh_config_hosts()
    try:
        auto_sync_sec = max(0, int(cfg.get("remote_auto_sync_sec", DEFAULT_AUTO_SYNC_SEC)))
    except Exception:
        auto_sync_sec = DEFAULT_AUTO_SYNC_SEC

    syncing_set = syncing_hosts or set()
    state_map: Dict[str, Dict[str, Any]] = {}
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT host, last_attempt_epoch, last_success_epoch, status, error,
                   files_found, files_updated, steps_synced, duration_ms
            FROM remote_sync_state
            """
        ).fetchall()
        for r in rows:
            state_map[r[0]] = {
                "last_attempt_epoch": r[1],
                "last_success_epoch": r[2],
                "status": r[3],
                "error": r[4],
                "files_found": r[5],
                "files_updated": r[6],
                "steps_synced": r[7],
                "duration_ms": r[8],
            }
    finally:
        conn.close()

    hosts_out = []
    for item in configured:
        h = item["host"]
        st = state_map.get(h, {})
        status = "syncing" if h in syncing_set else st.get("status", "idle")
        hosts_out.append(
            {
                "host": h,
                "enabled": item["enabled"],
                "status": status,
                "error": st.get("error", ""),
                "last_attempt_epoch": st.get("last_attempt_epoch"),
                "last_success_epoch": st.get("last_success_epoch"),
                "files_found": st.get("files_found", 0),
                "files_updated": st.get("files_updated", 0),
                "steps_synced": st.get("steps_synced", 0),
                "duration_ms": st.get("duration_ms", 0),
            }
        )

    configured_names = {item["host"] for item in configured}
    available_ssh = [h for h in discovered if h not in configured_names]

    return {
        "hosts": hosts_out,
        "discovered_ssh_hosts": discovered,
        "available_ssh_hosts": available_ssh,
        "auto_sync_sec": auto_sync_sec,
        "syncing": bool(syncing_set),
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Antigravity remote SSH host transcripts to local SQLite")
    parser.add_argument("--host", action="append", help="Specific SSH host(s) to sync")
    parser.add_argument("--add", type=str, help="Add and enable an SSH host")
    parser.add_argument("--remove", type=str, help="Remove an SSH host")
    parser.add_argument("--list", action="store_true", help="List configured & ~/.ssh/config hosts")
    parser.add_argument("--force", action="store_true", help="Force re-parse all remote transcripts")
    parser.add_argument("--timeout", type=int, default=20, help="Per-host SSH timeout in seconds")
    args = parser.parse_args()

    if args.add:
        add_ssh_host(args.add, enabled=True)
        print(f"已添加并启用远程主机: {args.add}")
    if args.remove:
        remove_ssh_host(args.remove)
        print(f"已移除远程主机: {args.remove}")
    if args.list:
        st = get_remote_status()
        print("已配置远程主机:")
        for item in st["hosts"]:
            flag = "启用" if item["enabled"] else "停用"
            print(f"  - {item['host']} [{flag}] status={item['status']} files={item['files_found']} err={item['error']}")
        print("~/.ssh/config 发现主机:", ", ".join(st["discovered_ssh_hosts"]) or "(无)")
    elif not args.add and not args.remove:
        res = sync_all_ssh_hosts(force=args.force, hosts_override=args.host, timeout=args.timeout)
        if not res:
            print("未配置任何已启用的 SSH 远程主机。可用 --add <host> 添加，或用 --list 查看 ~/.ssh/config。")
        for r in res:
            if r.get("ok"):
                print(
                    f"[OK] {r['host']}: 扫描 {r['files_found']} 个文件, 更新 {r['files_updated']} 个, "
                    f"同步 {r['steps_synced']} 条 ({r['duration_ms']}ms)"
                )
            else:
                print(f"[ERR] {r['host']}: {r.get('error')} ({r.get('duration_ms', 0)}ms)")

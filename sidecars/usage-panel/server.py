#!/usr/bin/env python3
"""Antigravity Aux Pane Web UI Sidecar server for local-usage-monitor.

- Reads index.html on each GET / so UI/theme changes take effect on refresh.
- Runs a lightweight background OpenSSH sync loop for configured remote Linux hosts
  without ever blocking GET /api/usage.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, urlparse

SIDECAR_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SIDECAR_DIR.parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
INDEX_PATH = SIDECAR_DIR / "index.html"
sys.path.insert(0, str(SCRIPTS_DIR))

from pricing import DATA_DIR
from remote_sync import (
    DEFAULT_AUTO_SYNC_SEC,
    add_ssh_host,
    normalize_ssh_hosts_config,
    remove_ssh_host,
    set_ssh_host_enabled,
    sync_all_ssh_hosts,
    validate_host_spec,
)
from report import (
    PLAN_PRESETS,
    apply_usage_calibration,
    build_report_data,
    parse_local_datetime,
    parse_relative_duration,
)
from tracker import load_config, save_config

RUNTIME_STATE_PATH = DATA_DIR / "sidecar_runtime.json"

_sync_state_lock = threading.Lock()
_syncing_hosts: Set[str] = set()
_last_auto_sync_ts: float = 0.0


def _get_syncing_snapshot() -> Set[str]:
    with _sync_state_lock:
        return set(_syncing_hosts)


def _run_remote_sync(hosts: Optional[List[str]] = None, force: bool = False) -> List[Dict[str, Any]]:
    global _last_auto_sync_ts
    if hosts is not None:
        requested = [validate_host_spec(h) for h in hosts if h and h.strip()]
    else:
        cfg = load_config()
        requested = [item["host"] for item in normalize_ssh_hosts_config(cfg) if item.get("enabled")]

    if not requested:
        return []

    to_sync: List[str] = []
    with _sync_state_lock:
        for h in requested:
            if h not in _syncing_hosts:
                _syncing_hosts.add(h)
                to_sync.append(h)

    if not to_sync:
        return []

    try:
        results = sync_all_ssh_hosts(force=force, hosts_override=to_sync)
        _last_auto_sync_ts = time.time()
        return results
    finally:
        with _sync_state_lock:
            for h in to_sync:
                _syncing_hosts.discard(h)


def _kick_async_sync(hosts: Optional[List[str]] = None, force: bool = False) -> None:
    t = threading.Thread(target=_run_remote_sync, args=(hosts, force), daemon=True)
    t.start()


def _auto_sync_loop() -> None:
    global _last_auto_sync_ts
    time.sleep(1.5)
    while True:
        try:
            cfg = load_config()
            enabled = [item["host"] for item in normalize_ssh_hosts_config(cfg) if item.get("enabled")]
            try:
                interval = max(0, int(cfg.get("remote_auto_sync_sec", DEFAULT_AUTO_SYNC_SEC)))
            except Exception:
                interval = DEFAULT_AUTO_SYNC_SEC

            if enabled and interval > 0 and (time.time() - _last_auto_sync_ts) >= interval:
                _run_remote_sync(hosts=enabled, force=False)
        except Exception:
            pass
        time.sleep(5.0)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content: str, status: int = 200) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _build_current_report(self, scan: bool = False, force_reprice: bool = False) -> Dict[str, Any]:
        return build_report_data(
            scan=scan,
            force_reprice=force_reprice,
            sync_remote=False,
            syncing_hosts=_get_syncing_snapshot(),
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_html(INDEX_PATH.read_text(encoding="utf-8"))
            return
        if parsed.path == "/healthz":
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/usage":
            qs = parse_qs(parsed.query)
            do_scan = qs.get("scan", ["1"])[0] != "0"
            data = self._build_current_report(scan=do_scan, force_reprice=False)
            self._send_json(data)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send_json({"error": "invalid json"}, status=400)
            return

        if parsed.path == "/api/calibrate":
            try:
                cal = apply_usage_calibration(
                    str(body.get("text") or ""),
                    calibrate_budgets=bool(body.get("calibrate_budgets")),
                )
                rep = self._build_current_report(scan=False, force_reprice=False)
                self._send_json({"calibration": cal, "report": rep})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        if parsed.path == "/api/config":
            cfg = load_config()
            if "plan" in body and body["plan"] in PLAN_PRESETS:
                cfg["plan"] = body["plan"]
            if "family_size" in body and body["family_size"] is not None:
                cfg["family_size"] = max(1, min(6, int(body["family_size"])))
            if "budget_5h_usd" in body and body["budget_5h_usd"] is not None:
                cfg["budget_5h_usd"] = max(0.01, float(body["budget_5h_usd"]))
            if "budget_weekly_usd" in body and body["budget_weekly_usd"] is not None:
                cfg["budget_weekly_usd"] = max(0.01, float(body["budget_weekly_usd"]))
            if "remote_auto_sync_sec" in body and body["remote_auto_sync_sec"] is not None:
                cfg["remote_auto_sync_sec"] = max(0, int(body["remote_auto_sync_sec"]))
            if body.get("clear_anchors"):
                cfg.pop("reset_5h_anchor_epoch", None)
                cfg.pop("reset_weekly_anchor_epoch", None)
                cfg.pop("last_official_snapshot", None)
            now = time.time()
            try:
                if body.get("set_5h_in"):
                    cfg["reset_5h_anchor_epoch"] = now + parse_relative_duration(str(body["set_5h_in"]))
                if body.get("set_weekly_in"):
                    cfg["reset_weekly_anchor_epoch"] = now + parse_relative_duration(str(body["set_weekly_in"]))
                if body.get("set_5h_reset"):
                    cfg["reset_5h_anchor_epoch"] = parse_local_datetime(str(body["set_5h_reset"]))
                if body.get("set_weekly_reset"):
                    cfg["reset_weekly_anchor_epoch"] = parse_local_datetime(str(body["set_weekly_reset"]))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
                return

            save_config(cfg)
            self._send_json(self._build_current_report(scan=False, force_reprice=False))
            return

        if parsed.path == "/api/remote/hosts":
            try:
                action = str(body.get("action") or "").strip().lower()
                host = str(body.get("host") or "").strip()
                if "auto_sync_sec" in body and body["auto_sync_sec"] is not None:
                    cfg = load_config()
                    cfg["remote_auto_sync_sec"] = max(0, int(body["auto_sync_sec"]))
                    save_config(cfg)
                if action == "add":
                    add_ssh_host(host, enabled=True)
                    _kick_async_sync([host], force=False)
                elif action == "toggle":
                    enabled = bool(body.get("enabled", True))
                    set_ssh_host_enabled(host, enabled=enabled)
                    if enabled:
                        _kick_async_sync([host], force=False)
                elif action == "remove":
                    remove_ssh_host(host, purge_data=bool(body.get("purge_data", False)))
                elif action:
                    self._send_json({"error": f"unknown action: {action}"}, status=400)
                    return
                self._send_json(self._build_current_report(scan=False, force_reprice=False))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        if parsed.path == "/api/remote/sync":
            try:
                host = str(body.get("host") or "").strip()
                hosts = [host] if host else None
                force = bool(body.get("force", False))
                is_async = bool(body.get("async", False))
                if is_async:
                    _kick_async_sync(hosts=hosts, force=force)
                    self._send_json({"started": True, "report": self._build_current_report(scan=False)})
                    return
                results = _run_remote_sync(hosts=hosts, force=force)
                self._send_json({"results": results, "report": self._build_current_report(scan=False)})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)
            return

        if parsed.path == "/api/reprice":
            data = self._build_current_report(scan=True, force_reprice=True)
            self._send_json(data)
            return

        self._send_json({"error": "not found"}, status=404)


def resolve_port() -> tuple[int, str]:
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv) and argv[i + 1].isdigit():
            return int(argv[i + 1]), "argv:--port"
        if arg.startswith("--port="):
            val = arg.split("=", 1)[1]
            if val.isdigit():
                return int(val), "argv:--port="
    for key in (
        "ANTIGRAVITY_SIDECAR_WEB_PORT",
        "ANTIGRAVITY_SIDECAR_PORT",
        "SIDECAR_WEB_PORT",
        "SIDECAR_PORT",
        "WEB_PORT",
        "PORT",
    ):
        val = os.environ.get(key)
        if val and val.isdigit():
            return int(val), f"env:{key}"
    return 18765, "default"


def main() -> None:
    port, source = resolve_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual_port = server.server_address[1]
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_STATE_PATH.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "port": actual_port,
                    "port_source": source,
                    "started_at": time.time(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    threading.Thread(target=_auto_sync_loop, daemon=True).start()
    print(f"local-usage-monitor sidecar listening on http://127.0.0.1:{actual_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

# Antigravity Usage Monitor

**English** | [简体中文](README.zh-CN.md)

Lightweight local & SSH-remote usage monitor for Google Antigravity (App / `agy` CLI / IDE). Tracks API-equivalent spend (with prompt-cache hit discounts), **5-hour** & **weekly (168h)** quota windows, and family-group per-member share.

> Pure Python 3 stdlib, zero third-party dependencies. Reads only local or OpenSSH-fetched `transcript.jsonl` logs — never proxies traffic, touches OAuth tokens, or calls Google endpoints.

---

## Features

- **App Sidecar Panel**: Minimal UI that adapts to Antigravity Light/Dark theme and App language (with 1-click `EN`/`中` toggle).
- **Family Share Tracking**: Switch plan tier (`Pro` / `Ultra 5x` / `Ultra 20x` / `Custom`) and family size (1–6).
- **SSH Remote Sync**: Zero remote setup; auto-discovers `~/.ssh/config` hosts, incrementally pulls remote App / `agy` CLI usage, and deduplicates across aliases.
- **`agy` CLI Statusline**: Live 5h / weekly spend & cache hit rate in your terminal footer.

---

## Install

Requires `python3` (pre-installed on macOS / Linux).

```bash
git clone https://github.com/SakuraToErii/antigravity-usage-monitor.git \
  ~/.gemini/config/plugins/local-usage-monitor
```

1. **Antigravity App**: Open **Skills & Customizations** and ensure **Local Usage Monitor** is enabled to view it in the side pane.
2. **`agy` CLI Statusline (optional)**: Run inside `agy`:
   ```text
   /statusline ~/.gemini/config/plugins/local-usage-monitor/scripts/statusline.py
   ```

---

## Quick Usage

- **Sync Reset Countdown**: Expand **Budgets & Reset Sync** and paste `/usage` output to align 5h / weekly reset times.
- **Monitor SSH Projects**: Expand **Remote SSH Hosts**, click a host from `~/.ssh/config` (or enter `user@host[:port]`), and enable auto sync (60s).
- **Custom Model Pricing (optional)**: Edit `~/.gemini/local-usage-monitor/pricing_overrides.json` and run `--reprice`.

---

## CLI Commands

```bash
# Terminal report (--html exports an offline dashboard)
python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py

# Add and sync an SSH host immediately
python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py --add-ssh-host <host> --sync-remote

# Update plugin (toggle plugin off/on in App after pulling)
git -C ~/.gemini/config/plugins/local-usage-monitor pull
```

---

## Data Storage

User data lives separately in `~/.gemini/local-usage-monitor/`:
- `usage.db`: Combined local + remote SQLite ledger
- `config.json`: Plan, family size, reset anchors, language, and SSH hosts

## License

MIT

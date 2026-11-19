---
name: local-usage
description: Inspect local Antigravity App and agy CLI token consumption, official API-equivalent cost (including prompt cache hits), and family-group per-member quota share across 5-hour and weekly rolling windows, generate an HTML dashboard, or calibrate account reset anchors.
---

# Local Usage Monitor Skill

Use this skill whenever the user asks to check their local Antigravity / `agy` CLI usage, equivalent API cost, prompt cache hit savings, family-group per-member quota %, or 5-hour / weekly rolling window spend, or wants to open the App UI panel or calibrate reset times from `/usage`.

## App Sidecar UI Panel

This plugin bundles a Web UI sidecar at `sidecars/usage-panel/` (`sidecarId`: `local-usage-monitor/usage-panel`, `entrypoint`: `SIDECAR_UI_ENTRYPOINT_AUX_PANE`, `path`: `/`).
- If the sidecar is enabled and running, you can surface the one-click pill `[本地用量监控](sidecar://local-usage-monitor/usage-panel/)` to toggle it in the Auxiliary Side Pane.
- Users can also manage/enable the plugin and its sidecar from `[Local Usage Monitor](customization://local-usage-monitor)`.

## Commands

Plugin script root: `~/.gemini/config/plugins/local-usage-monitor/scripts/`

1. **Show 5-hour & weekly rolling usage report (auto-syncs all App/CLI/IDE transcripts):**
   ```bash
   python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py
   ```

2. **Configure plan tier, family group member count (1-6), and custom 5h / weekly USD budgets:**
   ```bash
   python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py \
     --set-plan ultra_5x --set-family-size 2 --set-5h-budget 50 --set-weekly-budget 250
   ```
   Supported `--set-plan` values: `pro`, `ultra_5x`, `ultra_20x`, `custom`.

3. **Generate an offline HTML dashboard:**
   ```bash
   python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py --html
   ```
   Output file: `~/.gemini/local-usage-monitor/dashboard.html`.

4. **Calibrate to the account's exact `/usage` reset countdowns (optional):**
   ```bash
   python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py --set-5h-in 3h20m --set-weekly-in 4d12h
   ```
   Clear calibrated anchors: `--clear-anchors`.

5. **Sync remote SSH Linux projects (Antigravity App SSH & remote `agy` CLI):**
   ```bash
   # List ~/.ssh/config discovered & configured hosts
   python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py --list-ssh-hosts

   # Add SSH host(s) and sync immediately
   python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py --add-ssh-host <host> --sync-remote

   # Or run standalone incremental SSH sync
   python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/remote_sync.py
   ```
   Zero remote installation required; connects over standard OpenSSH (`BatchMode=yes`, respects `~/.ssh/config` & `ProxyJump`) and pulls only compact token counts from `~/.gemini/*/brain/` on the remote host.

6. **Re-price historical records (local + synced remote) after editing `~/.gemini/local-usage-monitor/pricing_overrides.json`:**
   ```bash
   python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py --reprice
   ```

## Important Rules
- Never touch OAuth tokens or call any remote billing/quota endpoints. Everything runs 100% locally on `~/.gemini/local-usage-monitor/usage.db` and local `transcript.jsonl` files.
- Family group member count cannot be read offline and is configured by the user in the App UI panel or via `--set-family-size`.
- Official plan USD ceilings are not published by Google; built-in presets are community reference estimates and can be edited anytime by the user.

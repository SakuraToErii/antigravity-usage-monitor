# Antigravity Usage Monitor

轻量、纯本地的 Google Antigravity（App / `agy` CLI / IDE）用量监控插件。基于官方公开 API 单价（含 Prompt Cache 命中折扣），按 **5 小时** 与 **每周（168h）** 窗口实时统计个人等效花费、缓存节省与家庭组人均占比，并支持汇总 **SSH 远程项目**。

> 纯 Python 3 标准库，零第三方依赖；仅读取本地或经 OpenSSH 拉取的 `transcript.jsonl`，不反代、不碰 OAuth 凭证、不请求任何 Google 接口。

---

## 核心功能

- **App 侧边栏面板**：极简界面，自动适配 Antigravity 明暗主题，一眼看清 5h / 每周占比与各模型明细。
- **家庭组人均份额**：一键切换套餐档位（Pro / Ultra 5x / Ultra 20x / 自定义）与家庭组人数（1–6 人）。
- **SSH 远程项目聚合**：零远端安装，自动识别 `~/.ssh/config` 主机，增量同步远端 App / `agy` CLI 用量并自动去重。
- **`agy` CLI 实时状态栏**：终端底部常驻显示 5h / 每周费用与缓存命中率。

---

## 快速安装

需要本机已安装 `python3`（macOS / Linux 默认自带）。

```bash
git clone https://github.com/SakuraToErii/antigravity-usage-monitor.git \
  ~/.gemini/config/plugins/local-usage-monitor
```

安装完成后：

1. **Antigravity App**：打开左侧 **Skills & Customizations**，确认 **Local Usage Monitor** 已启用，即可在右侧辅助栏打开 **「本地用量监控」**。
2. **`agy` CLI 状态栏（可选）**：在 `agy` 内执行一次：
   ```text
   /statusline ~/.gemini/config/plugins/local-usage-monitor/scripts/statusline.py
   ```

---

## 使用说明

- **对齐账号重置时间**：展开面板底部 **「额度与周期校准」**，粘贴 `/usage` 输出即可同步官方 5h / 每周刷新倒计时（5h 窗口默认也会按首笔请求自动推算）。
- **监控 SSH 远程项目**：展开底部 **「远程 SSH 主机」**，一键勾选 `~/.ssh/config` 主机或手动输入 `user@host[:port]`，后台每 60 秒自动增量同步（需本机免密 SSH 且远端装有 `python3`）。
- **自定义模型单价（可选）**：编辑 `~/.gemini/local-usage-monitor/pricing_overrides.json` 后运行 `--reprice` 重算历史记录。

---

## 常用命令

```bash
# 查看命令行报表（加 --html 可导出离线仪表盘）
python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py

# 添加并立即同步 SSH 远程主机
python3 ~/.gemini/config/plugins/local-usage-monitor/scripts/report.py --add-ssh-host <host> --sync-remote

# 拉取插件更新（更新后在 App 中开关一次插件即可生效）
git -C ~/.gemini/config/plugins/local-usage-monitor pull
```

---

## 数据存储

运行数据与插件代码分离，全部保存在 `~/.gemini/local-usage-monitor/`：

- `usage.db`：本地与远端合并的用量账本（SQLite）
- `config.json`：套餐档位、家庭组人数、周期锚点与 SSH 主机配置

## License

MIT

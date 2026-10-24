# Changelog

## [1.0.0] - 2026-10-21

首个公开版本。

### 功能

- NapCat OneBot 11 反向 WebSocket + HTTP API。
- OpenAI 兼容聊天与视觉模型；DeepSeek 官方 Search / Fetch。
- 人格提示词、短期上下文、全局/群级长期记忆、梗库。
- 模型自主联网与百炼 `enable_search`（二选一）。
- 百炼 Responses API 图搜图。
- 表情占位符与多段消息拆分。
- 本地命令：`#model`、`#时间`、`#记忆`、`#删除记忆`、`#搜索`、`#ds*`。

### 安全

- 移除所有真实 QQ 号、群号、真人身份、服务器地址、内部路径与 API 凭据。
- 人格与 owner 权限改为 `BOT_NAME` / `OWNER_QQ` 配置。
- 梗库、风格文件与记忆模板替换为通用示例。
- `dataset/` 清洗脚本改为显式传参，不再内置任何账号或本机路径。
- 新增 `.env.example`、`SECURITY.md` 与 MIT 许可证。

### 发布物

- Linux：`cyberyinyu-1.0.0-linux.tar.gz`（含 `install.sh` 与 systemd unit）。
- Windows：`cyberyinyu-1.0.0-windows.zip`（含 `install.ps1` 与 `start_bot.bat`）。
- `SHA256SUMS`：两个资产的校验和。

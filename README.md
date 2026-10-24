# CyberYinYu

一个可自托管的 QQ 聊天机器人：通过 **NapCat + OneBot 11 反向 WebSocket** 收发消息，调用任意 **OpenAI 兼容** 模型，并带有人格提示词、短期上下文、群级长期记忆、梗库、联网搜索、图搜图和表情/多段回复能力。

> 本仓库是通用公开版：所有角色设定、QQ 号、服务器信息与 API 凭据均已移除，人格与 `owner` 权限通过环境变量配置。

## 功能

- **人格化聊天**：`personality/style.txt` 控制说话风格，`BOT_NAME` 控制角色名。
- **私聊 / 群聊**：私聊默认自动回复；群聊默认只在被 `@` 时回复。
- **短期上下文**：每个会话保留最近若干轮，进程内存储，重启清空。
- **三层长期记忆**：
  - 全局事实：`memory/global.json`，人工维护，注入所有会话；
  - 群级记忆：`memory/groups/<群号>.json`，模型归纳 + 成员显式请求 + owner 确认；
  - 梗库：`memory/memes.json`，命中关键词才注入。
- **图片理解**：消息图片、`base64://`、引用消息图片。
- **联网搜索**：模型自主调用 DeepSeek 官方 `web_search`，或使用百炼 `enable_search`。
- **图搜图**：`#搜索` 基于百炼 Responses API 的 `image_search`。
- **表情与多段回复**：`[[表情名]]` 映射为 QQ 表情/图片，`[[QQ_MSG]]` 拆分多条消息。
- **本地命令**：`#model`、`#时间`、`#记忆`、`#删除记忆`、`#搜索`、`#ds*`。

## 架构

```
QQ 客户端 / 群聊
      │
      ▼
NapCat（OneBot 11 协议端）
      │  ① 反向 WebSocket：事件推给 bot
      │     ws://127.0.0.1:8080/onebot/v11/ws
      │  ② HTTP API：bot 调用发消息
      │     http://127.0.0.1:3000
      ▼
bot.py（aiohttp WebSocket Server + 事件路由）
      ├── ai/           提示词、模型参数兼容、联网、图搜图
      ├── memory/       短期上下文、全局/群级记忆、梗库
      └── personality/  风格、表情映射、空 @ 问候
      │
      ▼
OpenAI 兼容模型服务（聊天 / 视觉 / 图搜图 / 官方工具通道）
```

更完整的模块与流程说明见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 环境要求

- Python **3.10+**
- [NapCat](https://github.com/NapNeko/NapCatQQ)（OneBot 11）
- 一个 OpenAI 兼容的模型 API（默认示例为 DeepSeek；也可接百炼或其他网关）

## 快速开始

### Windows

1. 解压 `cyberyinyu-1.0.0-windows.zip`。
2. 右键运行 `install.ps1`（或在 PowerShell 中执行）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

3. 编辑生成的 `otowa.env`，至少填写 `BOT_QQ`、`NAPCAT_TOKEN`、`AI_API_KEY`、`OWNER_QQ`。
4. 在 NapCat 中启用 HTTP Server（3000）和反向 WebSocket Client（8080），token 与 `NAPCAT_TOKEN` 一致。
5. 双击 `start_bot.bat` 启动。

### Linux

```bash
tar -xzf cyberyinyu-1.0.0-linux.tar.gz
cd cyberyinyu-1.0.0
sudo bash install.sh
sudoedit /opt/cyberyinyu/otowa.env
sudo systemctl start cyberyinyu
sudo systemctl status cyberyinyu
```

默认安装到 `/opt/cyberyinyu`；可用 `APP_DIR=/your/path sudo bash install.sh` 覆盖。

### NapCat 配置

在 NapCat 的 OneBot 11 网络配置中同时启用两个适配器：

```json
{
  "network": {
    "httpServers": [{
      "name": "http-server",
      "enable": true,
      "host": "127.0.0.1",
      "port": 3000,
      "messagePostFormat": "array",
      "token": "与 otowa.env 的 NAPCAT_TOKEN 相同"
    }],
    "websocketClients": [{
      "name": "CyberYinYu",
      "enable": true,
      "url": "ws://127.0.0.1:8080/onebot/v11/ws",
      "messagePostFormat": "array",
      "reportSelfMessage": false,
      "reconnectInterval": 5000,
      "token": "与 otowa.env 的 NAPCAT_TOKEN 相同",
      "heartInterval": 30000
    }]
  }
}
```

合并到现有配置，不要整份覆盖。HTTP 与 WebSocket 都只绑定 `127.0.0.1`。

## 配置

完整键表见 [`.env.example`](.env.example)。最常用的：

| 键 | 说明 |
|---|---|
| `BOT_NAME` | 角色名，进入系统提示词 |
| `BOT_QQ` | **必填**，NapCat 实际登录的机器人 QQ 号 |
| `BOT_NICKNAME` | 用于识别「@昵称」形式的纯文本 @ |
| `OWNER_QQ` | 本实例最高权限用户；留空则关闭 owner 确认与删除记忆 |
| `AI_API_KEY` | 聊天模型密钥 |
| `AI_BASE_URL` | 只写到版本段（如 `https://api.deepseek.com`），不要带 `/chat/completions` |
| `AI_MODEL` | 文本模型 |
| `AI_VISION_MODEL` | 图片模型 |
| `NAPCAT_API` | NapCat HTTP 地址，默认 `http://127.0.0.1:3000` |
| `NAPCAT_TOKEN` | NapCat 的 HTTP/WS token |
| `GROUP_REPLY_ONLY_WHEN_MENTIONED` | 群聊是否只响应 `@`，默认 `true` |
| `MEMORY_CONFIRM_QQ` | 高危群记忆的确认人；留空沿用 `OWNER_QQ` |

**注意**：`config.py` 在 import 时读取一次 `otowa.env`，修改后必须重启进程；`style.txt`、`memes.json`、`memory/*.json` 每次生成都会重读，改完即生效。

## 命令

命令在 `@` 门禁之前处理，群聊不需要 `@`；但都受 `QQ_REPLY_WHITELIST` 限制。

| 命令 | 作用 | 权限 |
|---|---|---|
| `#model` | 查看当前文本/图片模型 | 所有人 |
| `#时间` / `#time` | 返回本机时间 | 所有人 |
| `#记忆` / `#memory` | 列出本群记忆（自动分页） | 所有人 |
| `#删除记忆 <序号\|id前缀\|片段>` | 删除一条群记忆 | `OWNER_QQ` |
| `#搜索 [描述]` | 图搜图（需带图） | 所有人（小时限额） |
| `#ds搜 <关键词>` | DeepSeek 官方搜索 | 所有人 |
| `#ds抓 <url>` | 抓取网页正文 | 所有人 |
| `#ds问 <问题>` | 先搜再答 | 所有人 |
| `#ds状态` | 通道配置与连通性 | 所有人 |
| `#确认` / `#拒绝` | 批准/驳回挂起的记忆写入 | `OWNER_QQ` |

## 目录结构

```
bot.py                 入口：WS 服务、事件路由、模型调用、命令
config.py              读取 otowa.env，导出配置常量
ai/                    提示词、模型兼容、联网、图搜图、限流
memory/                上下文、全局/群级记忆、梗库
personality/           风格、表情、问候语
dataset/               QQChatExporter 语料清洗（离线工具）
docs/                  架构与部署文档
packaging/             Linux/Windows 安装脚本与打包工具
```

## 构建发布包

```bash
python packaging/build_release.py
```

产物在 `dist/`：

- `cyberyinyu-<version>-linux.tar.gz`
- `cyberyinyu-<version>-windows.zip`
- `SHA256SUMS`

## 隐私与安全

- 仓库不包含任何真实密钥、token、QQ 号、群号或服务器信息。
- `otowa.env`、日志、群记忆、贴纸图片和数据集产物均已加入 `.gitignore`。
- 消息只在本机与所配置的模型 API 之间传输；没有遥测。
- 生产部署请只绑定 `127.0.0.1`，不要把 NapCat HTTP 或 bot WS 暴露到公网。
- 发现安全问题请按 [`SECURITY.md`](SECURITY.md) 私下报告，不要开公开 issue。

## 许可证

[MIT](LICENSE)

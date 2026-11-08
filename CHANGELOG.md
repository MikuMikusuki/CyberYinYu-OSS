# Changelog

## [1.1.0] - 2026-11-08

功能与架构同步版本。私有实例这两个多月新增的能力全部并入公开版，并按公开版口径重新脱敏。

### 新增

- **并发门**（`concurrency.py`）：同会话串行、跨会话并行；单会话积压上限、在途任务上限、过期消息丢弃与忙碌提示。
- **文生图 / 图像编辑**：`#画图`、`#改图`，接任意支持 `images/generations` / `images/edits` 的 OpenAI 兼容网关；先润色提示词再出图，用户原文一字不改。
- **以图搜源**：`#搜源`，SauceNAO 主备双 key、按日配额、相似度阈值与外传图床白名单。
- **表情包提取**：`#表情提取`，引用带表情的消息直接取图，不调模型。
- **戳一戳回复**：纯本地词库秒回，冷却 + 每分钟上限，不进模型、不进历史。
- **群内角色**：群主/管理员只认 OneBot 元数据；`ai/authority_guard.py` 拦截自称权限、冒充与要求改设定。
- **主体锚定**：记忆里的「谁是谁」锚到 QQ 号；`memory/subject.py` 处理人称代词，判断不出时反问。
- **群记忆开关**：`#关闭记忆` / `#开启记忆`，限群主、管理员或 owner。
- **接口增强**：`ai/web_fetch.py`（模型自己打开链接，带 SSRF 防护）、`ai/character_lookup.py`（作品/角色识别）、`memory/fewshot.py`（检索式 few-shot）、`ai/image_prompt.py`（提示词润色）。
- **热模式（可选，默认关闭）**：私聊专用的 18+ 提示词片段，`HEAT_MODE_ENABLED=false` 时不读取、不注入；只有 `HEAT_MODE_QQ` 里的账号能在私聊开启。仓库只提供模板，内容由部署者自负。

### 变更

- 模型通道继续分家：聊天 / 视觉 / 图搜 / 生图 / 搜源 / 官方工具各自独立配置，互不牵连。
- 系统提示词不再接受每轮输入，`[system, *history]` 作为稳定前缀以命中 provider prompt cache；每轮变化的身份、群角色、梗库命中改为 `build_turn_context` 生成的 tail 块。
- `BOT_QQ` 缺失时直接报错退出，不再使用任何内置默认号。
- 生图网关不再内置任何第三方默认地址（旧版默认值已移除），必须由部署者填写。
- 新增依赖：`Pillow`、`pypdf`。

### 安全

- 移除所有真实 QQ 号、群号、账号对应关系、内部路径与第三方网关域名。
- 数据集脚本不再内置目标账号：改为 `CYBERYINYU_TARGETS` / `CYBERYINYU_BOT_QQ` 显式传入，缺失即报错退出。
- `memory/GLOBAL_MEMORY.md`、`memory/GROUP_MEMORY_TEMPLATE.md`、`personality/pokes.py`、`personality/heat.txt` 中的真人示例全部替换为占位内容。
- `data/heat_mode.json`（热模式状态）加入 `.gitignore`。

### 发布物

- Linux：`cyberyinyu-1.1.0-linux.tar.gz`
- Windows：`cyberyinyu-1.1.0-windows.zip`
- `SHA256SUMS`：两个资产的校验和。

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

# Changelog

## [1.2.0] - 2026-11-27

新增可选 Web 管理控制台、SQLite 跨重启用量统计、模型通道实时体检，以及多项可选上下文增强能力；全部新增运行时能力均保持默认关闭、按需启用，不破坏原有部署行为。

### 新增

- **Web 管理控制台**（`webui.py`、`webui/`，默认关闭）：在 `otowa.env` 中设置 `WEBUI_ENABLED=true` 后与机器人同进程共用端口运行（默认 `http://127.0.0.1:8080/admin`），无外部前端依赖。涵盖：
  - 运行概览：NapCat WebSocket 连接状态、事件计数、并发门水位、各模型/工具通道配置与功能开关；
  - 在线试聊：复用线上同一套人格、记忆与工具链，支持私聊/群聊模式切换、指定群记忆注入及多段消息、`[[表情]]` 渲染预览；
  - 提示词与人格：实时预览按群组装后的系统提示词，在线编辑 `personality/style.txt` 与 `personality/heat.txt`（保存即生效）；
  - 记忆管理：可视化增删改查全局事实（`memory/global.json`）与群级记忆（`memory/groups/*.json`），复用底层原子写锁与主语/长度校验；
  - 表情包墙与外观：支持表情图片上传校验与映射管理、控制台自定义背景图与深浅主题切换；
  - 基本设置与实时日志：7 组配置项白名单校验、待重启状态比对，以及基于 SSE 的日志实时跟随与级别/关键字过滤。
- **SQLite 独立用量统计**（`ai/usage.py`、`/admin/#/usage`）：持久化记录至 `data/usage.sqlite3`，重启不清零。支持按「今天 / 近 7 天 / 全部」及每 5 秒自动刷新，分别按模型、功能通道与工具展示调用次数和已知 Token；准确区分成功、失败、取消、本地限流拒绝与服务商未返回 usage 的未知状态，且不重复累加 `reasoning_tokens`。
- **模型通道实时体检**（`#模型状态` / `#modelstatus`，`ai/model_status.py`）：并发探测已配置的文本与视觉通道真实连通性、密钥有效性与响应耗时；生图、改图与图搜等按次计费通道仅核验配置状态，不发起计费请求。
- **可选会话摘要与预算保护**（`memory/context_summary.py`，默认关闭）：设置 `CONTEXT_SUMMARY_ENABLED=true` 后自动压缩较早的已答对话轮次，并在请求发出前按本地保守 Token 预算检查输入体积。
- **可选近期群聊背景**（`group_background.py`，默认关闭）：设置 `GROUP_BACKGROUND_ENABLED=true` 后在内存中短时缓存近期未触发回复的普通群消息，在下次被叫到时作为对话背景注入。
- **视觉模型联网开关**（`VISION_WEB_SEARCH_ENABLED`，默认关闭）：允许同时支持视觉输入与工具调用的模型在带图提问中调用 DeepSeek 官方搜索。

### 变更

- 默认示例与回退文本模型由已停用的 `deepseek-chat` 更新为 `deepseek-flash`（`.env.example`、`config.py`）。
- 改进生图提示词润色（`ai/image_prompt.py`）、作品/角色检索（`ai/character_lookup.py`）与各通道用量分类统计。
- 收紧发布打包脚本（`packaging/build_release.py`）：仅收录 Git 已跟踪且未被忽略的文件，防止本地未跟踪文件或配置备份混入压缩包，同时保留 `dist/` 内其他产物。
- 发布工作流（`.github/workflows/release.yml` + `packaging/extract_release_notes.py`）在打 `v*` 标签发布时自动从 `CHANGELOG.md` 提取对应版本的更新说明写入 GitHub Release。

### 安全

- 控制台默认关闭；启用后默认仅接受本机回环访问（`127.0.0.1` / `::1`），强制校验 `Host` 头拦截 DNS Rebinding，写接口要求 `X-WebUI-Request` 头阻断跨源伪造请求；配置 `WEBUI_TOKEN` 后页面与数据接口均需鉴权。
- 控制台修改配置与人格前自动备份至 `data/_webui_backups/`（已列入 `.gitignore`），接口回显对所有密钥全程掩码；`data/usage.sqlite3` 不保存提示词、回复正文或 API Key。
- `ai/web_fetch.py` 在初始解析及每次 HTTP 重定向目标检查时均拒绝内网与回环地址。

### 发布物

- Linux：`cyberyinyu-1.2.0-linux.tar.gz`（含 `install.sh`、`run.sh` 与 `cyberyinyu.service`）
- Windows：`cyberyinyu-1.2.0-windows.zip`（含 `install.ps1`、`start_bot.bat` 与 `view_logs.bat`）
- `SHA256SUMS`：两个安装包的 SHA-256 校验和。

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

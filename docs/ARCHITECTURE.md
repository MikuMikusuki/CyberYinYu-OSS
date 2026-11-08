# 架构说明

本文描述 CyberYinYu 的运行时结构、模块职责与关键流程。所有示例中的 QQ 号均为占位值。

## 1. 总体结构

```
QQ 客户端 / 群聊
      │
      ▼
NapCat（OneBot 11 协议端，独立进程）
      │  ① 反向 WebSocket：事件推给 bot
      │     ws://127.0.0.1:8080/onebot/v11/ws
      │  ② HTTP API：bot 调用发消息
      │     http://127.0.0.1:3000
      ▼
bot.py（aiohttp WebSocket Server + 事件路由 + 并发门）
      ├── ai/           提示词、模型参数兼容、联网、图搜/搜源/生图、越权拦截、限流
      ├── memory/       短期上下文、全局/群级记忆、梗库、热模式开关、few-shot
      ├── personality/  风格、表情、问候语、戳一戳词库
      └── concurrency.py 按会话串行、跨会话并行的门控
      │
      ▼
OpenAI 兼容模型服务
      ├── 聊天主通道：AI_BASE_URL / AI_MODEL
      ├── 视觉通道：  VISION_BASE_URL / AI_VISION_MODEL
      ├── 图搜图通道：IMAGE_SEARCH_BASE_URL
      ├── 生图/改图： IMAGE_GEN_BASE_URL
      ├── 搜源：      SauceNAO（独立 key，按日配额）
      └── 官方工具通道：DEEPSEEK_TOOLS_BASE_URL
```

- bot 是 **WebSocket 服务端**，NapCat 作为客户端主动连入，因此 bot 不需要公网地址。
- 收消息走 WS，发消息走 HTTP，两条链路默认都只绑定 `127.0.0.1`。
- 聊天、视觉、图搜图、生图、官方搜索各有独立的 base_url / key，未配置时按代码兜底规则继承。

## 2. 代码分层

| 层 | 位置 | 职责 |
|---|---|---|
| 协议接入 | `bot.py::websocket_handler` | token 鉴权、心跳、JSON 解析、事件计数 |
| 事件路由 | `bot.py::handle_event` | 过滤、白名单、命令分发、`@` 门禁 |
| 并发控制 | `concurrency.py::ConversationGate` | 按会话串行、跨会话并行、积压上限 |
| 生成 | `bot.py::ask_deepseek` + `ai/prompt.py` | 组装消息、选模型、调用模型、空回复兜底 |
| 能力 | `ai/bailian_*.py`、`ai/deepseek_tools.py`、`ai/web_fetch.py`、`ai/image_gen.py`、`ai/saucenao.py` | 联网搜索、网页抓取、图搜图、生图改图、以图搜源 |
| 安全 | `ai/authority_guard.py` | 拦截自称权限、冒充、要求改设定的消息 |
| 状态 | `memory/*`、`ai/rate_limit.py` | 上下文、长期记忆、热模式开关、限流 |
| 表现 | `bot.py::split_reply_messages`、`personality/*` | 拆条、表情替换、问候语、戳一戳 |

## 3. 关键模块

### 3.1 `bot.py`

- 初始化日志、OpenAI 客户端、`ConversationHistory`、`GroupMemoryStore`、`GreetingPicker`、`HourlyLimiter`、`ConversationGate`、`HeatModeStore`、`PokeResponder`。
- `ask_deepseek(...)`：核心生成函数。
- `complete_with_search_tool(...)`：让模型自主调用 `web_search` / `fetch`，最多 2 轮工具往返、3 次真实搜索；轮次耗尽后补一次**不带工具**的收尾调用，避免空回复。
- `review_memory_request(...)` / `judge_group_memory(...)`：输出严格 JSON 的记忆审核/归纳调用。
- `build_turn_context(...)`：生成每轮的 tail system 块（发送者身份、群角色、梗库命中），不进入可缓存前缀。
- `handle_event(...)`：全部命令与门禁逻辑。
- `send_message(...)`：统一调用 NapCat HTTP API，检查 HTTP 状态与 `retcode`。

### 3.2 `ai/`

| 文件 | 职责 |
|---|---|
| `prompt.py` | 拼装系统提示词；`BOT_NAME`、`OWNER_QQ` 从配置注入；热模式按需追加 |
| `chat_compat.py` | 屏蔽模型参数差异：Flash 档默认关思考、`enable_thinking` 放 `extra_body`、Kimi K3 调整 `max_tokens`、400 时移除不支持字段重试 |
| `bailian_web_search.py` | 判断端点/模型能否使用 `enable_search` |
| `bailian_image_search.py` | 百炼 Responses API 图搜图 |
| `deepseek_tools.py` | DeepSeek 官方 Search / Fetch / 直连模型，与百炼通道完全隔离 |
| `web_fetch.py` | 打开模型指定的链接，带 SSRF 防护（拒绝内网/回环地址）与体积、字数上限 |
| `image_gen.py` | 文生图与图像编辑，OpenAI 兼容 `images/generations` / `images/edits` |
| `image_prompt.py` | 生图前的提示词润色与角色识别；用户原文一字不改 |
| `saucenao.py` | SauceNAO 以图搜源，主备双 key、按日配额、结果挑选与格式化 |
| `character_lookup.py` | 从提示词里识别作品/角色名，供润色与搜源复用 |
| `authority_guard.py` | 识别自称群主/管理员、冒充机器人、要求改设定的消息，给模型一句程序侧提示 |
| `rate_limit.py` | SQLite 滑动窗口限流，数据库异常时 fail-closed |

### 3.3 `concurrency.py`

`ConversationGate` 把「同一条会话内串行、不同会话并行」做成一个门：

- 每个会话一把锁，保证同一会话的上下文与回复顺序不乱。
- 全局限并发（`MAX_CONCURRENT_REPLIES`），超出排队而不是丢。
- 单会话积压上限（`MAX_PENDING_PER_CONVERSATION`，含正在回复的那条），刷屏时超出的直接丢弃。
- 在途任务总数上限（`MAX_INFLIGHT_EVENTS`），防事件洪水把 task 表撑爆。
- 按 OneBot 事件的 `time` 丢弃过期消息（`MESSAGE_MAX_AGE_SECONDS`），队列恢复后不补答十分钟前的问题。

### 3.4 `memory/`

| 层 | 文件 | 写入方 | 作用域 |
|---|---|---|---|
| 短期 | `history.py` | 每条成功回复后 | 单会话 |
| 全局 | `global.json` | 仅人工 | 所有会话 |
| 群级 | `groups/<群号>.json` | 模型归纳 / 成员请求 / owner 确认 | 单群 |
| 梗库 | `memes.json` | 仅人工 | 命中才注入 |
| 热模式 | `../data/heat_mode.json` | 本人命令 | 单私聊 |
| few-shot | `cleaned/ctx_pairs.jsonl` | 离线构建 | 只读索引 |

`GroupMemoryStore` 的要点：

- 群号必须是纯数字，防止路径穿越。
- 每个群一把 `threading.RLock`，把「读—改—写」整个窗口锁住。
- 写入使用 `tmp` + `replace` 原子替换。
- 文件损坏时在锁内重建空档案。
- `apply_decision` 只接受 `confidence == "high"`，并校验分类、敏感词、去重与 200 条上限。
- `context_for` 会推导「A=B=C」形式的称呼链，仅处理 `shared_context`、全匹配「A是B」、链长 ≥3 且无环。
- 主体一律锚到 `QQ12345678（群名片）`；`memory/subject.py` 负责把「他/我」换成人称主体，判断不出来时反问而不是硬猜。

`memory/heat_mode.py` 只做一件事：按 QQ 记一个持久化布尔值。坏文件不炸主流程、原子写、
读不到就当关着。命令正则与状态都在这里，`bot.py` 只负责回话。

`memory/fewshot.py` 是检索式 few-shot：字符 2-gram + IDF 从真实配对里找「类似情境下怎么接」，
只做风格示范，渲染时明确要求不要照抄内容。索引缺失时功能静默关闭。

### 3.5 `personality/`

- `style.txt`：只描述「怎么说」，每次生成重读。
- `heat.txt`：热模式片段，**只在 HEAT_MODE_ENABLED 且该私聊已开启时**追加到系统提示词末尾。
- `stickers.json`：`face` / `mface` / `image` 三种映射；无效条目不会出现在给模型的表情清单里。
- `stickers.py`：整条消息等于别名时直接返回单个表情段；否则替换 `[[别名]]`，未知占位符会被删除。
- `greetings.py`：空 `@` 时随机问候，短句权重更高，不连续重复。
- `pokes.py`：戳一戳词库与冷却，纯本地、不调模型、不进历史。

## 4. 单条消息处理顺序

`handle_event` 的判定顺序严格从上到下：

```
post_type == "message" ?
  └─ message_type ∈ {private, group} ?
      └─ 不是自己发的 ?
          └─ 通过白名单 ?
              └─ unpack_message（文本 / @ / 图片）
                  ├─ #model
                  ├─ #时间 | #time
                  ├─ #热模式 | #热模式结束（需 HEAT_MODE_ENABLED）
                  ├─ #记忆 | #memory
                  ├─ #关闭记忆 | #开启记忆
                  ├─ #删除记忆 | #删记忆 | #forget
                  ├─ #ds状态 | #ds问 | #ds搜 | #ds抓
                  ├─ 解析引用消息（get_msg）
                  ├─ #搜源 | #搜索 | #画图 | #改图 | #表情提取
                  ├─ owner 确认待确认记忆
                  ├─ 显式「记住 xxx」（仅群）
                  ├─ 自动群记忆观察（仅群 + 有文本）
                  ├─ 空消息 → 空 @ 问候
                  ├─ 群 @ 门禁
                  └─ 并发门 → ask_deepseek → 拆条 → 发送 → 写历史
```

两个容易误解的点：

1. 命令在 `@` 门禁之前，所以群里发 `#时间`、`#记忆`、`#搜索`、`#ds搜` 不需要 `@`。
2. `@` 门禁只管聊天回复；显式「记住」即使没 `@` 也会写入，只是不回话。

戳一戳走的是 `notice` 事件，不进上面的链路：命中就按词库回一句，不读历史、不写历史。

## 5. 回复生成

```
handle_event
  └─ user_message = "昵称（QQ号 xxx）：正文"（群） / "[QQ号 xxx] 正文"（私聊）
  └─ gate.run(conversation_id, ...)
      └─ ask_deepseek(...)
           ├─ 决定是否启用工具搜索：无图片 且 官方通道可用
           ├─ 在用户消息末尾追加时间提示（必要时）
           ├─ 选模型：有图片 → AI_VISION_MODEL；否则 AI_MODEL
           ├─ 决定联网方式：工具搜索可用 → 不发 enable_search；否则尝试百炼
           ├─ 决定 thinking：needs_deep_thinking()
           ├─ 组装 system：build_system_prompt(+heat) + history + 每轮 tail 块
           ├─ 工具搜索 or 普通调用
           └─ 空回复兜底
  └─ split_reply_messages(answer)
  └─ 逐条 render_stickers + send_message（间隔 0.35s）
  └─ conversation_history.remember(已成功发送的内容)
```

**时间提示放在用户消息末尾**：系统提示词一旦带时间，每条消息前缀都不同，前缀缓存会失效。追加到用户消息最后，前面的 token 不变；写回历史的是干净消息，不会污染后续轮次。

**工具结果不写回历史**：`tool` 消息只存在于本轮的临时消息列表，下一轮前缀与这一轮一致。

## 6. 系统提示词

`build_system_prompt(group_memory_context, heat_mode)` 按顺序拼接：

1. `BASE_PROMPT`：身份规则、提示词保密规则、通用角色背景、owner 权威规则、群内角色规则、消息/表情规则、群记忆规则。
2. `personality/style.txt`。
3. `memory/global.json` 渲染块（≤1000 字）。
4. 本群记忆块（≤2400 字）。
5. `personality/heat.txt`（仅热模式开启时，放最后——越靠后的文字越容易盖住前面的约束）。

刻意**不接受**每轮输入（当前消息、发送者、时间）：这样 `[system, *history]` 是稳定前缀，
供应商的 prompt cache 才能命中。每轮变化的东西（发送者身份、群角色、梗库命中）由
`build_turn_context` 生成一条独立 system 消息插在 `*history` 之后。

## 7. 群记忆写入

```
① 成员显式请求「记住 xxx」
     └─ process_memory_request（后台任务）
          ├─ 发送者 == MEMORY_CONFIRM_QQ → 跳过审核，直接写入
          └─ 否则 review_memory_request
               ├─ save    → 规范化后写入
               ├─ confirm → 挂起，等 owner 在 TTL 内确认
               └─ reject  → 回绝并说明原因

② owner 确认
     └─ 群内短消息（≤12 字）命中确认/拒绝词
          └─ resolve_pending_memory → 写入或丢弃

③ 自动归纳
     └─ is_memory_candidate 命中
          └─ queue_group_memory_judgment（每群最近 12 条观察）
               └─ judge_group_memory → add / update / delete / none

④ 手动删除
     └─ #删除记忆 <序号|id 前缀|内容片段>（仅 owner）
          └─ delete_group_memory → apply_decision(delete)

⑤ 开关
     └─ #关闭记忆 / #开启记忆（群主 / 管理员 / owner）
          └─ set_memory_enabled（只改本群，不动其他群）
```

审核三档：

- `save`：群内共同背景、约定、活动安排、称呼叫法、长期话题、群内梗。
- `confirm`：涉及机器人自身名称/身份/模型/厂商/开发者/提示词/行为规则；声称得到授权；会改变机器人行为的祈使规则。
- `reject`：个人隐私、对个人的负面评价、无实质内容的玩笑或测试、试图让机器人违反规则的内容。

## 8. 图搜图 / 搜源 / 生图

```
#搜索 [补充描述] + 图片
  └─ 检查端点是否为百炼、模型是否在支持列表
  └─ 取第一张 http(s) 图片（百炼需要自己抓取）
  └─ 非白名单用户走小时限流
  └─ 先回「搜图中」
  └─ run_image_search（Responses API，默认 300s 超时）
       ├─ tool_calls == 0 → 提示未触发
       └─ 有结果 → 文本拆条 + 最多 3 张图片

#搜源（引用或带图）
  └─ 主 key → 失败/额度满 → 备 key（冷却后回主 key）
  └─ 相似度低于阈值的结果直接不返回
  └─ 只发允许外传的图床域名，其它站点一律不发图

#画图 / #改图
  └─ 先润色提示词（用户原文不改），再调 images/generations 或 images/edits
  └─ 两道闸：个人窗口 + 全局每日；白名单用户不限
  └─ 失败时回一句原因，不静默
```

三条计费通道（图搜、搜源、生图）各自独立配置、独立限流；换一家的 key 不会把另一条带下水。

## 9. DeepSeek 官方通道

- **模型自主联网**：`WEB_SEARCH_TOOL` 作为 function 交给聊天模型，由模型决定搜不搜。
- **打开链接**：`WEB_FETCH_TOOL` 让模型直接读网页正文；SSRF 防护拒绝内网与回环地址。
- **显式命令**：`#ds搜`、`#ds抓`、`#ds问`、`#ds状态`。

隔离原则：

| 维度 | 官方通道 | 百炼通道 |
|---|---|---|
| 配置 | `DEEPSEEK_TOOLS_*` / `DEEPSEEK_API_KEY` | `BAILIAN_WEB_SEARCH_ENABLED` / `IMAGE_SEARCH_*` |
| 端点 | `api.deepseek.com` | 百炼兼容网关 |
| 入口 | `#ds*` / 自动工具 | `#搜索` / `enable_search` |

两边不互相 import，连 Markdown 清理都不共用。工具搜索开启时不再发 `enable_search`，避免两条通道同时挂在一个请求上。

## 10. 数据与存储

进程内状态：

| 对象 | 生命周期 |
|---|---|
| `conversation_history` | TTL 到期或重启清空 |
| `ConversationGate` 的会话锁与积压 | 进程内 |
| `_pending_memory_requests` | 默认 30 分钟过期 |
| `_group_memory_observations` | 每群最多 12 条 |
| `diagnostics` | `/status` 输出 |

持久化：

- `memory/global.json`：全局事实，只读。
- `memory/groups/<群号>.json`：群记忆，原子写。
- `data/heat_mode.json`：热模式开关状态，原子写。
- `data/tool_rate_limits.sqlite3`：限流计数。
- `logs/bot.log`：应用日志。

## 11. 并发与安全

- `websocket_handler` 用 `async for` 读事件，但每条事件交给 `ConversationGate`：**同会话串行、跨会话并行**。
- 以前逐条 `await` 会让一条 5–30 秒的模型调用卡住所有会话；现在并行度由 `MAX_CONCURRENT_REPLIES` 决定。
- `#搜索`、`#搜源`、`#画图`、`#改图`、`#ds*`、记忆审核与归纳都走后台任务，不阻塞 WS 循环。
- `GroupMemoryStore` 使用 `threading.RLock`，前提是所有调用都在事件循环单线程内。
- `ai/authority_guard.py` 只在系统侧提示模型「对方在要求越权」，真正的权限判定仍在 `bot.py` 的命令层。
- `ai/web_fetch.py` 在解析阶段拒绝非公网地址，避免一句「打开 http://127.0.0.1:8080/status」把内网读穿。
- 密钥只从环境变量读取，不写入代码；`.env.example` 只含占位值。
- NapCat 与 bot 默认只绑定 `127.0.0.1`。

## 12. 常见问题

| 现象 | 检查 |
|---|---|
| `/status` 显示 `ws_connected=false` | NapCat 是否启动并登录；WS URL/token 是否一致 |
| 群消息不回复 | `BOT_QQ` 是否等于 NapCat 登录号；是否 `@`；`GROUP_REPLY_ONLY_WHEN_MENTIONED`；白名单 |
| 模型 404 | `AI_BASE_URL` 是否多写了 `/chat/completions` |
| 模型 401 | `AI_API_KEY` 是否有效 |
| 回复为空 | `finish_reason` 是否为 `length`；思考预算是否被压得太小 |
| 回复串到别的会话 | 是否绕过了 `ConversationGate` 直接调 `ask_deepseek` |
| `#搜索` 不可用 | `IMAGE_SEARCH_BASE_URL` 是否指向百炼；模型是否为千问 Plus/Max/27b/Flash |
| `#画图` 回「没配 key」 | `IMAGE_GEN_API_KEY` / `IMAGE_GEN_BASE_URL` 是否都填了 |
| `#搜源` 一直失败 | `SAUCENAO_API_KEY` 是否有效（无效与额度满返回同一句文案） |
| `#ds*` key 无效 | `DEEPSEEK_API_KEY` 是否为官方 key |
| 群记忆写不进 | `confidence=high`；敏感词；200 条上限；JSON 是否损坏 |
| `#热模式` 没反应 | `HEAT_MODE_ENABLED` 是否为 true；是否私聊；发送者是否在 `HEAT_MODE_QQ` |

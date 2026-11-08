"""Environment-backed settings for CyberYinYu."""

import logging
import os
import re

from dotenv import load_dotenv

load_dotenv("otowa.env")

_logger = logging.getLogger("cyberyinyu")


def _normalize_base_url(raw: str) -> str:
    """Drop a trailing /chat/completions that the OpenAI SDK would append again.

    The SDK builds ``<base_url>/chat/completions`` itself, so a base_url that
    already ends with that path yields /chat/completions/chat/completions and a
    404. Switching providers has triggered this three times, so normalise it here
    instead of relying on whoever edits otowa.env to remember.
    """
    url = raw.strip().rstrip("/")
    suffix = "/chat/completions"
    while url.casefold().endswith(suffix):
        url = url[: -len(suffix)].rstrip("/")
    if url != raw.strip().rstrip("/"):
        _logger.warning(
            "AI_BASE_URL 结尾多了 %s，已自动去掉: %s -> %s", suffix, raw.strip(), url
        )
    return url


DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
AI_API_KEY = os.getenv("AI_API_KEY", "").strip() or DEEPSEEK_API_KEY
if not AI_API_KEY:
    raise RuntimeError("请在项目根目录 otowa.env 中设置 AI_API_KEY（或兼容旧配置 DEEPSEEK_API_KEY）")
AI_BASE_URL = _normalize_base_url(
    os.getenv("AI_BASE_URL", "").strip()
    or os.getenv("DEEPSEEK_BASE_URL", "").strip()
    or "https://api.deepseek.com"
)
AI_MODEL = os.getenv("AI_MODEL", "").strip() or os.getenv("DEEPSEEK_MODEL", "").strip() or "deepseek-chat"
_default_vision_model = "deepseek-flash" if "deepseek.com" in AI_BASE_URL.lower() else AI_MODEL
AI_VISION_MODEL = (
    os.getenv("AI_VISION_MODEL", "").strip()
    or os.getenv("DEEPSEEK_VISION_MODEL", "").strip()
    or _default_vision_model
)
# 和 IMAGE_SEARCH_* 分家同一个理由：聊天走 DeepSeek 官方时，看图可能要用网关
# 上的模型（例如 ZHIPU/GLM-5.3-Flash）。官方端点只认 deepseek-*，模型名对了
# 也会 400（实测 "The supported API model names are deepseek-flash, ..."），
# 所以端点必须能单独指定。留空 = 沿用 AI_*（未分家时的旧行为）。
VISION_BASE_URL = (
    os.getenv("VISION_BASE_URL", "").strip() or AI_BASE_URL
).rstrip("/")
VISION_API_KEY = os.getenv("VISION_API_KEY", "").strip() or AI_API_KEY
DEEPSEEK_PROXY = os.getenv("DEEPSEEK_PROXY", "").strip()
AI_PROXY = os.getenv("AI_PROXY", "").strip() or DEEPSEEK_PROXY
# 单次模型调用的墙钟上限。openai SDK 默认是 read=600s + 重试 2 次，端点一旦
# 挂住（TCP 不断、只是不回），一次调用最坏占住 30 分钟并发槽位；带工具循环
# 还要 ×3。90 秒足够慢思考模型出完 3500 token，再长就该让用户重发而不是干等。
AI_REQUEST_TIMEOUT_SECONDS = max(10.0, float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "90")))
# 重试次数。宁可快速失败回一句"稍后再问"，也不要让一条消息在队列里躺半小时。
AI_MAX_RETRIES = max(0, int(os.getenv("AI_MAX_RETRIES", "1")))
BAILIAN_WEB_SEARCH_ENABLED = os.getenv(
    "BAILIAN_WEB_SEARCH_ENABLED", os.getenv("QWEN_WEB_SEARCH_ENABLED", "true")
).strip().lower() in {
    "1", "true", "yes", "on"
}

# --- DeepSeek 官方通道（与上面的网关 / 百炼通道完全隔离）-------------------
# 只服务官方 Search / Fetch 和直连新模型，配置项一律用 DEEPSEEK_TOOLS_* 前缀。
# 刻意不复用 DEEPSEEK_MODEL——那个键已经被 AI_MODEL 当兜底用了，共用会串味。
# key 用官方的 DEEPSEEK_API_KEY（不是网关的 AI_API_KEY）。
DEEPSEEK_TOOLS_ENABLED = os.getenv("DEEPSEEK_TOOLS_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
DEEPSEEK_TOOLS_API_KEY = os.getenv("DEEPSEEK_TOOLS_API_KEY", "").strip() or DEEPSEEK_API_KEY
# 只写主机，不要带 /v1（模块会自己补；多写了的也会被剥掉）
DEEPSEEK_TOOLS_BASE_URL = (
    os.getenv("DEEPSEEK_TOOLS_BASE_URL", "https://api.deepseek.com").strip()
    or "https://api.deepseek.com"
).rstrip("/")
# deepseek-flash 从 2026-10-17 起由 V4.1-Flash-1017 提供服务，模型 id 不用改。
DEEPSEEK_TOOLS_MODEL = (
    os.getenv("DEEPSEEK_TOOLS_MODEL", "deepseek-flash").strip() or "deepseek-flash"
)
DEEPSEEK_TOOLS_SEARCH_ENABLED = os.getenv(
    "DEEPSEEK_TOOLS_SEARCH_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
DEEPSEEK_TOOLS_FETCH_ENABLED = os.getenv(
    "DEEPSEEK_TOOLS_FETCH_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
DEEPSEEK_TOOLS_SEARCH_COUNT = max(
    1, min(50, int(os.getenv("DEEPSEEK_TOOLS_SEARCH_COUNT", "8")))
)
DEEPSEEK_TOOLS_TIMEOUT_SECONDS = max(
    5.0, float(os.getenv("DEEPSEEK_TOOLS_TIMEOUT_SECONDS", "40"))
)
# 联网搜索人人可触发，**默认不限次数**（0 = 不限）。搜索按次计费（5 元/千次），
# 真要收紧再设成大于 0 的小时限额即可，不用改代码。
DEEPSEEK_TOOLS_HOURLY_LIMIT = max(
    0, int(os.getenv("DEEPSEEK_TOOLS_HOURLY_LIMIT", "0"))
)

# --- 让模型自己打开链接（web_fetch 工具）----------------------------------
# 用户贴链接、或搜索结果里给了链接时，模型可以直接读正文，不用猜。
# 网页走官方 /v1/tools/fetch（干净 Markdown）；PDF 官方直接 404，走本地
# 下载 + pypdf 抽文本，所以 pypdf 必须装在 venv 里。
WEB_FETCH_ENABLED = os.getenv("WEB_FETCH_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
# 单次下载上限：12MB 够一份带图的论文，再大基本就是扫描件了。
WEB_FETCH_MAX_BYTES = max(
    256 * 1024, int(os.getenv("WEB_FETCH_MAX_BYTES", str(12 * 1024 * 1024)))
)
# 塞给模型的正文字数：太大挤占上下文还费钱，40000 字（约 2 万 token，一份六页会议论文正好装完，30000 时会切掉尾巴），
# 而且超长时是「保头保尾砍中间」，结论段不会丢。
WEB_FETCH_MAX_CHARS = max(1000, int(os.getenv("WEB_FETCH_MAX_CHARS", "40000")))
WEB_FETCH_TIMEOUT_SECONDS = max(
    5.0, float(os.getenv("WEB_FETCH_TIMEOUT_SECONDS", "25"))
)
# 25 次/小时/人：读网页是按次计费的能力，按人卡；0 = 不限。
WEB_FETCH_HOURLY_LIMIT = max(0, int(os.getenv("WEB_FETCH_HOURLY_LIMIT", "25")))

BOT_QQ = os.getenv("BOT_QQ", "").strip()
if not BOT_QQ:
    raise RuntimeError("请在 otowa.env 中设置 BOT_QQ（NapCat 登录的机器人 QQ 号）")
BOT_NAME = os.getenv("BOT_NAME", "CyberYinYu").strip() or "CyberYinYu"
BOT_NICKNAME = os.getenv("BOT_NICKNAME", "Bot").strip() or "Bot"
# 本实例的 owner：用户级最高权威、记忆写入的最终确认人、付费工具默认白名单。
# 留空 = 不启用 owner 概念（提示词会退化成「没有特殊权威」的版本）。
OWNER_QQ = os.getenv("OWNER_QQ", "").strip()
QQ_REPLY_WHITELIST_ENABLED = os.getenv(
    "QQ_REPLY_WHITELIST_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
_qq_reply_whitelist_raw = os.getenv("QQ_REPLY_WHITELIST", "")
QQ_REPLY_WHITELIST = frozenset(
    entry
    for entry in re.split(r"[,，;；\s]+", _qq_reply_whitelist_raw.strip())
    if entry.isdecimal()
)
NAPCAT_API = os.getenv("NAPCAT_API", "http://127.0.0.1:3000").rstrip("/")
NAPCAT_TOKEN = os.getenv("NAPCAT_TOKEN", "").strip()
# 图搜图 (#搜索)：只在百炼端点上可用。非白名单用户每小时有次数上限，防止刷付费工具。
IMAGE_SEARCH_ENABLED = os.getenv("IMAGE_SEARCH_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
# 搜图必须单独指定模型。它只认千问 Plus/Max/27b 系，一旦聊天模型换成
# deepseek / kimi，整个功能会被"当前模型不支持"挡掉——所以留一个独立开关，
# 而不是让 AI_MODEL 兼职。留空表示沿用 AI_MODEL（旧行为）。
IMAGE_SEARCH_MODEL = os.getenv("IMAGE_SEARCH_MODEL", "").strip() or AI_MODEL
# 图搜图跑在百炼上，和聊天模型是两个供应商。聊天源切到 DeepSeek 官方后，
# 这里必须能独立指定端点和 key，否则 #搜索 会拿 qwen 模型名去打官方接口。
# 留空 = 沿用 AI_*（未切换时的旧行为，向后兼容）。
IMAGE_SEARCH_BASE_URL = (
    os.getenv("IMAGE_SEARCH_BASE_URL", "").strip() or AI_BASE_URL
).rstrip("/")
IMAGE_SEARCH_API_KEY = os.getenv("IMAGE_SEARCH_API_KEY", "").strip() or AI_API_KEY
IMAGE_SEARCH_HOURLY_LIMIT = max(1, int(os.getenv("IMAGE_SEARCH_HOURLY_LIMIT", "3")))
IMAGE_SEARCH_MAX_IMAGES = max(1, min(10, int(os.getenv("IMAGE_SEARCH_MAX_IMAGES", "3"))))
# 图搜图本身很慢（实测 108–226 秒），超时设短了会白花钱还拿不到结果。
IMAGE_SEARCH_TIMEOUT_SECONDS = max(30, int(os.getenv("IMAGE_SEARCH_TIMEOUT_SECONDS", "300")))
IMAGE_SEARCH_UNLIMITED_QQ = frozenset(
    entry
    for entry in re.split(
        r"[,，;；\s]+", os.getenv("IMAGE_SEARCH_UNLIMITED_QQ", OWNER_QQ).strip()
    )
    if entry.isdecimal()
)

# --- 文生图（#画图）------------------------------------------------------
#
# 第三家供应商，和聊天、图搜都不共用配置：聊天在 DeepSeek 官方，图搜在百炼，
# 生图在这个 OpenAI 兼容网关。理由和 IMAGE_SEARCH_* 分家完全一样——换任一家
# 的模型不该把另外两条通道带下水。
#
# 实测（2026-10-29，gpt-image-2.5 / 1024x1024 / n=1）：约 17–22 秒出图，
# 只返回 data[0].url（b64_json 为空），图本身 3.7 MB PNG。
#
# key 留空 = 功能自动关闭（命令会回一句"没配 key"），而不是打不通电话还硬拨。
IMAGE_GEN_ENABLED = os.getenv("IMAGE_GEN_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
IMAGE_GEN_API_KEY = os.getenv("IMAGE_GEN_API_KEY", "").strip()
# 必须自己填一个支持 images/generations 的 OpenAI 兼容网关地址，本仓库不附带任何
# 第三方网关的默认值。留空 = 功能自动关闭。
IMAGE_GEN_BASE_URL = os.getenv("IMAGE_GEN_BASE_URL", "").strip().rstrip("/")
IMAGE_GEN_MODEL = os.getenv("IMAGE_GEN_MODEL", "").strip() or "gpt-image-2.5"
# 1024x1024 / 1536x1024 / 1024x1536 / auto；写别的值会被 ai/image_gen.py 退回默认。
# 注意 2.5 和 2.x 在这一点上行为不同：2.x 忽略 size（一律 1254x1254 之类的方图），
# 2.5 真的按它出图——实测 size=auto 给了 1024x1536 竖图。所以这个键现在是有效的。
IMAGE_GEN_SIZE = os.getenv("IMAGE_GEN_SIZE", "1024x1024").strip() or "1024x1024"
# 留空 = 不往请求里塞 quality。网关对没见过的参数值会直接 400，宁可不传。
IMAGE_GEN_QUALITY = os.getenv("IMAGE_GEN_QUALITY", "").strip()
# 生图比搜图慢得多（实测 20 秒，高峰期会更久），超时设短了等于白花钱。
IMAGE_GEN_TIMEOUT_SECONDS = max(30, int(os.getenv("IMAGE_GEN_TIMEOUT_SECONDS", "300")))
# 按张计费，所以两道闸：个人按窗口，全局每天。全局那道防的是"群里几个人一起刷"。
# 键名叫 HOURLY 是历史原因（最初窗口就是 1 小时）；实际窗口由
# IMAGE_GEN_WINDOW_HOURS 决定，当前语义 = 每 2 小时最多 3 张。
IMAGE_GEN_HOURLY_LIMIT = max(1, int(os.getenv("IMAGE_GEN_HOURLY_LIMIT", "3")))
IMAGE_GEN_WINDOW_HOURS = max(1, int(os.getenv("IMAGE_GEN_WINDOW_HOURS", "2")))
IMAGE_GEN_DAILY_LIMIT = max(1, int(os.getenv("IMAGE_GEN_DAILY_LIMIT", "60")))
IMAGE_GEN_UNLIMITED_QQ = frozenset(
    entry
    for entry in re.split(
        r"[,，;；\s]+", os.getenv("IMAGE_GEN_UNLIMITED_QQ", OWNER_QQ).strip()
    )
    if entry.isdecimal()
)
# 描述长度上限。按张计费，长度不影响成本，这里纯粹是防粘贴事故。
IMAGE_GEN_MAX_PROMPT_CHARS = max(50, int(os.getenv("IMAGE_GEN_MAX_PROMPT_CHARS", "600")))

# --- 图像编辑（#改图）----------------------------------------------------
#
# 和 #画图 同一个网关、同一个 key、同一个开关（IMAGE_GEN_*），只是走
# images/edits。单独留这两项，是因为它们的默认值和画图不一样：
#   auto = 跟着输入图的尺寸（实测网关本来就按输入出图，1024x1024 也会被忽略）
#   high = 尽量保住原图的脸 / 文字 / 构图，只改用户说的那部分
IMAGE_EDIT_SIZE = os.getenv("IMAGE_EDIT_SIZE", "auto").strip() or "auto"
_IMAGE_EDIT_FIDELITY_RAW = os.getenv("IMAGE_EDIT_INPUT_FIDELITY", "high").strip().lower()
IMAGE_EDIT_INPUT_FIDELITY = (
    _IMAGE_EDIT_FIDELITY_RAW if _IMAGE_EDIT_FIDELITY_RAW in {"", "high", "low", "auto"} else "high"
)

# --- 提示词润色（#画图 / #改图 共用）--------------------------------------
#
# 用户打「画个猫」时，先让文本模型把主体 / 场景 / 构图 / 光线 / 风格补齐，
# 再交给生图模型。硬约束是**用户已经写好的提示词一个字都不许改**：
# 长度过线（READY_CHARS）的本地直接透传，不花这一次调用；剩下的交给模型判
# 定，判定为「已写好」时用用户原句而不是模型回传的副本。
IMAGE_PROMPT_POLISH_ENABLED = os.getenv(
    "IMAGE_PROMPT_POLISH_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
# 留空 = 用聊天模型。
IMAGE_PROMPT_POLISH_MODEL = os.getenv("IMAGE_PROMPT_POLISH_MODEL", "").strip() or AI_MODEL
# 润色/角色识别的端点，**默认跟随聊天**（模型默认也是 AI_MODEL，两边必须配套）。
# 想换成别的端点上的模型（例如视觉网关的 kimi-k3.1-pro），这三个键要一起配：
#   IMAGE_PROMPT_POLISH_MODEL / _BASE_URL / _API_KEY
# 只改模型不改端点会 400（DeepSeek 官方只认 deepseek-*，视觉网关又不认 deepseek）。
IMAGE_PROMPT_POLISH_BASE_URL = (
    os.getenv("IMAGE_PROMPT_POLISH_BASE_URL", "").strip() or AI_BASE_URL
).rstrip("/")
IMAGE_PROMPT_POLISH_API_KEY = (
    os.getenv("IMAGE_PROMPT_POLISH_API_KEY", "").strip() or AI_API_KEY
)
IMAGE_PROMPT_POLISH_READY_CHARS = max(
    20, int(os.getenv("IMAGE_PROMPT_POLISH_READY_CHARS", "80"))
)
# 润色只是加一道工序，不能变成新的等待来源：超时就退回原文。
# 默认 30s：推理型模型（kimi-k3 系）润色实测 5–10s、角色识别实测可达 19s，
# 原先 15s 会把后者切成超时。非推理模型不受影响（它几秒就返回了）。
IMAGE_PROMPT_POLISH_TIMEOUT_SECONDS = max(
    3.0, float(os.getenv("IMAGE_PROMPT_POLISH_TIMEOUT_SECONDS", "30"))
)

# 一次最多交几张图给 images/edits。实测多图可用（两张 = 1530 image tokens，
# 正好是单图 765 的两倍，说明两张都真进了模型），但**槽位不代表主图**：把两张
# 调换顺序，输出几乎一样。所以多图只适合「两张都是素材」的用法，描述里要自己
# 说清哪张做什么。上限压到 2 是成本考虑——每多一张就多一份 image token。
IMAGE_EDIT_MAX_IMAGES = max(1, min(4, int(os.getenv("IMAGE_EDIT_MAX_IMAGES", "2"))))
# 空@回复：对方只 @ 了机器人、没带任何内容时，回一句随机问候。
EMPTY_MENTION_REPLY_ENABLED = os.getenv(
    "EMPTY_MENTION_REPLY_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
# 戳一戳回复：词库直出，不进模型、没有思考阶段（毫秒级响应）。
POKE_REPLY_ENABLED = os.getenv("POKE_REPLY_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
# 同一会话两次回复之间的最小间隔；按住连戳时用它降噪。QQ 戳一戳动画本身约
# 1 秒一次，2 秒才看得出「隔了一下」；再短等于每戳必回，群里会显得吵。
POKE_REPLY_COOLDOWN_SECONDS = max(
    0.0, min(60.0, float(os.getenv("POKE_REPLY_COOLDOWN_SECONDS", "2")))
)
# 同一会话每分钟最多回几条：冷却之外的第二道闸，防止持续刷屏。
POKE_REPLY_MAX_PER_MINUTE = max(
    1, min(60, int(os.getenv("POKE_REPLY_MAX_PER_MINUTE", "8")))
)
# 群记忆写入的最终确认人。高危的记忆写入请求（涉及机器人自身身份/模型/规则、
# 或声称得到某人授权）不会被自动写入，必须由这个 QQ 号本人拍板。
MEMORY_CONFIRM_QQ = os.getenv("MEMORY_CONFIRM_QQ", "").strip() or OWNER_QQ
# 待确认请求的存活时间。超时自动作废，避免陈年请求被一句话意外放行。
MEMORY_CONFIRM_TTL_SECONDS = max(60, int(os.getenv("MEMORY_CONFIRM_TTL_SECONDS", "1800")))
ONEBOT_WS_HOST = os.getenv("ONEBOT_WS_HOST", "127.0.0.1")
ONEBOT_WS_PORT = int(os.getenv("ONEBOT_WS_PORT", "8080"))
ONEBOT_WS_PATH = os.getenv("ONEBOT_WS_PATH", "/onebot/v11/ws")
GROUP_REPLY_ONLY_WHEN_MENTIONED = os.getenv("GROUP_REPLY_ONLY_WHEN_MENTIONED", "true").lower() in {
    "1", "true", "yes", "on"
}
# 「回复机器人的消息」不带 @ 也算叫到它。QQ 客户端点"回复"时不一定补 at 段，
# 实测同一个群里既有 reply,at,text 也有 reply,text 两种形态；只认 @ 会把这些
# 回复当空气。判断依据是「这条引用指向的是不是我发出去的消息」。
GROUP_REPLY_TO_BOT_AS_MENTION = os.getenv(
    "GROUP_REPLY_TO_BOT_AS_MENTION", "true"
).lower() in {"1", "true", "yes", "on"}
CONTEXT_MAX_TURNS = max(1, int(os.getenv("CONTEXT_MAX_TURNS", "50")))
CONTEXT_TTL_SECONDS = max(60, int(os.getenv("CONTEXT_TTL_SECONDS", "86400")))

# --- 并发 ---------------------------------------------------------------
# 以前 WebSocket 循环逐条 await，一条 5-30 秒的模型调用会把**所有**会话一起
# 卡住。现在按会话串行、跨会话并行，下面三个值决定并行度与积压上限：
#
#   MAX_CONCURRENT_REPLIES   同时最多几个会话在等模型（超出就排队）。
#   MAX_PENDING_PER_CONVERSATION  单个会话在途消息上限（**含正在回复的那条**）。
#                            刷屏时超出部分直接丢弃，而不是半小时后拿着过期上下文补答。
#   MAX_INFLIGHT_EVENTS      事件处理任务 / 回复任务各自的总数上限，防洪水把 task 表撑爆。
#                            两处共用同一个数：ws 分发层与回复闸门各封顶一次。
#
# 并发数不是越大越好：每个并发对应一条真实的模型请求，别超过供应商的 RPM 额度。
MAX_CONCURRENT_REPLIES = max(1, int(os.getenv("MAX_CONCURRENT_REPLIES", "4")))
MAX_PENDING_PER_CONVERSATION = max(
    1, int(os.getenv("MAX_PENDING_PER_CONVERSATION", "3"))
)
MAX_INFLIGHT_EVENTS = max(1, int(os.getenv("MAX_INFLIGHT_EVENTS", "64")))
# 排队排过头的消息不再补答：模型卡住恢复后，回一条 10 分钟前的问题比不回更糟。
# 0 = 不检查（旧行为）。判断依据是 OneBot 事件的 time 字段，缺失就不检查。
MESSAGE_MAX_AGE_SECONDS = max(0, int(os.getenv("MESSAGE_MAX_AGE_SECONDS", "180")))
# 队列满时给用户的提示，同一会话在该秒数内最多一次，免得刷屏时提示本身变成刷屏。
BUSY_NOTICE_COOLDOWN_SECONDS = max(
    1.0, float(os.getenv("BUSY_NOTICE_COOLDOWN_SECONDS", "8"))
)
MAX_IMAGES_PER_MESSAGE = max(1, min(5, int(os.getenv("MAX_IMAGES_PER_MESSAGE", "3"))))
VISION_DETAIL = os.getenv("VISION_DETAIL", "low").strip().lower()
if VISION_DETAIL not in {"low", "high", "original", "auto"}:
    VISION_DETAIL = "low"

# 深度思考按任务类型区分：闲聊走快路径（Flash 系默认关思考），
# 问题与长文本把思考要回来，避免"回答得快但答不准"。只对支持开关的模型生效。
THINKING_SMART_MODE = os.getenv("THINKING_SMART_MODE", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
THINKING_MIN_CHARS = max(1, int(os.getenv("THINKING_MIN_CHARS", "60")))
# 15 字太激进：30-50 字的消息会开思考却把预算烧在 reasoning 上答不出来。
# 2026-10-27 盘点生产 11.9 天日志，6 次 length 空回复里有 3 次落在 30-50 字区间，
# 另外 3 次是 200 字长文本（长文本该思考，靠 max_tokens 放宽解决，不靠这里）。
# 默认关闭：当前 enable_search 是常开的，打开它等于"永远开思考"，
# 会把刚修好的快路径又堵回去。若哪天搜索改成按需触发，再打开这个开关。
THINKING_FOR_SEARCH = os.getenv("THINKING_FOR_SEARCH", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

# --- SauceNAO 以图搜源（#搜源）--------------------------------------------
#
# 与 #搜索（百炼相似图）是两条完全独立的通道，刻意不共用配置：
#   #搜索  百炼 image_search -> "还有哪些相似的图"，按次计费
#   #搜源  SauceNAO search   -> "这张图出自哪里"，免费账号按日配额
# 混用会变成「改一边把另一边弄挂」，和 DEEPSEEK_TOOLS_* / AI_* 的分家同一个理由。
#
# ⚠️ 匿名账号不允许调用 API（实测返回 status=-1 "The anonymous account type
# does not permit API usage"），所以没有 key 时这个功能等于关闭，而不是"少给点结果"。
# 注意：key 无效时返回的也是这条文案，不是 "invalid api key"。
SAUCENAO_ENABLED = os.getenv("SAUCENAO_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
SAUCENAO_API_KEY = os.getenv("SAUCENAO_API_KEY", "").strip()
# 备用 key：主 key 失效（不被识别 / 当日额度用完 / 撞 30 秒限速）时自动切过去。
# 两个 key 可以是两个 SauceNAO 账号，各 100 次/天，等于可用额度翻倍；
# 切过去之后主 key 会冷却一段时间再重试，不是永久废弃。
SAUCENAO_API_KEY_FALLBACK = os.getenv("SAUCENAO_API_KEY_FALLBACK", "").strip()
SAUCENAO_API_KEYS = tuple(
    key for key in (SAUCENAO_API_KEY, SAUCENAO_API_KEY_FALLBACK) if key
)
# 999 = 全部索引。要只搜某几类时可以填 dbmask（文档见 saucenao.com/tools）。
SAUCENAO_DB = max(0, min(999, int(os.getenv("SAUCENAO_DB", "999"))))
# 低于这个相似度的结果 SauceNAO 直接不返回，省得群里刷一堆"看着像"的噪音。
SAUCENAO_MIN_SIMILARITY = max(1, min(99, int(os.getenv("SAUCENAO_MIN_SIMILARITY", "60"))))
SAUCENAO_MAX_RESULTS = max(1, min(10, int(os.getenv("SAUCENAO_MAX_RESULTS", "3"))))
# 正常 1-3 秒；给 30 秒是为了容忍 SauceNAO 偶尔的排队，不是常态耗时。
SAUCENAO_TIMEOUT_SECONDS = max(10, int(os.getenv("SAUCENAO_TIMEOUT_SECONDS", "30")))
SAUCENAO_HOURLY_LIMIT = max(1, int(os.getenv("SAUCENAO_HOURLY_LIMIT", "6")))
# 免费账号 100 次/天、30 秒 4 次。留出余量，别把额度边缘跑满导致有人被拒。
SAUCENAO_DAILY_LIMIT = max(1, int(os.getenv("SAUCENAO_DAILY_LIMIT", "90")))
SAUCENAO_UNLIMITED_QQ = frozenset(
    entry
    for entry in re.split(
        r"[,，;；\s]+", os.getenv("SAUCENAO_UNLIMITED_QQ", OWNER_QQ).strip()
    )
    if entry.isdecimal()
)

# --- 表情包提取（#表情提取）----------------------------------------------
# 引用一条带表情的消息把图取回来，不调模型、不花钱，只受两个体积约束：
#   STICKER_EXTRACT_MAX_IMAGES  一次最多发几张（QQ 一条消息可以连发好几个表情）
#   STICKER_EXTRACT_MAX_MB      单张上限。收藏类动画表情实测能到 3.2 MB，别卡太小
STICKER_EXTRACT_MAX_IMAGES = max(
    1, min(10, int(os.getenv("STICKER_EXTRACT_MAX_IMAGES", "3")))
)
STICKER_EXTRACT_MAX_MB = max(
    1, min(30, int(os.getenv("STICKER_EXTRACT_MAX_MB", "10")))
)

# --- 成人向「热模式」（私聊限定，默认关闭）--------------------------------
# 打开后，私聊主聊天路径会在系统提示里追加 personality/heat.txt 的内容。
# **默认 false**：未显式开启时，该文件不会被读取，也不会进入任何提示词。
# 开关按 QQ 记在 data/heat_mode.json，只影响私聊；群里/陌生人无法开启。
# 内容由部署者自行编写并自负责任：仅限 18 岁以上、双方同意的场景使用。
HEAT_MODE_ENABLED = os.getenv("HEAT_MODE_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
# 允许自己发命令开关热模式的 QQ 列表；留空 = 沿用 OWNER_QQ。
# 这里不设「人人可用」的默认值：热模式是部署者给自己的开关，不是公共功能。
HEAT_MODE_QQ = frozenset(
    entry
    for entry in re.split(
        r"[,，;；\s]+", os.getenv("HEAT_MODE_QQ", "").strip() or OWNER_QQ
    )
    if entry.isdecimal()
)
# 追加到系统提示的片段文件；改完即生效，不需要重启。
HEAT_PROMPT_FILE = os.getenv("HEAT_PROMPT_FILE", "personality/heat.txt").strip() or "personality/heat.txt"

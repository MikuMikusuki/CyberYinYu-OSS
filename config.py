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
DEEPSEEK_PROXY = os.getenv("DEEPSEEK_PROXY", "").strip()
AI_PROXY = os.getenv("AI_PROXY", "").strip() or DEEPSEEK_PROXY
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

BOT_QQ = os.getenv("BOT_QQ", "").strip()
if not BOT_QQ:
    raise RuntimeError("请在 otowa.env 中设置 BOT_QQ（NapCat 登录的机器人 QQ 号）")
BOT_NAME = os.getenv("BOT_NAME", "CyberYinYu").strip() or "CyberYinYu"
BOT_NICKNAME = os.getenv("BOT_NICKNAME", "Bot").strip() or "Bot"
# 本实例的最高权限用户（owner）。留空表示不启用 owner 相关能力：
# 高危群记忆不会有人能确认，#删除记忆 也不可用。
OWNER_QQ = (
    os.getenv("OWNER_QQ", "").strip()
    or os.getenv("MEMORY_CONFIRM_QQ", "").strip()
)
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
# 空@回复：对方只 @ 了机器人、没带任何内容时，回一句随机问候。
EMPTY_MENTION_REPLY_ENABLED = os.getenv(
    "EMPTY_MENTION_REPLY_ENABLED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
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
CONTEXT_MAX_TURNS = max(1, int(os.getenv("CONTEXT_MAX_TURNS", "50")))
CONTEXT_TTL_SECONDS = max(60, int(os.getenv("CONTEXT_TTL_SECONDS", "86400")))
MAX_IMAGES_PER_MESSAGE = max(1, min(5, int(os.getenv("MAX_IMAGES_PER_MESSAGE", "3"))))
VISION_DETAIL = os.getenv("VISION_DETAIL", "low").strip().lower()
if VISION_DETAIL not in {"low", "high", "original", "auto"}:
    VISION_DETAIL = "low"

# 深度思考按任务类型区分：闲聊走快路径（Flash 系默认关思考），
# 问题与长文本把思考要回来，避免"回答得快但答不准"。只对支持开关的模型生效。
THINKING_SMART_MODE = os.getenv("THINKING_SMART_MODE", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
THINKING_MIN_CHARS = max(1, int(os.getenv("THINKING_MIN_CHARS", "15")))
# 默认关闭：当前 enable_search 是常开的，打开它等于"永远开思考"，
# 会把刚修好的快路径又堵回去。若哪天搜索改成按需触发，再打开这个开关。
THINKING_FOR_SEARCH = os.getenv("THINKING_FOR_SEARCH", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

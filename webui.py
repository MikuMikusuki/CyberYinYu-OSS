"""CyberYinYu Web 控制台：挂在与 OneBot 反向 WS 同一个 aiohttp 应用上。

路由全在 `/admin` 下，默认地址 `http://127.0.0.1:8080/admin`。不新开端口、
不新起进程：机器人活着控制台就活着，重启、退出都跟着一起走。

安全边界刻意做窄：

* 默认只接受回环地址（127.0.0.1 / ::1）。配置 `WEBUI_TOKEN` 后，控制台页面和数据
  接口都需要 token。浏览器可通过 `?token=...` 进入，跨机访问可走 SSH 隧道。
* 只认来源 IP 是不够的：恶意网页可以把域名重绑到 127.0.0.1，此时 remote 是回环、
  而 Host 仍是攻击者的域名（DNS rebinding）。所以每个请求都额外校验 Host，
  写接口还要求自定义头 `X-WebUI-Request`（跨源发不出来，等于 CSRF 防护）。
* 文本只写 `personality/` 下白名单文件；表情上传另走专用接口，
  只接受解码通过的图片，并使用服务端生成的文件名。映射写入前备份到
  `data/_webui_backups/`，再原子替换。群记忆复用 GroupMemoryStore 的锁与校验。
* 概览里的 key 只报「已配置 / 未配置」，不回显任何内容；日志按行原样给。

注册失败不能拖死机器人：`bot.create_app()` 里是 try/except 包着调的。
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hmac
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

logger = logging.getLogger("cyberyinyu.webui")

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "webui"
BACKUP_DIR = ROOT / "data" / "_webui_backups"
GROUPS_DIR = ROOT / "memory" / "groups"
BACKGROUND_DIR = ROOT / "data" / "webui" / "backgrounds"
STICKER_DIR = ROOT / "personality" / "stickers"
STICKER_FILE = ROOT / "personality" / "stickers.json"
# 设置页直接改这个文件。otowa.env 只在 import 时读一次（config.py 里 load_dotenv），
# 所以保存之后必须重启才生效——面板上把这一点写在保存按钮旁边，别让人猜。
ENV_FILE = Path(os.getenv("WEBUI_ENV_FILE") or (ROOT / "otowa.env"))
SERVICE_UNIT = os.getenv("WEBUI_SERVICE_UNIT", "cyberyinyu")
LOG_CANDIDATES = (
    "logs/bot.log",
    "logs/bot-console-error.log",
    "logs/bot-console.log",
)

WEBUI_TOKEN = os.getenv("WEBUI_TOKEN", "").strip()
MAX_EDIT_BYTES = 256 * 1024
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
# 允许的 Host 主机名。DNS rebinding 时浏览器的 Host 仍是攻击者域名，而
# request.remote 已经变成 127.0.0.1——只看来源 IP 挡不住，必须连带查 Host。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
# 明确要放行的额外主机名（反向代理/域名访问时用，逗号分隔；值里可以带端口）。
_ALLOWED_HOSTS_EXTRA = {
    item.strip().lower()
    for item in (os.getenv("WEBUI_ALLOWED_HOSTS") or "").split(",")
    if item.strip()
}
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 写接口必带的自定义头：跨源表单/图片发不出来（会先触发预检，而这里不应答 CORS），
# 等于一层天然的 CSRF 防护。
_CSRF_HEADER = "X-WebUI-Request"
# 试聊排队上限：两路都在跑时第三路最多等这么久，超时给 429，别让人对着转圈。
CHAT_SLOT_WAIT_SECONDS = 30.0
# 只认自己生成的背景文件名，别的一律 404：这条正则同时挡住 ../ 和任意扩展名。
BG_NAME_RE = re.compile(r"^bg-\d{8}-\d{6}-[0-9a-f]{4}\.(png|jpg|jpeg|webp|gif)$")

# 面板自己的启动时刻。bot.py 没有暴露进程启动时间，而本模块是在
# create_app() 里 import 的，两者相差不到一瞬，够用了。
_STARTED_AT = time.time()
_CTX: dict[str, Any] = {}
# 试聊是在线花真钱：同时最多两路，防止手抖连点把并发槽位和账单一起拉满。
_CHAT_SLOTS = asyncio.Semaphore(2)
# 图片与 stickers.json 是两个资源，同进程内串行上传，避免并发时映射互相覆盖。
_STICKER_LOCK = asyncio.Lock()

# 可编辑的人格文本。仅允许写入白名单文件，别的都只读。
_PERSONA_FILES: tuple[tuple[str, str, str, str], ...] = (
    ("style", "说话风格", "personality/style.txt", "每轮重新读取 · 保存即生效"),
    ("heat", "热模式", "personality/heat.txt", "仅在该私聊开关打开时注入 · 保存即生效"),
)

_STATIC_FILES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}

_LOG_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})[.,]\d{3}\s+"
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(.*)$"
)
_TRACEBACK_START = ("Traceback (most recent call last)", "  ", "\t")


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------
def register(app: web.Application, ctx: dict[str, Any]) -> None:
    """把面板挂到主应用上。ctx 由 bot.py 传入（诊断计数、并发闸门、模型入口）。"""
    global _ENV_SNAPSHOT
    # 进程刚起来这一刻的 otowa.env 就是「正在生效的那一版」，立刻留个快照。
    # 之后文件再变（控制台改、或者 ssh 上去手改）都会显示成「待重启」。
    _ENV_SNAPSHOT = _parse_env(_read_env_text())
    _CTX.update(ctx)
    router = app.router
    router.add_get("/", _root)
    router.add_get("/admin", _page)
    router.add_get("/admin/", _page)
    router.add_get("/admin/static/{name}", _static)
    router.add_get("/admin/api/overview", _guard(api_overview))
    router.add_get("/admin/api/usage", _guard(api_usage))
    router.add_get("/admin/api/logs", _guard(api_logs))
    router.add_get("/admin/api/logs/stream", _guard(api_log_stream))
    router.add_get("/admin/api/persona", _guard(api_persona_list))
    router.add_post("/admin/api/persona", _guard(api_persona_save))
    router.add_get("/admin/api/memory", _guard(api_memory_index))
    router.add_get("/admin/api/memory/{group_id}", _guard(api_memory_group))
    router.add_post("/admin/api/memory/{group_id}/entries", _guard(api_memory_upsert))
    router.add_delete(
        "/admin/api/memory/{group_id}/entries/{entry_id}", _guard(api_memory_delete)
    )
    router.add_post("/admin/api/memory/{group_id}/toggle", _guard(api_memory_toggle))
    router.add_post("/admin/api/global/entries", _guard(api_global_upsert))
    router.add_delete("/admin/api/global/entries/{entry_id}", _guard(api_global_delete))
    router.add_get("/admin/api/backgrounds", _guard(api_backgrounds))
    router.add_post("/admin/api/backgrounds", _guard(api_background_upload))
    router.add_get("/admin/api/backgrounds/{name}", _guard(api_background_file))
    router.add_delete("/admin/api/backgrounds/{name}", _guard(api_background_delete))
    router.add_get("/admin/api/env", _guard(api_env_get))
    router.add_post("/admin/api/env", _guard(api_env_save))
    router.add_post("/admin/api/restart", _guard(api_restart))
    router.add_get("/admin/api/stickers", _guard(api_stickers))
    router.add_post("/admin/api/stickers/analyze", _guard(api_sticker_analyze))
    router.add_post("/admin/api/stickers", _guard(api_sticker_upload))
    router.add_get("/admin/api/stickers/{name}", _guard(api_sticker_file))
    router.add_get("/admin/api/prompt", _guard(api_prompt))
    router.add_post("/admin/api/chat", _guard(api_chat))
    logger.info(
        "WebUI 已挂载: http://127.0.0.1:%s/admin （鉴权: %s）",
        os.getenv("ONEBOT_WS_PORT", "8080"),
        "token" if WEBUI_TOKEN else "仅本机",
    )
    _silence_admin_access_log()


class _AdminAccessFilter(logging.Filter):
    """/admin 的每条请求都记一行 access log 的话，面板开着就会把 bot.log 灌满
    （概览 4 秒轮询一次 = 一天两万多行）。只丢这一类，其余访问日志照旧。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "aiohttp.access":
            return True
        try:
            return "/admin" not in record.getMessage()
        except Exception:
            return True


def _silence_admin_access_log() -> None:
    flt = _AdminAccessFilter()
    for handler in logging.getLogger().handlers:
        try:
            handler.addFilter(flt)
        except Exception:
            continue


# --------------------------------------------------------------------------
# 鉴权 / 通用
# --------------------------------------------------------------------------
class _BadRequest(Exception):
    """请求本身不合法（坏 JSON、类型不对）。由 _guard 统一翻成 400。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _display_path(path: Path) -> str:
    """项目内的路径显示成相对路径，项目外的原样返回。

    `WEBUI_ENV_FILE` 存在的意义就是允许配置指向项目外；对它调 `relative_to(ROOT)`
    会抛 ValueError——读路径曾经躲过这个坑，写路径没有，于是「保存必定 500 且没写进去」。
    统一走这个函数，别再各写一份。
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _host_only(raw: str) -> str:
    """从 Host 头里取主机名：`127.0.0.1:8080` / `[::1]:8080` / `localhost`。"""
    host = (raw or "").strip().lower()
    if host.startswith("["):  # [::1]:8080
        return host.split("]", 1)[0] + "]"
    if host.count(":") == 1:  # 127.0.0.1:8080
        return host.split(":", 1)[0]
    return host


def _host_ok(request: web.Request) -> bool:
    raw = (request.headers.get("Host") or "").strip().lower()
    if not raw:
        return False
    if raw in _ALLOWED_HOSTS_EXTRA or _host_only(raw) in _ALLOWED_HOSTS_EXTRA:
        return True
    return _host_only(raw) in _LOOPBACK_HOSTS


def _is_loopback(request: web.Request) -> bool:
    return (request.remote or "") in LOOPBACK


def _authorized(request: web.Request) -> bool:
    if not _host_ok(request):
        # Host 不是本机名 —— 浏览器被 DNS rebinding 指过来的典型特征。
        return False
    if not WEBUI_TOKEN:
        return _is_loopback(request)
    supplied = request.headers.get("X-WebUI-Token") or request.query.get("token") or ""
    return bool(supplied) and hmac.compare_digest(supplied, WEBUI_TOKEN)


def _guard(handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        if not _authorized(request):
            return web.json_response(
                {
                    "ok": False,
                    "error": "未授权：请检查访问地址和控制台 token",
                },
                status=403,
            )
        # 跨源页面带不了自定义头（预检会先被拒），这一条把「用户在别处点了个链接
        # 就顺手改了配置」的路堵掉。前端所有写请求都带这个头。
        if request.method in _WRITE_METHODS and request.headers.get(_CSRF_HEADER) != "1":
            return web.json_response(
                {"ok": False, "error": f"缺少写操作标识头 {_CSRF_HEADER}"}, status=403
            )
        try:
            return await handler(request)
        except _BadRequest as exc:
            return web.json_response({"ok": False, "error": exc.message}, status=400)
        except web.HTTPException:
            raise
        except Exception:  # 面板出错绝不能把异常抛回 aiohttp 变成裸 500
            request_id = uuid.uuid4().hex[:12]
            logger.exception("WebUI 接口失败: %s（request_id=%s）", request.path, request_id)
            # 不把异常类型/原文回给客户端：里面有绝对路径、配置片段。
            return web.json_response(
                {
                    "ok": False,
                    "error": "面板内部错误，详情见服务端日志",
                    "request_id": request_id,
                },
                status=500,
            )

    return wrapper


async def _json_body(request: web.Request) -> dict[str, Any]:
    """读取 JSON 请求体。坏 JSON 是客户端的错，回 400，不是 500。"""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        raise _BadRequest("请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise _BadRequest("请求体必须是 JSON 对象")
    return body


def _json(payload: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(
        payload, status=status, dumps=lambda obj: json.dumps(obj, ensure_ascii=False)
    )


async def _root(request: web.Request) -> web.StreamResponse:
    raise web.HTTPFound("/admin")


async def _page(request: web.Request) -> web.StreamResponse:
    if not _authorized(request):
        return web.Response(
            text="未授权：请检查访问地址和控制台 token。", status=403, content_type="text/plain"
        )
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return web.Response(text="缺少 webui/index.html", status=500)
    return web.Response(
        body=index.read_bytes(),
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def _static(request: web.Request) -> web.StreamResponse:
    # 故意不挂 _guard：index.html 里的 <link>/<script> 没法带 token，走鉴权会让
    # 远程（带 WEBUI_TOKEN）打开页面时样式全丢。代价是「面板存在」这个信号无需
    # 鉴权即可探测——能拿到的也只有白名单里的样式和脚本。
    name = request.match_info["name"]
    if not _host_ok(request):
        raise web.HTTPForbidden()
    mime = _STATIC_FILES.get(name)
    path = STATIC_DIR / name
    if not mime or not path.is_file():
        raise web.HTTPNotFound()
    return web.Response(
        body=path.read_bytes(),
        content_type=mime.split(";")[0],
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------
# 概览
# --------------------------------------------------------------------------
def _cfg() -> Any:
    """config 模块已在 bot 启动时加载过，这里拿的是同一份（含 otowa.env）。"""
    try:
        import config as cfg

        return cfg
    except Exception:  # pragma: no cover - 只在脱离 bot 单独跑时发生
        return None


def _on(cfg: Any, name: str, default: bool = True) -> bool:
    return bool(getattr(cfg, name, default)) if cfg is not None else default


def _num(cfg: Any, name: str, default: Any = "-") -> Any:
    return getattr(cfg, name, default) if cfg is not None else default


def _features(cfg: Any) -> list[dict[str, Any]]:
    sauce_keys = getattr(cfg, "SAUCENAO_API_KEYS", ()) if cfg is not None else ()
    items = [
        (
            "联网搜索",
            _on(cfg, "DEEPSEEK_TOOLS_ENABLED") and _on(cfg, "DEEPSEEK_TOOLS_SEARCH_ENABLED"),
            f"{_num(cfg, 'DEEPSEEK_TOOLS_MODEL')} · 每次 {_num(cfg, 'DEEPSEEK_TOOLS_SEARCH_COUNT')} 条",
        ),
        (
            "打开链接",
            _on(cfg, "WEB_FETCH_ENABLED"),
            f"单页上限 {_num(cfg, 'WEB_FETCH_MAX_CHARS')} 字 · {_num(cfg, 'WEB_FETCH_HOURLY_LIMIT')}/人·时",
        ),
        (
            "图搜图 #搜索",
            _on(cfg, "IMAGE_SEARCH_ENABLED"),
            f"{_num(cfg, 'IMAGE_SEARCH_MODEL')} · {_num(cfg, 'IMAGE_SEARCH_HOURLY_LIMIT')}/人·时",
        ),
        (
            "画图 / 改图",
            _on(cfg, "IMAGE_GEN_ENABLED") and bool(getattr(cfg, "IMAGE_GEN_API_KEY", "")),
            f"{_num(cfg, 'IMAGE_GEN_MODEL')} · 每 {_num(cfg, 'IMAGE_GEN_WINDOW_HOURS')}h "
            f"{_num(cfg, 'IMAGE_GEN_HOURLY_LIMIT')} 张 / 每日 {_num(cfg, 'IMAGE_GEN_DAILY_LIMIT')}",
        ),
        (
            "以图搜源 #搜源",
            _on(cfg, "SAUCENAO_ENABLED") and bool(sauce_keys),
            f"每日 {_num(cfg, 'SAUCENAO_DAILY_LIMIT')} 次 · 双 key" if len(sauce_keys or ()) > 1 else
            f"每日 {_num(cfg, 'SAUCENAO_DAILY_LIMIT')} 次",
        ),
        ("戳一戳", _on(cfg, "POKE_REPLY_ENABLED"), "词库直出，不进模型"),
        ("空 @ 问候", _on(cfg, "EMPTY_MENTION_REPLY_ENABLED"), "只 @ 不说话时回一句"),
        (
            "按题开深度思考",
            _on(cfg, "THINKING_SMART_MODE"),
            f"{_num(cfg, 'THINKING_MIN_CHARS')} 字以上才要思考",
        ),
        (
            "群回复需 @",
            _on(cfg, "GROUP_REPLY_ONLY_WHEN_MENTIONED"),
            "引用机器人的消息也算叫到",
        ),
        (
            "回复白名单",
            _on(cfg, "QQ_REPLY_WHITELIST_ENABLED", False),
            f"已设 {len(getattr(cfg, 'QQ_REPLY_WHITELIST', ()) or ())} 个 QQ",
        ),
    ]
    return [{"label": label, "on": bool(on), "note": note} for label, on, note in items]


def _limits(cfg: Any) -> list[dict[str, str]]:
    gate = _CTX.get("gate")
    stats = gate.stats() if gate is not None else {}
    items = [
        ("同时回复上限", _num(cfg, "MAX_CONCURRENT_REPLIES")),
        ("单会话积压上限", _num(cfg, "MAX_PENDING_PER_CONVERSATION")),
        ("全局在途上限", _num(cfg, "MAX_INFLIGHT_EVENTS")),
        ("上下文轮数", _num(cfg, "CONTEXT_MAX_TURNS")),
        (
            "上下文存活",
            f"{round(float(_num(cfg, 'CONTEXT_TTL_SECONDS', 0)) / 3600, 1)} 小时"
            if cfg is not None
            else "-",
        ),
        ("过期消息阈值", f"{_num(cfg, 'MESSAGE_MAX_AGE_SECONDS')} 秒"),
        ("模型单次超时", f"{_num(cfg, 'AI_REQUEST_TIMEOUT_SECONDS')} 秒"),
        ("健康状态", "gate 已加载" if stats else "gate 缺失"),
    ]
    return [{"label": label, "value": str(value)} for label, value in items]


# 概览每 4 秒被轮询一次，而 _persona_stats 要遍历所有群档案。10 个群是 10 ms 级，
# 500 个群就是几百毫秒的同步磁盘读——全发生在事件循环里，期间机器人回不了消息。
# 这里按 mtime/size 做签名缓存：文件没变就直接返回上次的结果（stat 比 read 便宜两个量级）。
_STATS_CACHE: dict[str, Any] = {"sig": None, "value": None}


def _stat_sig(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (path.name, -1, -1)
    return (path.name, stat.st_mtime_ns, stat.st_size)


def _persona_signature() -> tuple[Any, ...]:
    style = ROOT / "personality" / "style.txt"
    stickers = ROOT / "personality" / "stickers.json"
    groups = []
    try:
        paths = list(GROUPS_DIR.glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        groups.append(_stat_sig(path))
    groups.sort()
    return (_stat_sig(style), _stat_sig(stickers), tuple(groups))


def _persona_stats(cfg: Any) -> dict[str, Any]:
    """同步实现；调用方要丢进线程（asyncio.to_thread），别在事件循环里直接跑。"""
    signature = _persona_signature()
    if _STATS_CACHE["sig"] == signature and _STATS_CACHE["value"] is not None:
        return _STATS_CACHE["value"]

    style = ROOT / "personality" / "style.txt"
    aliases: list[str] = []
    try:
        raw = json.loads((ROOT / "personality" / "stickers.json").read_text("utf-8"))
        if isinstance(raw, dict):
            aliases = [name for name, conf in raw.items() if isinstance(conf, dict)]
    except (OSError, json.JSONDecodeError):
        pass
    groups = 0
    entries = 0
    for path in GROUPS_DIR.glob("*.json"):
        groups += 1
        try:
            data = json.loads(path.read_text("utf-8"))
            entries += len(data.get("memories") or [])
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    value = {
        "style_chars": len(style.read_text("utf-8")) if style.is_file() else 0,
        "stickers": len(aliases),
        "groups": groups,
        "memory_entries": entries,
    }
    _STATS_CACHE.update(sig=signature, value=value)
    return value


async def api_overview(request: web.Request) -> web.StreamResponse:
    cfg = _cfg()
    diag = dict(_CTX.get("diagnostics") or {})
    gate = _CTX.get("gate")
    stats = gate.stats() if gate is not None else {}
    base_url = str(_num(cfg, "AI_BASE_URL", ""))
    host = base_url.split("//", 1)[-1].split("/", 1)[0] if base_url else "-"
    now = time.time()
    try:
        from ai.chat_compat import token_usage_snapshot

        token_usage = await asyncio.to_thread(token_usage_snapshot)
    except Exception:
        token_usage = {
            "calls": None, "prompt_tokens": None, "completion_tokens": None,
            "total_tokens": None, "reasoning_tokens": None, "by_model": {}, "last": None,
            "error": "用量数据库暂不可读",
        }
    return _json(
        {
            "ok": True,
            "now": now,
            "started_at": _STARTED_AT,
            "uptime_seconds": max(0.0, now - _STARTED_AT),
            "bot": {
                "qq": str(_num(cfg, "BOT_QQ", "-")),
                "nickname": str(_num(cfg, "BOT_NICKNAME", "-")),
                "owner_qq": str(_num(cfg, "MEMORY_CONFIRM_QQ", "")),
            },
            "ws": {
                "connected": bool(diag.get("ws_connected")),
                "host": str(_num(cfg, "ONEBOT_WS_HOST", "-")),
                "port": _num(cfg, "ONEBOT_WS_PORT", "-"),
                "path": str(_num(cfg, "ONEBOT_WS_PATH", "-")),
            },
            "diagnostics": diag,
            "token_usage": token_usage,
            "concurrency": stats,
            "model": {
                "chat": str(_num(cfg, "AI_MODEL", "-")),
                "vision": str(_num(cfg, "AI_VISION_MODEL", "-")),
                "vision_split": bool(
                    cfg is not None
                    and getattr(cfg, "VISION_BASE_URL", "") != getattr(cfg, "AI_BASE_URL", "")
                ),
                "host": host,
                "key": "已配置" if getattr(cfg, "AI_API_KEY", "") else "未配置",
                "vision_key": "已配置" if getattr(cfg, "VISION_API_KEY", "") else "未配置",
            },
            "features": _features(cfg),
            "limits": _limits(cfg),
            "persona": await asyncio.to_thread(_persona_stats, cfg),
            "log_file": _display_path(_log_path()) if _log_path() else None,
        }
    )


# --------------------------------------------------------------------------
# 用量
# --------------------------------------------------------------------------
async def api_usage(request: web.Request) -> web.StreamResponse:
    period = request.query.get("period", "all")
    if period not in {"today", "7d", "all"}:
        return _json({"ok": False, "error": "period 仅支持 today、7d、all"}, status=400)
    try:
        from ai.usage import snapshot as usage_snapshot
        data = await asyncio.to_thread(usage_snapshot, period)
    except Exception:
        return _json({"ok": False, "error": "用量数据库暂不可读，不能将错误视为空数据"}, status=503)
    return _json({"ok": True, **data})


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
def _log_path() -> Path | None:
    for rel in LOG_CANDIDATES:
        path = ROOT / rel
        if path.is_file():
            return path
    return None


def _log_item(raw: str) -> dict[str, str]:
    match = _LOG_RE.match(raw)
    if match:
        return {
            "t": f"{match.group(1)} {match.group(2)}",
            "level": match.group(3),
            "msg": match.group(4),
        }
    level = "TRACE" if raw.startswith(_TRACEBACK_START) else ""
    return {"t": "", "level": level, "msg": raw}


def _tail_lines(path: Path, wanted: int, max_bytes: int = 512 * 1024) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        blob = handle.read()
    lines = blob.decode("utf-8", "replace").splitlines()
    return lines[-wanted:]


def _read_from(path: Path, offset: int) -> tuple[int, bytes]:
    """读 offset 之后的新增字节，返回 (当前大小, 新增内容)。"""
    size = path.stat().st_size
    if size <= offset:
        return size, b""
    with path.open("rb") as handle:
        handle.seek(offset)
        return size, handle.read(size - offset)


async def api_logs(request: web.Request) -> web.StreamResponse:
    path = _log_path()
    if path is None:
        return _json(
            {
                "ok": True,
                "file": None,
                "lines": [
                    {
                        "t": "",
                        "level": "",
                        "msg": "还没有日志文件（logs/bot.log 不存在）。机器人跑起来后就有了。",
                    }
                ],
            }
        )
    try:
        wanted = max(10, min(2000, int(request.query.get("lines", "300"))))
    except ValueError:
        wanted = 300
    # 512 KB 的同步读在事件循环里就是一次明显的卡顿，丢线程。
    raw = await asyncio.to_thread(_tail_lines, path, wanted)
    return _json(
        {
            "ok": True,
            "file": _display_path(path),
            "size": path.stat().st_size,
            "lines": [_log_item(line) for line in raw],
        }
    )


async def _sse(resp: web.StreamResponse, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False)
    await resp.write(f"data: {data}\n\n".encode("utf-8"))


async def api_log_stream(request: web.Request) -> web.StreamResponse:
    path = _log_path()
    if path is None:
        return _json({"ok": False, "error": "还没有日志文件"}, status=404)
    resp = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    await resp.prepare(request)
    try:
        # 先补一段历史，前端一打开就有东西看；之后只推新增字节。
        # 断线重连/取消暂停时前端会带 backlog=0，避免把同一段历史再灌一遍。
        try:
            backlog = int(request.query.get("backlog", "150"))
        except ValueError:
            backlog = 150
        backlog = max(0, min(1000, backlog))
        lines = await asyncio.to_thread(_tail_lines, path, backlog) if backlog else []
        for line in lines:
            await _sse(resp, _log_item(line))
        offset = path.stat().st_size
        buffer = b""
        last_ping = time.monotonic()
        while True:
            transport = request.transport
            if transport is None or transport.is_closing():
                break
            try:
                size, chunk = await asyncio.to_thread(_read_from, path, offset)
            except OSError:
                break
            if size < offset:  # 文件被截断/换掉，从头再来
                offset = 0
                try:
                    size, chunk = await asyncio.to_thread(_read_from, path, 0)
                except OSError:
                    break
            if chunk:
                offset += len(chunk)
                buffer += chunk
                *complete, buffer = buffer.split(b"\n")
                for raw in complete:
                    await _sse(resp, _log_item(raw.decode("utf-8", "replace")))
                if len(buffer) > 1_000_000:  # 半行卡住时的兜底，别让内存长起来
                    buffer = b""
            if time.monotonic() - last_ping > 15:
                await resp.write(b": ping\n\n")
                last_ping = time.monotonic()
            await asyncio.sleep(0.7)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        try:
            await resp.write_eof()
        except (ConnectionResetError, RuntimeError):
            pass
    return resp


# --------------------------------------------------------------------------
# 人格文本
# --------------------------------------------------------------------------
def _persona_entry(key: str) -> tuple[str, str, Path, str] | None:
    for entry_key, label, rel, note in _PERSONA_FILES:
        if entry_key == key:
            return label, rel, (ROOT / rel), note
    return None


def _persona_view(key: str, label: str, rel: str, path: Path, note: str) -> dict[str, Any]:
    try:
        content = path.read_text("utf-8")
        mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        size = path.stat().st_size
        exists = True
    except OSError:
        content, mtime, size, exists = "", "-", 0, False
    return {
        "key": key,
        "label": label,
        "path": rel,
        "note": note,
        "exists": exists,
        "content": content,
        "chars": len(content),
        "size": size,
        "mtime": mtime,
    }


async def api_persona_list(request: web.Request) -> web.StreamResponse:
    files = [
        _persona_view(key, label, rel, ROOT / rel, note)
        for key, label, rel, note in _PERSONA_FILES
    ]
    return _json({"ok": True, "files": files, "backup_dir": _display_path(BACKUP_DIR)})


async def api_persona_save(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    key = str(body.get("key") or "")
    content = body.get("content")
    entry = _persona_entry(key)
    if entry is None:
        return _json({"ok": False, "error": f"不可写的人格文件: {key!r}"}, status=400)
    if not isinstance(content, str):
        return _json({"ok": False, "error": "content 必须是字符串"}, status=400)
    label, rel, path, note = entry
    payload = content.encode("utf-8")
    if len(payload) > MAX_EDIT_BYTES:
        return _json(
            {"ok": False, "error": f"内容过大：{len(payload)} 字节，上限 {MAX_EDIT_BYTES}"},
            status=413,
        )
    if path == (ROOT / "personality" / "style.txt") and not content.strip():
        # 空 style.txt 会让提示词只剩 BASE_PROMPT，人格直接塌掉。
        return _json({"ok": False, "error": "style.txt 不能存成空文件"}, status=400)

    path.parent.mkdir(parents=True, exist_ok=True)
    backup_name = ""
    if path.is_file():
        original = path.read_bytes()
        # 浏览器 textarea 只吐 LF。原来是谁的行尾，还回去——否则在 Windows
        # 编辑器里打开会看到「整个文件都被改了」的假 diff。
        if b"\r\n" in original and b"\r\n" not in payload:
            payload = payload.replace(b"\n", b"\r\n")
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = BACKUP_DIR / f"{path.name}.{stamp}.bak"
        shutil.copy2(path, backup)
        backup_name = _display_path(backup)

    # 原子替换：写 .tmp 再 os.replace，中途断电也不会留下半个 style.txt。
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)
    logger.info("WebUI 保存人格文件 %s（%d 字节，备份 %s）", rel, len(payload), backup_name or "无")
    view = _persona_view(key, label, rel, path, note)
    view["backup"] = backup_name
    return _json({"ok": True, "file": view})


# --------------------------------------------------------------------------
# 群记忆 / 全局记忆 / 梗库（只读）
# --------------------------------------------------------------------------
def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


async def api_memory_index(request: web.Request) -> web.StreamResponse:
    groups: list[dict[str, Any]] = []
    for path in GROUPS_DIR.glob("*.json"):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        memories = data.get("memories") or []
        latest = ""
        for item in memories:
            stamp = str(item.get("updated_at") or "")
            latest = max(latest, stamp)
        groups.append(
            {
                "group_id": str(data.get("group_id") or path.stem),
                "group_name": str(data.get("group_name") or ""),
                "count": len(memories),
                "enabled": data.get("memory_enabled") is not False,
                "latest": latest,
            }
        )
    groups.sort(key=lambda row: (-row["count"], row["group_id"]))

    global_entries = (_read_json(ROOT / "memory" / "global.json") or {}).get("entries") or []
    memes = (_read_json(ROOT / "memory" / "memes.json") or {}).get("entries") or []
    return _json(
        {
            "ok": True,
            "groups": groups,
            "global_entries": global_entries,
            "memes": [
                {
                    "name": str(item.get("name") or item.get("id") or ""),
                    "keywords": item.get("keywords") or [],
                    "enabled": item.get("enabled") is not False,
                    "trigger": str(item.get("trigger") or item.get("reply") or item.get("meaning") or ""),
                }
                for item in memes
                if isinstance(item, dict)
            ],
        }
    )


async def api_memory_group(request: web.Request) -> web.StreamResponse:
    group_id = request.match_info["group_id"]
    # 和存储层的 _path_for 用同一条规则：群号只能是纯数字。
    # 下面的 parent 判断仍然留着当第二道闸（纵深防御，别只靠一条）。
    if not re.fullmatch(r"\d+", group_id):
        return _json({"ok": False, "error": "群号不合法"}, status=404)
    path = GROUPS_DIR / f"{group_id}.json"
    if not path.is_file() or path.parent != GROUPS_DIR:
        return _json({"ok": False, "error": "没有这个群"}, status=404)
    data = _read_json(path) or {}
    categories = {
        "group_context": "群背景",
        "shared_context": "共同背景",
        "recurring_topics": "常聊话题",
        "inside_jokes": "群内梗",
        "group_preferences": "群偏好",
        "ongoing_threads": "持续话题",
    }
    entries = []
    for item in data.get("memories") or []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "")
        entries.append(
            {
                "id": str(item.get("id") or ""),
                "category": category,
                "category_label": categories.get(category, category or "未分类"),
                "content": str(item.get("content") or ""),
                "updated_at": str(item.get("updated_at") or ""),
                "source": str(item.get("source") or ""),
            }
        )
    return _json(
        {
            "ok": True,
            "group_id": group_id,
            "group_name": str(data.get("group_name") or ""),
            "enabled": data.get("memory_enabled") is not False,
            "schema_version": data.get("schema_version"),
            "entries": entries,
        }
    )


# --------------------------------------------------------------------------
# 记忆的写操作
# --------------------------------------------------------------------------
# 三条规矩，和机器人自己的写入路径一模一样，绕不过去也不另起一套：
#   1. 群记忆走 GroupMemoryStore 的方法（它自带每群锁 + 原子替换）；
#   2. 全局事实走 memory.global_memory 的锁 + 原子替换；
#   3. 校验（分类白名单、字数、隐私词、主语必须锚 QQ号）在存储层做一次，
#      控制台只负责把「为什么被拒」原样显示出来。
async def api_memory_upsert(request: web.Request) -> web.StreamResponse:
    store = _CTX.get("group_memory")
    if store is None:
        return _json({"ok": False, "error": "bot 没有注入 group_memory"}, status=501)
    group_id = request.match_info["group_id"]
    body = await _json_body(request)
    ok, message = store.upsert_entry(
        group_id,
        category=str(body.get("category") or ""),
        content=str(body.get("content") or ""),
        entry_id=str(body.get("id") or ""),
    )
    logger.info("控制台写群记忆 group=%s ok=%s msg=%s", group_id, ok, message)
    return _json(
        {"ok": ok, "message": message, "error": "" if ok else message},
        status=200 if ok else 400,
    )


async def api_memory_delete(request: web.Request) -> web.StreamResponse:
    store = _CTX.get("group_memory")
    if store is None:
        return _json({"ok": False, "error": "bot 没有注入 group_memory"}, status=501)
    group_id = request.match_info["group_id"]
    entry_id = request.match_info["entry_id"]
    ok, message = store.delete_entry(group_id, entry_id)
    logger.info("控制台删群记忆 group=%s id=%s ok=%s", group_id, entry_id, ok)
    return _json(
        {"ok": ok, "message": message, "error": "" if ok else message},
        status=200 if ok else 400,
    )


async def api_memory_toggle(request: web.Request) -> web.StreamResponse:
    store = _CTX.get("group_memory")
    if store is None:
        return _json({"ok": False, "error": "bot 没有注入 group_memory"}, status=501)
    group_id = request.match_info["group_id"]
    body = await _json_body(request)
    enabled = bool(body.get("enabled"))
    ok, changed = store.set_memory_enabled(
        group_id,
        enabled,
        operator_qq=str(_num(_cfg(), "MEMORY_CONFIRM_QQ", "")),
        operator_label="控制台",
        source="WebUI 控制台",
    )
    if not ok:
        return _json({"ok": False, "error": "这个群还没有记忆档案"}, status=400)
    logger.info("控制台切换群记忆开关 group=%s enabled=%s changed=%s", group_id, enabled, changed)
    return _json({"ok": True, "enabled": enabled, "changed": changed})


async def api_global_upsert(request: web.Request) -> web.StreamResponse:
    from memory.global_memory import upsert_entry

    body = await _json_body(request)
    # 文件很小、纯本地 IO，直接同步跑；写成线程反而多一层竞态面。
    ok, message, entry = upsert_entry(
        entry_id=str(body.get("id") or ""),
        topic=str(body.get("topic") or ""),
        content=str(body.get("content") or ""),
    )
    logger.info("控制台写全局事实 ok=%s msg=%s", ok, message)
    return _json(
        {
            "ok": ok,
            "message": message,
            "error": "" if ok else message,
            "entry": entry,
        },
        status=200 if ok else 400,
    )


async def api_global_delete(request: web.Request) -> web.StreamResponse:
    from memory.global_memory import delete_entry

    ok, message = delete_entry(request.match_info["entry_id"])
    logger.info("控制台删全局事实 id=%s ok=%s", request.match_info["entry_id"], ok)
    return _json(
        {"ok": ok, "message": message, "error": "" if ok else message},
        status=200 if ok else 400,
    )


# --------------------------------------------------------------------------
# 背景图
# --------------------------------------------------------------------------
_IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


def _normalize_image(blob: bytes) -> tuple[bytes, str]:
    """校验并（必要时）压到 2560 以内的 jpg，返回 ``(字节, 扩展名)``。"""
    from PIL import Image

    with Image.open(io.BytesIO(blob)) as img:
        img.load()
        fmt = (img.format or "").upper()
        if fmt not in {"PNG", "JPEG", "WEBP", "GIF"}:
            raise ValueError(f"不支持的图片格式：{fmt or '未知'}")
        if max(img.width, img.height) > 3840 or img.width * img.height > 12_000_000:
            # 4K 起步的图当背景没人看得出差别，但浏览器吃内存、传输也慢。
            shrunk = img.convert("RGB")
            shrunk.thumbnail((2560, 2560), Image.LANCZOS)
            buffer = io.BytesIO()
            shrunk.save(buffer, format="JPEG", quality=90, optimize=True)
            return buffer.getvalue(), "jpg"
    return blob, {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}[fmt]


def _background_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not BACKGROUND_DIR.is_dir():
        return items
    for path in BACKGROUND_DIR.iterdir():
        if not path.is_file() or not BG_NAME_RE.match(path.name):
            continue
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "added": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "url": f"/admin/api/backgrounds/{path.name}",
            }
        )
    items.sort(key=lambda item: item["added"], reverse=True)
    return items


async def api_backgrounds(request: web.Request) -> web.StreamResponse:
    return _json({"ok": True, "items": _background_items(), "dir": "data/webui/backgrounds"})


async def api_background_upload(request: web.Request) -> web.StreamResponse:
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES + 64 * 1024:
        return _json(
            {"ok": False, "error": f"文件太大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）"},
            status=413,
        )
    post = await request.post()
    field = post.get("file")
    if not isinstance(field, web.FileField):
        return _json({"ok": False, "error": "没有收到 file 字段"}, status=400)
    blob = field.file.read(MAX_UPLOAD_BYTES + 1)
    if len(blob) > MAX_UPLOAD_BYTES:
        return _json(
            {"ok": False, "error": f"文件太大（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）"},
            status=413,
        )
    try:
        # 解码大图是 CPU 活，丢线程里跑，别卡住正在回消息的事件循环。
        blob, ext = await asyncio.to_thread(_normalize_image, blob)
    except Exception as exc:
        return _json({"ok": False, "error": f"这不是能用的图片：{exc}"}, status=400)

    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    name = f"bg-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}.{ext}"
    (BACKGROUND_DIR / name).write_bytes(blob)
    logger.info("控制台上传背景图 %s（%d 字节）", name, len(blob))
    item = next((i for i in _background_items() if i["name"] == name), None)
    return _json({"ok": True, "item": item})


async def api_background_file(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if not BG_NAME_RE.match(name):
        raise web.HTTPNotFound()
    path = BACKGROUND_DIR / name
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.Response(
        body=path.read_bytes(),
        content_type=_IMAGE_TYPES[path.suffix.lstrip(".").lower()],
        headers={"Cache-Control": "max-age=86400"},
    )


async def api_background_delete(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    if not BG_NAME_RE.match(name):
        return _json({"ok": False, "error": "文件名不对"}, status=400)
    path = BACKGROUND_DIR / name
    if not path.is_file():
        return _json({"ok": False, "error": "没有这张图"}, status=404)
    path.unlink()
    logger.info("控制台删除背景图 %s", name)
    return _json({"ok": True, "deleted": name})


# --------------------------------------------------------------------------
# 基本设置（otowa.env）
# --------------------------------------------------------------------------
# 只做「改值」，不做「随便往里塞键」：白名单外的键一概拒收，否则拼错一个字母
# 就变成一行永不生效的垃圾，还看不出来。空值 = 删掉这一行（回到程序里的默认值），
# 而不是写成 KEY= —— 那样 int('') 会在启动时炸掉整个进程。
_ENV_SECTIONS = (
    ("identity", "身份与权限", "改这里不会换号，只是让程序知道「我是谁」；白名单只在其开关打开时生效。"),
    ("chat", "聊天模型", "主对话通道。换供应商时 AI_BASE_URL 和 AI_API_KEY 要一起改，模型名要对着端点上的真实名字。"),
    ("vision", "看图", "图片消息走的模型；和聊天分家时 VISION_* 要能独立指向另一个端点。"),
    ("tools", "联网与工具", "模型自己决定要不要搜、要不要打开链接；工具式搜索和网页抓取是两个独立开关。"),
    ("media", "图搜 / 生图 / 搜源", "按次计费的三条通道，各自独立端点与 key，换任一家不会把另外两条带下水。"),
    ("runtime", "并发与上下文", "并发数不是越大越好：每个并发对应一条真实的模型请求，别超过供应商的 RPM。"),
    ("service", "服务连接", "NapCat 反向 WS 与本机 HTTP；这些一改，端口对不上就连不上，改完务必重启。"),
)

_ENV_FIELDS: tuple[dict[str, Any], ...] = (
    # -- 身份与权限 --
    {"section": "identity", "key": "BOT_QQ", "label": "机器人 QQ", "type": "qq", "required": True,
     "hint": "事件里用它判断「这条是不是我自己发的」，写错会把自己的消息当成别人。"},
    {"section": "identity", "key": "BOT_NAME", "label": "角色名", "type": "text"},
    {"section": "identity", "key": "OWNER_QQ", "label": "实例管理者 QQ", "type": "qq"},
    {"section": "identity", "key": "BOT_NICKNAME", "label": "昵称", "type": "text",
     "hint": "用于识别纯文本中的 @昵称。"},
    {"section": "identity", "key": "MEMORY_CONFIRM_QQ", "label": "记忆确认人 QQ", "type": "qq",
     "hint": "高危记忆写入必须这个人拍板。"},
    {"section": "identity", "key": "MEMORY_CONFIRM_TTL_SECONDS", "label": "待确认有效期（秒）", "type": "int",
     "min": 60, "max": 86400},
    {"section": "identity", "key": "QQ_REPLY_WHITELIST_ENABLED", "label": "回复白名单", "type": "bool",
     "hint": "打开后只有下面这些 QQ 能触发回复。"},
    {"section": "identity", "key": "QQ_REPLY_WHITELIST", "label": "白名单 QQ", "type": "qqlist",
     "hint": "逗号或空格分隔。"},

    # -- 聊天模型 --
    {"section": "chat", "key": "AI_BASE_URL", "label": "接口地址", "type": "url",
     "hint": "OpenAI 兼容端点；结尾不要带 /chat/completions（带了会自动剥掉并报警告）。"},
    {"section": "chat", "key": "AI_API_KEY", "label": "API Key", "type": "secret",
     "hint": "留空表示不修改。旧配置的 DEEPSEEK_API_KEY 会兜底，但建议只用这一个。"},
    {"section": "chat", "key": "AI_MODEL", "label": "模型", "type": "text",
     "hint": "必须是端点上真实存在的模型名，否则每次调用都 400。"},
    {"section": "chat", "key": "AI_REQUEST_TIMEOUT_SECONDS", "label": "单次超时（秒）", "type": "int",
     "min": 10, "max": 600, "hint": "超时太长会占住并发槽位；90 秒够慢思考模型出完 3500 token。"},
    {"section": "chat", "key": "AI_MAX_RETRIES", "label": "失败重试次数", "type": "int", "min": 0, "max": 5},
    {"section": "chat", "key": "AI_PROXY", "label": "代理", "type": "text",
     "hint": "http://127.0.0.1:7897 这种；留空直连。"},
    {"section": "chat", "key": "THINKING_SMART_MODE", "label": "按题开深度思考", "type": "bool"},
    {"section": "chat", "key": "THINKING_MIN_CHARS", "label": "开思考的字数门槛", "type": "int", "min": 1, "max": 1000},
    {"section": "chat", "key": "BAILIAN_WEB_SEARCH_ENABLED", "label": "百炼内置联网", "type": "bool",
     "hint": "只有千问系模型 + 百炼端点才有效；走官方工具式搜索时不用开。"},

    # -- 看图 --
    {"section": "vision", "key": "AI_VISION_MODEL", "label": "看图模型", "type": "text"},
    {"section": "vision", "key": "VISION_BASE_URL", "label": "看图端点", "type": "url",
     "hint": "留空 = 跟随聊天端点。"},
    {"section": "vision", "key": "VISION_API_KEY", "label": "看图 Key", "type": "secret", "hint": "留空 = 不修改，沿用聊天 key。"},
    {"section": "vision", "key": "VISION_DETAIL", "label": "图片精度", "type": "select",
     "options": ["low", "high", "original", "auto"]},
    {"section": "vision", "key": "MAX_IMAGES_PER_MESSAGE", "label": "单条最多几张图", "type": "int", "min": 1, "max": 5},

    # -- 联网与工具 --
    {"section": "tools", "key": "DEEPSEEK_TOOLS_ENABLED", "label": "工具通道总开关", "type": "bool"},
    {"section": "tools", "key": "DEEPSEEK_TOOLS_MODEL", "label": "工具模型", "type": "text",
     "hint": "官方通道默认 deepseek-flash。"},
    {"section": "tools", "key": "DEEPSEEK_TOOLS_BASE_URL", "label": "工具端点", "type": "url"},
    {"section": "tools", "key": "DEEPSEEK_TOOLS_API_KEY", "label": "工具 Key", "type": "secret",
     "hint": "留空 = 不修改。注意这里用的是官方 key，不是网关的。"},
    {"section": "tools", "key": "DEEPSEEK_TOOLS_SEARCH_ENABLED", "label": "联网搜索", "type": "bool"},
    {"section": "tools", "key": "DEEPSEEK_TOOLS_FETCH_ENABLED", "label": "打开链接", "type": "bool"},
    {"section": "tools", "key": "DEEPSEEK_TOOLS_SEARCH_COUNT", "label": "每次搜索条数", "type": "int", "min": 1, "max": 50},
    {"section": "tools", "key": "WEB_FETCH_ENABLED", "label": "网页抓取", "type": "bool"},
    {"section": "tools", "key": "WEB_FETCH_HOURLY_LIMIT", "label": "抓取次数/小时/人", "type": "int", "min": 0, "max": 1000,
     "hint": "0 = 不限。"},

    # -- 图搜 / 生图 / 搜源 --
    {"section": "media", "key": "IMAGE_SEARCH_ENABLED", "label": "图搜图 #搜索", "type": "bool"},
    {"section": "media", "key": "IMAGE_SEARCH_MODEL", "label": "图搜模型", "type": "text",
     "hint": "只认千问 Plus/Max/27b 系。"},
    {"section": "media", "key": "IMAGE_SEARCH_BASE_URL", "label": "图搜端点", "type": "url"},
    {"section": "media", "key": "IMAGE_SEARCH_API_KEY", "label": "图搜 Key", "type": "secret", "hint": "留空 = 不修改。"},
    {"section": "media", "key": "IMAGE_SEARCH_HOURLY_LIMIT", "label": "图搜次数/小时/人", "type": "int", "min": 1, "max": 100},
    {"section": "media", "key": "IMAGE_SEARCH_UNLIMITED_QQ", "label": "图搜不限次 QQ", "type": "qqlist"},
    {"section": "media", "key": "IMAGE_GEN_ENABLED", "label": "画图 / 改图", "type": "bool"},
    {"section": "media", "key": "IMAGE_GEN_API_KEY", "label": "生图 Key", "type": "secret",
     "hint": "留空 = 不修改；清空这一项等于关掉画图。"},
    {"section": "media", "key": "IMAGE_GEN_BASE_URL", "label": "生图端点", "type": "url"},
    {"section": "media", "key": "IMAGE_GEN_MODEL", "label": "生图模型", "type": "text"},
    {"section": "media", "key": "IMAGE_GEN_SIZE", "label": "出图尺寸", "type": "select",
     "options": ["1024x1024", "1536x1024", "1024x1536", "auto"]},
    {"section": "media", "key": "IMAGE_GEN_HOURLY_LIMIT", "label": "每人每窗口张数", "type": "int", "min": 1, "max": 50},
    {"section": "media", "key": "IMAGE_GEN_WINDOW_HOURS", "label": "窗口小时数", "type": "int", "min": 1, "max": 24},
    {"section": "media", "key": "IMAGE_GEN_DAILY_LIMIT", "label": "全局每日张数", "type": "int", "min": 1, "max": 1000},
    {"section": "media", "key": "IMAGE_GEN_UNLIMITED_QQ", "label": "画图不限次 QQ", "type": "qqlist"},
    {"section": "media", "key": "SAUCENAO_ENABLED", "label": "以图搜源 #搜源", "type": "bool"},
    {"section": "media", "key": "SAUCENAO_API_KEY", "label": "SauceNAO Key", "type": "secret", "hint": "留空 = 不修改。"},
    {"section": "media", "key": "SAUCENAO_DAILY_LIMIT", "label": "搜源每日上限", "type": "int", "min": 1, "max": 500},

    # -- 并发与上下文 --
    {"section": "runtime", "key": "MAX_CONCURRENT_REPLIES", "label": "同时回复上限", "type": "int", "min": 1, "max": 32},
    {"section": "runtime", "key": "MAX_PENDING_PER_CONVERSATION", "label": "单会话积压上限", "type": "int", "min": 1, "max": 20},
    {"section": "runtime", "key": "MAX_INFLIGHT_EVENTS", "label": "全局在途上限", "type": "int", "min": 1, "max": 512},
    {"section": "runtime", "key": "MESSAGE_MAX_AGE_SECONDS", "label": "过期消息阈值（秒）", "type": "int", "min": 0, "max": 3600,
     "hint": "排队太久的问题不再补答；0 = 不检查。"},
    {"section": "runtime", "key": "CONTEXT_MAX_TURNS", "label": "上下文轮数", "type": "int", "min": 1, "max": 500},
    {"section": "runtime", "key": "CONTEXT_TTL_SECONDS", "label": "上下文存活（秒）", "type": "int", "min": 60, "max": 604800},
    {"section": "runtime", "key": "GROUP_REPLY_ONLY_WHEN_MENTIONED", "label": "群里必须 @", "type": "bool"},
    {"section": "runtime", "key": "GROUP_REPLY_TO_BOT_AS_MENTION", "label": "引用机器人也算叫它", "type": "bool"},
    {"section": "runtime", "key": "POKE_REPLY_ENABLED", "label": "戳一戳回复", "type": "bool"},

    # -- 服务连接 --
    {"section": "service", "key": "ONEBOT_WS_HOST", "label": "监听地址", "type": "host",
     "hint": "只绑 127.0.0.1；要外网访问请走隧道，别直接 0.0.0.0。"},
    {"section": "service", "key": "ONEBOT_WS_PORT", "label": "监听端口", "type": "int", "min": 1, "max": 65535},
    {"section": "service", "key": "ONEBOT_WS_PATH", "label": "WS 路径", "type": "path",
     "hint": "NapCat 里配的反向 WS 地址必须和这里一致。"},
    {"section": "service", "key": "NAPCAT_API", "label": "NapCat HTTP", "type": "url"},
    {"section": "service", "key": "NAPCAT_TOKEN", "label": "NapCat Token", "type": "secret", "hint": "留空 = 不修改。"},
    {"section": "service", "key": "WEBUI_TOKEN", "label": "控制台 Token", "type": "secret",
     "hint": "留空时只允许本机访问；填入后，本机和远程请求都需要 token。"},
)

_FIELD_BY_KEY = {field["key"]: field for field in _ENV_FIELDS}
_ENV_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
_SECRET_TYPES = {"secret"}


def _read_env_text() -> str:
    try:
        return ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_LINE_RE.match(line.strip())
        if match:
            values[match.group("key")] = match.group("value").strip()
    return values


def _mask_secret(value: str) -> str:
    if not value:
        return "（未设置）"
    if len(value) <= 8:
        return "••••" + value[-2:]
    prefix = value[:3] if value[:3].lower().startswith("sk-") else ""
    return f"{prefix}••••{value[-4:]}"


def _validate_env(field: dict[str, Any], value: str) -> str | None:
    """返回错误文案；None 表示通过。空串由调用方当作「删掉这行」。"""
    if "\n" in value or "\r" in value:
        return "值里不能有换行"
    if not value:
        if field.get("required"):
            return "这一项不能为空"
        return None
    kind = field["type"]
    if kind == "qq":
        if not re.fullmatch(r"\d{5,12}", value):
            return "QQ 号应为 5–12 位数字"
    elif kind == "qqlist":
        parts = [p for p in re.split(r"[,，;；\s]+", value) if p]
        if any(not p.isdigit() for p in parts):
            return "只能填 QQ 号，用逗号或空格分隔"
    elif kind == "int":
        if not re.fullmatch(r"-?\d+", value):
            return "要填整数"
        number = int(value)
        if "min" in field and number < field["min"]:
            return f"不能小于 {field['min']}"
        if "max" in field and number > field["max"]:
            return f"不能大于 {field['max']}"
    elif kind == "float":
        try:
            float(value)
        except ValueError:
            return "要填数字"
    elif kind == "bool":
        if value.lower() not in {"true", "false"}:
            return "只能是 true / false"
    elif kind == "url":
        if not re.match(r"^https?://", value):
            return "要以 http:// 或 https:// 开头"
    elif kind == "host":
        if re.search(r"\s", value):
            return "地址里不能有空格"
    elif kind == "path":
        if not value.startswith("/"):
            return "路径要以 / 开头"
    elif kind == "select":
        if value not in (field.get("options") or []):
            return "只能从给定选项里选"
    elif kind == "secret":
        if re.search(r"\s", value):
            return "密钥里不该有空白字符"
    return None


class _EnvWriteError(Exception):
    """写前自检不通过。此时文件**没有**被改写，调用方回 400 说明原因。"""


def _env_change_row(key: str, old: str, value: str) -> dict[str, str]:
    """一行改动摘要。密钥永远只出现掩码，值不进日志也不回前端。"""
    field = _FIELD_BY_KEY[key]
    secret = field["type"] in _SECRET_TYPES
    return {
        "key": key,
        "label": field["label"],
        "old": _mask_secret(old) if secret else (old or "（未设置）"),
        "new": _mask_secret(value) if secret else (value or "（删除该行，用默认值）"),
    }


def _env_keys(text: str) -> dict[str, int]:
    """统计每个键出现了几次。重复键是「改不动还报成功」的源头，得让人看见。"""
    counts: dict[str, int] = {}
    for line in text.splitlines():
        match = _ENV_LINE_RE.match(line.strip())
        if match:
            counts[match.group("key")] = counts.get(match.group("key"), 0) + 1
    return counts


def _dotenv_values_of(text: str) -> dict[str, str]:
    """用 dotenv 自己的解析器读一遍文本——这才是程序启动时真正拿到的值。"""
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - requirements 里有，兜底而已
        return _parse_env(text)
    try:
        parsed = dotenv_values(stream=io.StringIO(text))
    except Exception:  # pragma: no cover - 解析器自己出错时退回朴素解析
        return _parse_env(text)
    return {key: (value or "") for key, value in parsed.items()}


def _verify_env_text(text: str, changes: dict[str, str]) -> None:
    """写盘前用 dotenv 回读一遍：字符串拼接对了不算数，解析结果对了才算。

    python-dotenv 对重复键取最后一次出现，遇到 `#`、`${}`、引号也有自己的规则。
    与其相信自己的拼接，不如让它读一遍：读回来的值和界面要写的值不一致，就拒绝写入。
    """
    actual = _dotenv_values_of(text)
    bad = [key for key, want in changes.items() if actual.get(key, "") != want]
    if bad:
        raise _EnvWriteError(
            "写入前自检没过：" + "、".join(bad[:5]) + " 的值里有 dotenv 无法原样保留的字符"
            "（# 前有空格、${变量}、引号）。换个写法再保存。"
        )


def _apply_env_changes(changes: dict[str, str]) -> tuple[list[dict[str, str]], str]:
    """把改动写回 otowa.env，保留注释、顺序和原来的行尾。返回 (改动摘要, 备份名)。"""
    text = _read_env_text()
    eol = "\r\n" if "\r\n" in text else "\n"
    had_trailing_newline = text.endswith(("\n", "\r\n")) or not text
    lines = text.splitlines()

    # dotenv 对重复键取「最后一次出现」。以前这里用 setdefault 记第一次，
    # 结果面板改了第一行、真正生效的是最后一行：报成功，实际没变。
    indices: dict[str, list[int]] = {}
    for idx, line in enumerate(lines):
        match = _ENV_LINE_RE.match(line.strip())
        if match:
            indices.setdefault(match.group("key"), []).append(idx)

    summary: list[dict[str, str]] = []
    appended: list[str] = []
    for key, value in changes.items():
        where = indices.get(key) or []
        # 界面上的「原来是什么」也要按 dotenv 的语义取最后一行。
        old = _parse_env(lines[where[-1]]).get(key, "") if where else ""
        if value == "":
            for idx in where:  # 清空 = 删掉这个键的**所有**行，否则重复行还会生效
                lines[idx] = None  # type: ignore[call-overload]
        elif where:
            lines[where[-1]] = f"{key}={value}"
            for idx in where[:-1]:  # 顺手把前面重复的旧行清掉，避免「改不动」
                lines[idx] = None  # type: ignore[call-overload]
        else:
            appended.append(f"{key}={value}")
        summary.append(_env_change_row(key, old, value))

    kept = [line for line in lines if line is not None]
    if appended:
        if kept and kept[-1].strip():
            kept.append("")
        kept.append("# --- 以下由控制台追加 ---")
        kept.extend(appended)
    new_text = eol.join(kept) + (eol if had_trailing_newline and kept else "")

    # 先验证、后落盘：验证不过就一个字都不改，别留「备份写了、配置没写」的中间态。
    _verify_env_text(new_text, changes)

    backup_name = ""
    if ENV_FILE.is_file():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        # 精确到微秒：同一秒内连点两次保存不会互相覆盖备份。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = BACKUP_DIR / f"{ENV_FILE.name}.{stamp}.bak"
        shutil.copy2(ENV_FILE, backup)
        backup_name = _display_path(backup)

    temporary = ENV_FILE.with_suffix(ENV_FILE.suffix + ".tmp")
    temporary.write_text(new_text, encoding="utf-8")
    os.replace(temporary, ENV_FILE)
    return summary, backup_name


# 进程启动那一刻读到的值。拿它和当前文件比，才能准确回答「改了没重启」——
# 和 config 里的属性比是不行的：那边已经被转成了 bool / float / frozenset，
# 一比就全在「待重启」。
_ENV_SNAPSHOT: dict[str, str] | None = None


def _env_snapshot() -> dict[str, str]:
    global _ENV_SNAPSHOT
    if _ENV_SNAPSHOT is None:
        _ENV_SNAPSHOT = _parse_env(_read_env_text())
    return _ENV_SNAPSHOT


def _format_effective(field: dict[str, Any], value: Any) -> str:
    """把 config 里解析后的值写成人能读的样子。

    frozenset({'1000000001'}) 这种直接甩给用户没意义；bool 也统一成开/关。
    """
    if value is None:
        return ""
    if isinstance(value, (set, frozenset)):
        items = sorted(str(item) for item in value)
        return ", ".join(items) if items else "（空）"
    if field["type"] == "bool":
        return "开" if bool(value) else "关"
    return str(value)


def _env_view() -> dict[str, Any]:
    text = _read_env_text()
    values = _parse_env(text)
    key_counts = _env_keys(text)
    duplicates = {key: count for key, count in key_counts.items() if count > 1}
    cfg = _cfg()
    sections: list[dict[str, Any]] = []
    pending: list[str] = []
    for section_id, title, note in _ENV_SECTIONS:
        fields = []
        for field in _ENV_FIELDS:
            if field["section"] != section_id:
                continue
            key = field["key"]
            value = values.get(key, "")
            effective = getattr(cfg, key, None) if cfg is not None else None
            effective_text = _format_effective(field, effective)
            secret = field["type"] in _SECRET_TYPES
            # 和进程启动时的快照比：不一致 = 改了还没重启。
            stale = value != _env_snapshot().get(key, "")
            if stale:
                pending.append(key)
            fields.append(
                {
                    "key": key,
                    "label": field["label"],
                    "type": field["type"],
                    "hint": field.get("hint", ""),
                    "required": bool(field.get("required")),
                    "options": field.get("options"),
                    "min": field.get("min"),
                    "max": field.get("max"),
                    "secret": secret,
                    "is_set": bool(value),
                    "value": "" if secret else value,
                    "display": _mask_secret(value) if secret else value,
                    "effective": _mask_secret(effective_text) if secret else effective_text,
                    "stale": stale,
                    "duplicate": key_counts.get(key, 0),
                }
            )
        sections.append({"id": section_id, "title": title, "note": note, "fields": fields})
    return {
        "ok": True,
        "file": _display_path(ENV_FILE),
        "mtime": datetime.fromtimestamp(ENV_FILE.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        if ENV_FILE.is_file()
        else "—",
        "pending": pending,
        # 写了几次的键：dotenv 只认最后一次，界面在保存前就该提醒。
        "duplicates": duplicates,
        "can_restart": bool(shutil.which("systemctl")),
        "unit": SERVICE_UNIT,
        "sections": sections,
    }


async def api_env_get(request: web.Request) -> web.StreamResponse:
    return _json(_env_view())


async def api_env_save(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    changes = body.get("changes")
    if not isinstance(changes, dict):
        return _json({"ok": False, "error": "changes 必须是对象"}, status=400)

    unknown = [key for key in changes if key not in _FIELD_BY_KEY]
    if unknown:
        return _json(
            {"ok": False, "error": f"不认识的配置项：{', '.join(unknown[:5])}"}, status=400
        )

    errors: dict[str, str] = {}
    clean: dict[str, str] = {}
    for key, raw in changes.items():
        value = str(raw if raw is not None else "").strip()
        problem = _validate_env(_FIELD_BY_KEY[key], value)
        if problem:
            errors[key] = problem
        else:
            clean[key] = value
    if errors:
        return _json({"ok": False, "error": "有几项不合法，未写入", "errors": errors}, status=400)

    current = _parse_env(_read_env_text())
    todo = {key: value for key, value in clean.items() if current.get(key, "") != value}
    if not todo:
        return _json({"ok": True, "changed": [], "message": "没有改动", "pending": _env_view()["pending"]})

    try:
        summary, backup = _apply_env_changes(todo)
    except _EnvWriteError as exc:
        # 自检不过：文件一个字都没改，把原因原样告诉人，别含糊成 500。
        logger.warning("控制台改配置未写入：%s", exc)
        return _json({"ok": False, "error": str(exc)}, status=400)
    except OSError as exc:
        logger.exception("写入 otowa.env 失败")
        return _json({"ok": False, "error": f"写文件失败：{exc}"}, status=500)

    # 只记键名，不记值——密钥不进日志。
    logger.info(
        "控制台改配置 %d 项: %s（备份 %s）",
        len(summary),
        ", ".join(item["key"] for item in summary),
        backup or "无",
    )
    view = _env_view()
    return _json(
        {
            "ok": True,
            "changed": summary,
            "backup": backup,
            "pending": view["pending"],
            "mtime": view["mtime"],
            "message": f"已写入 {len(summary)} 项，重启后生效",
        }
    )


async def api_restart(request: web.Request) -> web.StreamResponse:
    """重启服务。只有 systemd 管着的机器才做，开发机的裸进程给明确提示。"""
    if not shutil.which("systemctl"):
        return _json(
            {
                "ok": False,
                "error": "这台机器没有 systemd（开发环境）。停止当前进程后重新运行 start_bot.bat 或 bot.py。",
            },
            status=400,
        )
    try:
        state = subprocess.run(
            ["systemctl", "is-active", SERVICE_UNIT],
            capture_output=True,
            text=True,
            timeout=8,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return _json({"ok": False, "error": f"查询服务状态失败：{exc}"}, status=500)
    if state != "active":
        return _json(
            {"ok": False, "error": f"{SERVICE_UNIT} 当前状态是「{state or '未知'}」，不自动重启"},
            status=400,
        )

    async def _later() -> None:
        # 先把 HTTP 响应送出去，再让 systemd 动手：--no-block 只是把任务排进队列，
        # 由 manager 去停启，不会出现「自己等自己退出」的死锁。
        await asyncio.sleep(1.2)
        try:
            subprocess.Popen(
                ["systemctl", "restart", "--no-block", SERVICE_UNIT],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            logger.exception("调用 systemctl restart 失败")

    asyncio.create_task(_later())
    logger.warning("控制台触发重启 %s（1.2 秒后执行）", SERVICE_UNIT)
    return _json({"ok": True, "message": f"1.2 秒后重启 {SERVICE_UNIT}，约 10 秒后本页会自动刷新"})


# --------------------------------------------------------------------------
# 表情包
# --------------------------------------------------------------------------
async def api_stickers(request: web.Request) -> web.StreamResponse:
    sticker_dir = STICKER_DIR
    mapping = _read_json(STICKER_FILE) or {}
    items = []
    for alias, config in mapping.items():
        if not isinstance(alias, str):
            continue
        if not isinstance(config, dict):
            items.append({"alias": alias, "configured": False})
            continue
        kind = str(config.get("type") or "")
        name = str(config.get("file") or "")
        path = sticker_dir / Path(name).name if name else None
        exists = bool(path and path.is_file())
        item = {
            "alias": alias,
            "configured": True,
            "type": kind,
            "file": name,
            "exists": exists,
            "size": path.stat().st_size if exists and path else 0,
            "url": f"/admin/api/stickers/{Path(name).name}" if exists else "",
        }
        for field in ("category", "emotion", "description", "usage", "tags", "confidence"):
            if field in config:
                item[field] = config[field]
        items.append(item)
    return _json({"ok": True, "items": items, "dir": "personality/stickers"})


def _validate_sticker_image(blob: bytes) -> str:
    """校验表情图片但不转码，保留 PNG 透明通道和 GIF 动画。"""
    from PIL import Image

    with Image.open(io.BytesIO(blob)) as img:
        img.load()
        fmt = (img.format or "").upper()
        if fmt not in {"PNG", "JPEG", "WEBP", "GIF"}:
            raise ValueError(f"不支持的图片格式：{fmt or '未知'}")
        if img.width < 1 or img.height < 1:
            raise ValueError("图片尺寸无效")
        if max(img.width, img.height) > 2048 or img.width * img.height > 8_000_000:
            raise ValueError("表情图片最大边不能超过 2048px，总像素不能超过 800 万")
    return {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}[fmt]


_STICKER_CATEGORIES = ("开心", "难过", "生气", "惊讶", "疑惑", "无奈", "嫌弃", "害羞", "鼓励", "道歉", "问候", "其他")


def _sticker_params(source: Any) -> dict[str, Any]:
    """把模型或人工提交的参数收敛到固定形状，避免任意 JSON 污染映射。"""
    def text(name: str, limit: int) -> str:
        return str(source.get(name) or "").strip()[:limit]

    raw_tags = source.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = re.split(r"[,，]", raw_tags)
    if not isinstance(raw_tags, (list, tuple)):
        raw_tags = []
    tags = list(dict.fromkeys(str(tag).strip()[:12] for tag in raw_tags if str(tag).strip()))[:6]
    category = text("category", 12)
    if category not in _STICKER_CATEGORIES:
        category = "其他"
    try:
        confidence = min(1.0, max(0.0, float(source.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "category": category,
        "emotion": text("emotion", 16),
        "description": text("description", 120),
        "usage": text("usage", 120),
        "tags": tags,
        "confidence": round(confidence, 2),
    }


def _sticker_analysis_data(blob: bytes) -> str:
    """给视觉模型一张较小的 PNG 首帧；原图仍按原格式保存。"""
    from PIL import Image

    with Image.open(io.BytesIO(blob)) as img:
        img.seek(0)
        frame = img.convert("RGBA")
        frame.thumbnail((640, 640), Image.LANCZOS)
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _parse_sticker_analysis(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("视觉模型没有返回 JSON 对象")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("视觉模型返回的不是 JSON 对象")
    params = _sticker_params(data)
    alias = str(data.get("alias") or "新表情").strip()[:16]
    if not alias or re.search(r"[\[\]<>\r\n\t/\\]", alias):
        alias = "新表情"
    return {"alias": alias, **params}


async def api_sticker_analyze(request: web.Request) -> web.StreamResponse:
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES + 64 * 1024:
        return _json({"ok": False, "error": "图片太大（上限 8 MB）"}, status=413)
    post = await request.post()
    field = post.get("file")
    if not isinstance(field, web.FileField):
        return _json({"ok": False, "error": "请选择一张表情图片"}, status=400)
    blob = field.file.read(MAX_UPLOAD_BYTES + 1)
    if len(blob) > MAX_UPLOAD_BYTES:
        return _json({"ok": False, "error": "图片太大（上限 8 MB）"}, status=413)
    try:
        await asyncio.to_thread(_validate_sticker_image, blob)
        data_url = await asyncio.to_thread(_sticker_analysis_data, blob)
    except Exception as exc:
        return _json({"ok": False, "error": f"图片校验失败：{exc}"}, status=400)
    ask = _CTX.get("ask_deepseek")
    if ask is None:
        return _json({"ok": False, "error": "当前实例没有可用的视觉模型"}, status=501)
    prompt = """你是表情包参数标注器。观察图片，只输出一个 JSON 对象，不要 Markdown，不要解释。
字段：alias（1–16字中文别名，简短自然）、category（只能从开心/难过/生气/惊讶/疑惑/无奈/嫌弃/害羞/鼓励/道歉/问候/其他中选一个）、emotion（16字内的具体情绪）、description（120字内客观描述画面与文字）、usage（120字内适合使用的聊天场景）、tags（最多6个短标签字符串数组）、confidence（0到1的识别置信度数字）。
不要臆测看不清的文字或人物身份；无法确定就降低 confidence，category 选其他。"""
    try:
        raw = await asyncio.wait_for(
            ask(prompt, [], image_urls=[data_url], memory_off=True, is_group=False),
            timeout=60,
        )
        analysis = _parse_sticker_analysis(raw)
    except Exception as exc:
        logger.warning("表情视觉分析失败: %s", type(exc).__name__)
        return _json({"ok": False, "error": "视觉模型暂时无法分析，请手动填写参数后保存"}, status=502)
    return _json({"ok": True, "analysis": analysis})


async def api_sticker_upload(request: web.Request) -> web.StreamResponse:
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES + 64 * 1024:
        return _json({"ok": False, "error": "图片太大（上限 8 MB）"}, status=413)
    post = await request.post()
    field = post.get("file")
    if not isinstance(field, web.FileField):
        return _json({"ok": False, "error": "请选择一张表情图片"}, status=400)
    alias = str(post.get("alias") or "").strip()
    if not alias or len(alias) > 16 or re.search(r"[\[\]<>\r\n\t/\\]", alias):
        return _json(
            {"ok": False, "error": "别名需为 1–16 个字符，不能包含方括号、尖括号、斜杠或换行"},
            status=400,
        )
    params = _sticker_params(post)
    blob = field.file.read(MAX_UPLOAD_BYTES + 1)
    if len(blob) > MAX_UPLOAD_BYTES:
        return _json({"ok": False, "error": "图片太大（上限 8 MB）"}, status=413)
    try:
        ext = await asyncio.to_thread(_validate_sticker_image, blob)
    except Exception as exc:
        return _json({"ok": False, "error": f"图片校验失败：{exc}"}, status=400)

    async with _STICKER_LOCK:
        mapping = _read_json(STICKER_FILE)
        if not isinstance(mapping, dict):
            return _json({"ok": False, "error": "stickers.json 缺失或不是合法 JSON 对象"}, status=400)
        if any(str(name).casefold() == alias.casefold() for name in mapping):
            return _json({"ok": False, "error": f"别名「{alias}」已存在，请换一个"}, status=400)

        STICKER_DIR.mkdir(parents=True, exist_ok=True)
        name = f"st-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.{ext}"
        path = STICKER_DIR / name
        path.write_bytes(blob)
        try:
            mapping[alias] = {"type": "image", "file": name, **params}
            backup_name = ""
            if STICKER_FILE.is_file():
                BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                backup = BACKUP_DIR / f"stickers.json.{stamp}.bak"
                shutil.copy2(STICKER_FILE, backup)
                backup_name = _display_path(backup)
            payload = json.dumps(mapping, ensure_ascii=False, indent=2) + "\n"
            temporary = STICKER_FILE.with_suffix(".json.tmp")
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, STICKER_FILE)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    logger.info("控制台添加表情 alias=%s file=%s bytes=%d backup=%s", alias, name, len(blob), backup_name or "无")
    return _json(
        {
            "ok": True,
            "item": {
                "alias": alias,
                "configured": True,
                "type": "image",
                "file": name,
                "exists": True,
                "size": len(blob),
                "url": f"/admin/api/stickers/{name}",
                **params,
            },
            "backup": backup_name,
        },
        status=201,
    )


async def api_sticker_file(request: web.Request) -> web.StreamResponse:
    name = Path(request.match_info["name"]).name
    sticker_dir = STICKER_DIR.resolve()
    path = (sticker_dir / name).resolve()
    if path.parent != sticker_dir or not path.is_file():
        raise web.HTTPNotFound()
    return web.Response(
        body=path.read_bytes(),
        content_type=_guess_image_type(path),
        headers={"Cache-Control": "max-age=60"},
    )


def _guess_image_type(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")


# --------------------------------------------------------------------------
# 提示词预览 / 试聊
# --------------------------------------------------------------------------
async def api_prompt(request: web.Request) -> web.StreamResponse:
    group_id = (request.query.get("group_id") or "").strip()
    heat = _on(_cfg(), "HEAT_MODE_ENABLED", False) and request.query.get("heat") == "1"
    build = _CTX.get("build_system_prompt")
    if build is None:
        return _json({"ok": False, "error": "bot 没有注入 build_system_prompt"}, status=501)
    memory_context = ""
    store = _CTX.get("group_memory")
    if group_id and store is not None:
        try:
            memory_context = store.context_for(group_id)
        except Exception:
            logger.exception("预览提示词时读取群记忆失败: %s", group_id)
            memory_context = ""
    prompt = build(memory_context, heat_mode=heat)
    return _json(
        {
            "ok": True,
            "prompt": prompt,
            "chars": len(prompt),
            "group_id": group_id,
            "memory_injected": bool(memory_context),
            "heat": heat,
        }
    )


def _flatten_segments(segments: Any) -> list[str]:
    """把 render_stickers 的输出压成给人看的形状，图片只报文件名。"""
    if isinstance(segments, str):
        return [segments] if segments.strip() else []
    out: list[str] = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        kind = segment.get("type")
        data = segment.get("data") or {}
        if kind == "text":
            text = str(data.get("text") or "")
            if text.strip():
                out.append(text)
        elif kind == "image":
            out.append(f"[图片] {Path(str(data.get('file') or '')).name}")
        elif kind == "face":
            out.append(f"[表情] id={data.get('id')}")
        elif kind == "mface":
            out.append(f"[表情包] {data.get('summary') or data.get('emoji_id')}")
    return out


async def api_chat(request: web.Request) -> web.StreamResponse:
    ask = _CTX.get("ask_deepseek")
    if ask is None:
        return _json({"ok": False, "error": "bot 没有注入 ask_deepseek"}, status=501)
    body = await _json_body(request)
    message = str(body.get("message") or "").strip()
    if not message:
        return _json({"ok": False, "error": "消息不能为空"}, status=400)
    if len(message) > 2000:
        return _json({"ok": False, "error": "消息太长（上限 2000 字）"}, status=400)

    history: list[dict[str, str]] = []
    for item in body.get("history") or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            history.append({"role": role, "content": content})
    history = history[-40:]

    mode = "group" if body.get("mode") == "group" else "private"
    group_id = str(body.get("group_id") or "").strip()
    sender = str(body.get("sender") or "").strip()
    heat = _on(_cfg(), "HEAT_MODE_ENABLED", False) and bool(body.get("heat")) and mode == "private"

    memory_context = ""
    memory_off = False
    store = _CTX.get("group_memory")
    if mode == "group" and group_id and store is not None:
        try:
            memory_context = store.context_for(group_id)
            if not memory_context:
                memory_off = not store.memory_enabled(group_id)
        except Exception:
            logger.exception("试聊读取群记忆失败: %s", group_id)

    started = time.perf_counter()
    # 只限并发不限排队的话，两路卡住时第三路会一直挂着转圈。给它一个明确的等待上限。
    try:
        await asyncio.wait_for(_CHAT_SLOTS.acquire(), timeout=CHAT_SLOT_WAIT_SECONDS)
    except (asyncio.TimeoutError, TimeoutError):
        return _json(
            {
                "ok": False,
                "error": f"试聊队列满了（同时最多 2 路），等了 {int(CHAT_SLOT_WAIT_SECONDS)} 秒还没轮上，稍后再试",
            },
            status=429,
        )
    try:
        answer = await ask(
            message,
            history,
            None,
            memory_context,
            current_sender_id=sender,
            time_hint="",
            roles=None,
            is_group=mode == "group",
            memory_off=memory_off,
            heat_mode=heat,
        )
    except Exception:
        logger.exception("试聊调用模型失败")
        return _json({"ok": False, "error": "模型调用失败，详情见服务端日志"}, status=502)
    finally:
        _CHAT_SLOTS.release()
    elapsed = time.perf_counter() - started

    split = _CTX.get("split_reply_messages")
    render = _CTX.get("render_stickers")
    segments = split(answer) if split else [answer]
    rendered: list[dict[str, Any]] = []
    for part in segments:
        shown = render(part) if render else part
        rendered.append({"raw": part, "shown": _flatten_segments(shown)})

    return _json(
        {
            "ok": True,
            "reply": answer,
            "elapsed": round(elapsed, 2),
            "segments": rendered,
            "chars": len(answer),
            "memory_injected": bool(memory_context),
            "heat": heat,
        }
    )

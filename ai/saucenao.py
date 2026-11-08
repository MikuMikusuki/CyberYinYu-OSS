"""SauceNAO 以图搜源：找出「这张图出自哪里」。

与 `#搜索`（百炼 image_search）的分工是刻意的，两者不要合并：

    #搜索  找「相似的图」——走百炼 Responses API，按次计费，返回图片列表
    #搜源  找「这张图的出处处」——走 SauceNAO search.php，返回作品名/作者/原站链接

⚠️ 网络事实（2026-10-25 在生产机上逐条实测，**改代码前务必先读**）：

    GET  /search.php?output_type=2         -> 403 + cf-mitigated: challenge
    POST /search.php（multipart/form-data） -> 穿过 Cloudflare，到达应用层
    匿名 / 伪造 api_key                     -> {"status":-1,"message":"The anonymous
                                               account type does not permit API usage."}

    结论：必须 **POST + multipart + 真实 api_key**，三条缺一不可。
    不要「优化」成 GET + url= 参数去省一次上传——那条路在数据中心出口上会被
    Cloudflare 挡死，而且报的是 403 而不是 SauceNAO 的错误，很难查。

为什么上传字节而不是交给 SauceNAO 抓 URL：
    QQ 图片是 gchat.qpic.cn 的临时签名链接，带时效、可能校验 Referer，
    让 SauceNAO 自己去抓经常拿到 403/空图。本地下载完再上传是唯一稳的路径，
    顺带也避免把群友的图片 URL 泄露给第三方。

⚠️ `aiohttp` 的 `response.content.read(n)` 不是「读满 n 字节」
（2026-10-25 实测，这是「API 结果一个都对不上」的真凶）：
    同一张 474,569 字节的 JPEG，`read(8MB)` 只返回缓冲区已有的 **16,384** 字节
    （另一次 114,688 字节）。截断的 JPEG 仍能解码成「只剩上半截的图」，于是
    SauceNAO 拿着半张图去比，**93.61% 掉到 67.0%**，而且不报任何错；截断的 PNG
    连解码都过不了，直接回 HTTP 500。所以下载一律走 `_read_capped()`（分块读满）。

配图同理：`header.thumbnail` 是 61x200 / 104x150 的匹配条、几百字节到 1.3 KB，
当结果图发出去等于没发。真图走 `fetch_preview()`。
"""

import base64
import binascii
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

try:  # Pillow 是可选的：没有它也能跑，只是不做转码/缩放
    from PIL import Image as _PILImage

    _HAS_PIL = True
except ImportError:  # pragma: no cover - 取决于部署环境
    _PILImage = None
    _HAS_PIL = False

logger = logging.getLogger("cyberyinyu")

SEARCH_URL = "https://saucenao.com/search.php"
# QQ 图片极少超过 2MB；8MB 是个既不会误杀、又不会被 SauceNAO 拒绝的上限。
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# 传给 SauceNAO 之前的归一化阈值。超过就缩放/压缩，原因见 normalize_image()。
_MAX_EDGE = 4000
_MAX_UPLOAD_BYTES = 4 * 1024 * 1024
# 单条 QQ 消息的字数上限，超了拆多条（与 #记忆 / #ds搜 的做法一致）。
MESSAGE_CHARS = 600
# 相似度低于这个值就标「低置信」。SauceNAO 的冷门图常给出 60-70% 的构图相似
# 命中（同一角色不同画、同色调不同人物），当作定论会自信地报错角色。
LOW_CONFIDENCE = 75.0

# 主备 key 的冷却时长。SauceNAO 上两个 key 是两个独立账号，各自有 100 次/天
# 和 30 秒 4 次，所以换 key 不只是容错，可用额度也翻倍。
_INVALID_KEY_COOLDOWN = 30 * 60.0  # key 不被识别：半小时后再试一次
_EXHAUSTED_COOLDOWN = 6 * 60 * 60.0  # 当日额度用完：按小时级重试
_RATE_COOLDOWN = 60.0  # 撞 30 秒限速：等一分钟

# SauceNAO 的 `data` 字段随索引而变：Pixiv 给 member_name，Danbooru 给 creator，
# Twitter 给 author_name……所以一律「有什么取什么」，缺字段不算错。
_TITLE_KEYS = ("title", "jp_name", "source", "material", "eng_name")
_AUTHOR_KEYS = ("author_name", "member_name", "creator", "artist_name", "user_name")

# 索引专用 ID → 站点名。只用于在标题缺失时补一句「来自哪个站」。
_ID_KEYS: tuple[tuple[str, str], ...] = (
    ("pixiv_id", "Pixiv"),
    ("danbooru_id", "Danbooru"),
    ("gelbooru_id", "Gelbooru"),
    ("sankaku_id", "Sankaku"),
    ("yandere_id", "yande.re"),
    ("konachan_id", "Konachan"),
    ("e621_id", "e621"),
    ("nijie_id", "Nijie"),
    ("seiga_id", "Nico Seiga"),
    ("anime_pictures_id", "Anime-Pictures"),
    ("deviantart_id", "deviantArt"),
    ("artstation_id", "ArtStation"),
    ("twitter_id", "Twitter"),
    ("bcy_id", "bcy.net"),
    ("pawoo_id", "Pawoo"),
    ("fakku_id", "FAKKU"),
)


class SauceNaoError(Exception):
    """Any failure that should be reported to the user in their own words."""

    def __init__(self, message: str, *, status: int = 0, raw: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.raw = raw

    @property
    def is_quota(self) -> bool:
        return self.status == -1 or self.status == -2

    @property
    def key_related(self) -> bool:
        """True when retrying with another key could plausibly work.

        Only key identity and that account's quota fall in here. "图片太大" or
        a transport failure are not key problems — trying the backup key would
        burn a request and return the same error.
        """
        lowered = (self.raw or "").casefold()
        if any(
            marker in lowered
            for marker in ("anonymous", "api key", "daily", "rate", "limit", "quota")
        ):
            return True
        return self.status in {-1, -2, -3}

    @property
    def cooldown_seconds(self) -> float:
        """How long to leave this key alone before trying it again."""
        lowered = (self.raw or "").casefold()
        # 限速排在额度前面：SauceNAO 的限速文案里也带 "limit"。
        if "rate" in lowered or "too many" in lowered:
            return _RATE_COOLDOWN
        if "daily" in lowered or "quota" in lowered:
            return _EXHAUSTED_COOLDOWN
        return _INVALID_KEY_COOLDOWN

    def user_message(self) -> str:
        return str(self)


# --- 多 key 故障转移 ------------------------------------------------------

class KeyRing:
    """按顺序尝试多个 API key，失败的进冷却。

    冷却中的 key 不是永久停用——它排在队尾兜底。否则两个 key 同时冷却时功能
    会整个停摆，而其中很可能只是临时抖动。
    """

    def __init__(self, keys: list[str]) -> None:
        self._keys = [key for key in keys if key]
        self._cool_until = [0.0] * len(self._keys)

    def __bool__(self) -> bool:
        return bool(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def order(self) -> list[tuple[int, str]]:
        """尝试顺序：可用的按原序在前，冷却中的按原序垫底。"""
        now = time.monotonic()
        ready = [(i, k) for i, k in enumerate(self._keys) if self._cool_until[i] <= now]
        cooling = [(i, k) for i, k in enumerate(self._keys) if self._cool_until[i] > now]
        return ready + cooling

    def cool_down(self, index: int, seconds: float) -> None:
        if 0 <= index < len(self._cool_until):
            self._cool_until[index] = time.monotonic() + seconds

    def revive(self, index: int) -> None:
        if 0 <= index < len(self._cool_until):
            self._cool_until[index] = 0.0

    def describe(self) -> str:
        now = time.monotonic()
        states = []
        for index, until in enumerate(self._cool_until):
            if until <= now:
                states.append(f"#{index} 可用")
            else:
                states.append(f"#{index} 冷却中（{int(until - now)}s）")
        return " / ".join(states)


# key 列表 -> KeyRing。状态要跨请求保留，所以按 key 组合缓存；
# 改配置重启后组合变了，自然拿到新的 ring。
_rings: dict[tuple[str, ...], KeyRing] = {}


def _ring_for(keys: list[str]) -> KeyRing:
    signature = tuple(keys)
    ring = _rings.get(signature)
    if ring is None:
        ring = KeyRing(keys)
        _rings[signature] = ring
    return ring


# --- 配置与可用性 ---------------------------------------------------------

def _key_list(api_keys: Any) -> list[str]:
    if isinstance(api_keys, str):
        return [api_keys] if api_keys.strip() else []
    return [str(key) for key in (api_keys or []) if str(key).strip()]


def availability(enabled: bool, api_keys: Any) -> tuple[bool, str]:
    """Return (usable, reason_when_not)."""
    if not enabled:
        return False, "搜源功能已关闭（SAUCENAO_ENABLED=0）。"
    if not _key_list(api_keys):
        return False, (
            "没配 SAUCENAO_API_KEY。SauceNAO 不允许匿名调用 API（实测返回 "
            "「The anonymous account type does not permit API usage」），"
            "要去 saucenao.com 注册后拿 key 填进 otowa.env。"
        )
    return True, ""


def _redacted_key(api_key: str) -> str:
    return f"{api_key[:4]}****{api_key[-4:]}" if len(api_key) >= 8 else "（过短）"


def describe(enabled: bool, api_keys: Any) -> str:
    keys = _key_list(api_keys)
    usable, reason = availability(enabled, api_keys)
    lines = ["SauceNAO 搜源："]
    if not usable:
        lines.append(f"  ⚠️ 不可用：{reason}")
    lines.append(f"  启用: {'是' if enabled else '否'}")
    lines.append(f"  端点: {SEARCH_URL}（POST multipart）")
    if keys:
        lines.append(f"  key : {len(keys)} 个")
        for index, key in enumerate(keys):
            role = "主" if index == 0 else "备"
            lines.append(f"        #{index}（{role}）{_redacted_key(key)}")
        lines.append(f"  状态: {_ring_for(keys).describe()}")
    else:
        lines.append("  key : （未设置）")
    return "\n".join(lines)


# --- 图片预处理 -----------------------------------------------------------

def _sniff_extension(data: bytes) -> tuple[str, str]:
    """Return (extension, mime) from magic bytes, or raise when it is not an image.

    SauceNAO 靠内容识别格式，但 multipart 的 filename 仍影响它的初步判断，
    所以按真实字节给扩展名，而不是一律 image.jpg。RIFF 单独判 WEBP：
    AVI/WAV 也是 RIFF 头，直接放行会把视频当成图片发出去。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if data.startswith(b"BM"):
        return ".bmp", "image/bmp"
    raise SauceNaoError(
        "这不像是图片（无法识别文件头）。SauceNAO 只收 png/jpg/gif/webp/bmp。"
    )


def normalize_image(data: bytes) -> tuple[bytes, str, str]:
    """Turn the upload into something SauceNAO can definitely decode.

    实测（2026-10-25，同一群连续三次）：干净的小 png 走得很顺，手机截图那类
    图片直接回 **HTTP 500 + 一张 HTML 错误页**，不是 JSON、也不是 CF 挑战。
    触发条件通常是这三个：

      * webp：QQ 会自动把图片转成 webp 再发，SauceNAO 对它支持不稳；
      * 超长截图：手机长截图动辄 1080×12000，解码器直接崩；
      * 几 MB 的原图：超出它的处理预算。

    所以：非 png/jpg 一律转码、长边超过 4000 缩放、超过 4 MB 压缩。
    png/jpg 且尺寸体积都正常时**原样上传**——重新编码只会糊掉视觉指纹，
    反而降低命中率。

    Pillow 缺失时退化为「原样上传」，不会因此炸掉整个功能。
    """
    extension, mime = validate_image(data)
    if not _HAS_PIL:
        return data, extension, mime

    try:
        with _PILImage.open(io.BytesIO(data)) as image:
            width, height = image.size
            needs_transcode = extension not in {".png", ".jpg"}
            needs_resize = max(width, height) > _MAX_EDGE
            needs_shrink = len(data) > _MAX_UPLOAD_BYTES
            if not (needs_transcode or needs_resize or needs_shrink):
                return data, extension, mime

            if needs_resize:
                image.thumbnail((_MAX_EDGE, _MAX_EDGE), _PILImage.LANCZOS)
            buffer = io.BytesIO()
            if extension == ".png" and not needs_transcode:
                image.save(buffer, "PNG", optimize=True)
                converted, out_extension, out_mime = buffer.getvalue(), ".png", "image/png"
            else:
                image.convert("RGB").save(buffer, "JPEG", quality=92, optimize=True)
                converted, out_extension, out_mime = buffer.getvalue(), ".jpg", "image/jpeg"
    except Exception:
        logger.warning("搜源图片归一化失败，按原样上传", exc_info=True)
        return data, extension, mime

    logger.info(
        "搜源图片已归一化: %dx%d %s %d 字节 → %s %d 字节",
        width,
        height,
        extension,
        len(data),
        out_extension,
        len(converted),
    )
    return converted, out_extension, out_mime


def validate_image(data: bytes) -> tuple[str, str]:
    """Raise a user-facing error when the bytes cannot possibly be an image."""
    if not data:
        raise SauceNaoError("图片是空的，没东西可搜。")
    if len(data) > MAX_IMAGE_BYTES:
        raise SauceNaoError(
            f"图片太大了（{len(data) / 1024 / 1024:.1f} MB，上限 8 MB），发小一点的。"
        )
    return _sniff_extension(data)


# --- 结果配图 -------------------------------------------------------------

# `header.thumbnail` 不是"图"，是匹配条：实测 61x200 / 104x150 / 200x112，
# 876–1,369 字节，在群里显示出来就是一条模糊的色块，还容易被 QQ 压成加载失败。
# 所以按命中 ID 去拿原图，拿不到就不发——宁缺毋滥。
#
# 为什么只有 Pixiv 有：生产机（国内 VPS）实测 pixiv.net / i.pximg.net /
# danbooru / gelbooru / konachan / twitter 全部超时，只有 pixiv.re 这个镜像
# 能通（实测 90541510 → 200，4,108,889 字节 3000x4500）。其它站点一律不发图。
PREVIEW_MAX_EDGE = 900
PREVIEW_QUALITY = 82
# 小于这个体积的图在群里照样是"糊的"，没有发的价值（900px 的线稿一般也有几十 KB，
# 4 KB 以下基本是纯色块）。
PREVIEW_MIN_BYTES = 4 * 1024
PREVIEW_MAX_DOWNLOAD = 16 * 1024 * 1024
# 发出去的预览图上限：base64 之后约 1.33 倍，超过这个就别再往上顶了。
PREVIEW_MAX_BYTES = 400 * 1024
_PIXIV_MIRRORS = ("https://pixiv.re/{id}.png", "https://pixiv.re/{id}.jpg")


async def fetch_preview(session: Any, hit: "SauceHit", *, timeout: float = 25.0) -> bytes | None:
    """Return a preview worth looking at, or None.

    None is a normal outcome, not an error: the caller just sends no image and
    the text result (which already carries the source link) stands on its own.
    """
    import aiohttp

    if not hit.pixiv_id:
        logger.info("搜源配图跳过: 「%s」没有 Pixiv ID", hit.label[:40])
        return None

    for template in _PIXIV_MIRRORS:
        url = template.format(id=hit.pixiv_id)
        try:
            async with session.get(url, headers=_IMAGE_HEADERS, timeout=timeout) as response:
                if response.status != 200:
                    logger.info("搜源配图 %s 返回 HTTP %s", url, response.status)
                    continue
                blob = await _read_capped(response, PREVIEW_MAX_DOWNLOAD)
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.info("搜源配图下载失败 %s: %s", url, type(exc).__name__)
            continue
        except SauceNaoError as exc:
            logger.info("搜源配图放弃 %s: %s", url, exc)
            continue

        preview = shrink_preview(blob)
        if preview:
            logger.info(
                "搜源配图: pixiv%s %d 字节 → %d 字节", hit.pixiv_id, len(blob), len(preview)
            )
            return preview
    return None


def shrink_preview(blob: bytes) -> bytes | None:
    """Fit the artwork into a QQ-sized JPEG; None when it is not worth sending."""
    if not _HAS_PIL:
        # 没有 Pillow 时只能原样发，但原图动辄 4 MB，太大就别发了。
        return blob if PREVIEW_MIN_BYTES <= len(blob) <= 512 * 1024 else None

    try:
        with _PILImage.open(io.BytesIO(blob)) as image:
            image.load()
            width, height = image.size
            if max(width, height) < 200:
                return None
            image.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), _PILImage.LANCZOS)
            if image.mode not in {"RGB", "L"}:
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=PREVIEW_QUALITY, optimize=True)
            shrunk = buffer.getvalue()
            if len(shrunk) > PREVIEW_MAX_BYTES:
                # 细节极多的图 900px 也可能几百 KB，base64 进 WS 会撑大一条消息，
                # 降一档再试；这一档只影响体积，不影响「能不能看清是哪张」。
                buffer = io.BytesIO()
                image.thumbnail((700, 700), _PILImage.LANCZOS)
                image.save(buffer, "JPEG", quality=72, optimize=True)
                shrunk = buffer.getvalue()
    except Exception:  # noqa: BLE001 - 解码/转码失败只是少一张图
        logger.warning("搜源配图转码失败，跳过", exc_info=True)
        return None

    if len(shrunk) < PREVIEW_MIN_BYTES:
        return None
    return shrunk


# --- HTTP -----------------------------------------------------------------

# QQ 图片 CDN 对空 UA 会挑刺，带一个正常浏览器 UA 最省事。
_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "image/*,*/*;q=0.8",
}


async def load_image(session: Any, url: str, *, timeout: float = 20.0) -> bytes:
    """Fetch the bytes behind a OneBot image reference.

    Three shapes reach us from NapCat:
      * `https://gchat.qpic.cn/...` — QQ CDN, signed and short-lived
      * `data:image/png;base64,...` — inline image
      * `base64://...` — already normalised upstream, but tolerated here too
    """
    import aiohttp

    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        if ";base64" not in header:
            raise SauceNaoError("图片是 data URL 但不是 base64，读不了。")
        try:
            return base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SauceNaoError("图片 base64 解不开，重发一次图。") from exc

    if url.startswith("base64://"):
        try:
            return base64.b64decode(url[len("base64://") :], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SauceNaoError("图片 base64 解不开，重发一次图。") from exc

    if not url.startswith(("http://", "https://")):
        raise SauceNaoError("图片地址不是 http(s)，下载不了。")

    try:
        async with session.get(url, headers=_IMAGE_HEADERS, timeout=timeout) as response:
            if response.status != 200:
                raise SauceNaoError(
                    f"下载图片失败（HTTP {response.status}）。QQ 的图链会过期，重发一次图。"
                )
            data = await _read_capped(response, MAX_IMAGE_BYTES)
    except aiohttp.ClientError as exc:
        raise SauceNaoError(f"下载图片失败：{type(exc).__name__}。") from exc

    verify_complete(data)
    return data


async def _read_capped(response: Any, limit: int) -> bytes:
    """Read the whole body, refusing to buffer more than `limit` bytes.

    ⚠️ 不要换回 `await response.content.read(limit)`。`StreamReader.read(n)` 只
    返回**缓冲区里现在就有的数据**，不会等满 n 字节：同一张 474,569 字节的 JPEG
    实测只拿到 16,384 / 114,688 字节，截断后仍能解码 → 相似度从 93.61% 静默掉到
    67.0%；截断的 PNG 则直接让 SauceNAO 回 HTTP 500。这是 2026-10-25「API 结果
    一个都对不上、网页有 93%」的根因，别再"优化"回去。
    """
    buffer = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise SauceNaoError(f"图片太大（上限 {limit // 1024} KB），先压一下再发。")
    if not buffer:
        raise SauceNaoError("图片下载回来是空的，重发一次图。")
    return bytes(buffer)


def verify_complete(blob: bytes) -> None:
    """Reject a half-downloaded image before it reaches SauceNAO.

    Pillow 把「文件被截断」报成 `OSError: image file is truncated`。不检查的话，
    残图会被当成合法图片上传，相似度悄悄掉下去，日志里一点痕迹都没有——这正是
    最难查的那类失败。
    """
    if not _HAS_PIL:
        return
    try:
        with _PILImage.open(io.BytesIO(blob)) as image:
            image.load()
    except Exception as exc:  # noqa: BLE001 - 任何解码异常都意味着字节不可用
        raise SauceNaoError(
            f"图片下载不完整（{type(exc).__name__}），重发一次图再试。"
        ) from exc


async def search(
    session: Any,
    image: bytes,
    *,
    api_keys: Any,
    db: int = 999,
    minsim: int = 60,
    numres: int = 3,
    hide: int = 0,
    timeout: float = 30.0,
) -> "SauceResult":
    """Try each configured key in turn until one answers.

    Key #0 is the primary; the rest are backups. A key that fails for a key-level
    reason (not recognised, out of quota, rate-limited) goes into a cooldown so
    the next request does not waste a round-trip on it, and the next key is tried
    immediately. Errors that have nothing to do with the key — oversized image,
    unreadable bytes, transport failure — are raised as-is.
    """
    keys = _key_list(api_keys)
    if not keys:
        raise SauceNaoError("没有可用的 SauceNAO API key。")

    image, extension, mime = normalize_image(image)
    ring = _ring_for(keys)
    last_error: SauceNaoError | None = None

    for index, key in ring.order():
        try:
            result = await _search_once(
                session,
                image,
                extension=extension,
                mime=mime,
                api_key=key,
                db=db,
                minsim=minsim,
                numres=numres,
                hide=hide,
                timeout=timeout,
            )
        except SauceNaoError as exc:
            if not exc.key_related:
                raise
            ring.cool_down(index, exc.cooldown_seconds)
            last_error = exc
            logger.warning(
                "SauceNAO key #%d 不可用（%ss 后重试），切换下一个: %s",
                index,
                int(exc.cooldown_seconds),
                exc,
            )
            continue
        ring.revive(index)
        result.key_index = index
        if index != 0:
            logger.info("SauceNAO 已切到备用 key #%d 成功", index)
        return result

    raise last_error or SauceNaoError("所有 SauceNAO key 都不可用，稍后再试。")


async def _search_once(
    session: Any,
    image: bytes,
    *,
    extension: str,
    mime: str,
    api_key: str,
    db: int,
    minsim: int,
    numres: int,
    hide: int,
    timeout: float,
) -> "SauceResult":
    """One request with one key.

    Two behaviours of the API shape this, both measured on the production host
    rather than assumed:

    * **Query-string parameters only.** `db` / `minsim` / `numres` / `hide`
      sent as multipart form fields are silently ignored — `results_requested`
      comes back as 8 (the web default) no matter what was posted. Putting them
      in the URL works. `api_key` and `file` do have to be in the body, which is
      also where we want the key: it never lands in a URL or an access log.
    * **`minsim` does not filter.** With `minsim=60` the API still returns
      54.7% hits, so the threshold is applied here instead. That is also why we
      ask for more candidates than the caller wants — filtering would otherwise
      leave fewer than `numres` results.
    """
    # 多要几条候选，过滤后还能剩够 numres 条。上限 40 是 SauceNAO 的硬顶。
    request_count = min(40, max(numres, numres * 3))
    params = {
        "output_type": "2",
        "db": str(db),
        "minsim": str(minsim),
        "numres": str(request_count),
        "hide": str(hide),
    }
    form = _build_form(image=image, extension=extension, mime=mime, api_key=api_key)
    logger.info("SauceNAO 上传: %d 字节 %s", len(image), mime)
    headers = {
        # 不带浏览器 UA 时 CF 的成功率明显更低；这里不是伪装，是让 POST 稳定落地。
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    async with session.post(
        SEARCH_URL, params=params, data=form, headers=headers, timeout=timeout
    ) as response:
        raw = await response.text()
        status = response.status

    if not raw.strip():
        raise SauceNaoError(
            f"SauceNAO 返回了空响应（HTTP {status}），可能被 Cloudflare 拦了，过会儿再试。"
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        # 两种可能：Cloudflare 的挑战页，或 SauceNAO 自己 500 的 HTML 错误页。
        head = _html_summary(raw)
        if "Just a moment" in raw or "cf-mitigated" in raw:
            raise SauceNaoError(
                "又被 Cloudflare 挑战了（返回的是挑战页，不是 JSON）。"
                "如果是刚改过请求方式，检查是否还保持 POST + multipart。"
            ) from None
        logger.warning(
            "SauceNAO 返回非 JSON: status=%s 长度=%d 摘要=%s", status, len(raw), head
        )
        if status >= 500:
            raise SauceNaoError(
                f"SauceNAO 处理这张图时出错了（HTTP {status}），换一张图再试。"
                f"（{head[:160]}）"
            ) from None
        raise SauceNaoError(f"SauceNAO 返回异常（HTTP {status}）：{head[:160]}") from None

    if not isinstance(payload, dict):
        raise SauceNaoError("SauceNAO 返回结构异常。")

    header = payload.get("header") or {}
    code = _as_int(header.get("status"), 0)
    message = str(header.get("message") or "").strip()
    if code < 0:
        logger.warning("SauceNAO 拒绝请求: status=%s message=%s", code, message[:160])
        raise SauceNaoError(_explain(code, message), status=code, raw=message)

    hits = [
        hit for hit in _parse_hits(payload.get("results") or []) if hit.similarity >= minsim
    ][:numres]
    result = SauceResult(
        hits=hits,
        returned=_as_int(header.get("results_returned"), len(hits)),
        requested=numres,
        short_remaining=_as_int(header.get("short_remaining"), -1),
        long_remaining=_as_int(header.get("long_remaining"), -1),
    )
    logger.info(
        "SauceNAO 搜源完成: 候选 %d 条 → 过 %d%% 阈值后 %d 条（请求 %d），30秒余量=%s 当日余量=%s",
        len(payload.get("results") or []),
        minsim,
        len(hits),
        numres,
        result.short_remaining,
        result.long_remaining,
    )
    return result


def _build_form(
    *,
    image: bytes,
    extension: str,
    mime: str,
    api_key: str,
) -> Any:
    """Build the multipart body: only the key and the file live here.

    Every tuning parameter goes in the query string instead — see the note in
    `search()` for why. Keeping `api_key` in the body also keeps it out of URLs.
    """
    import aiohttp

    form = aiohttp.FormData()
    form.add_field("api_key", api_key)
    form.add_field(
        "file",
        image,
        filename=f"image{extension}",
        content_type=mime,
    )
    return form


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def _html_summary(raw: str, limit: int = 240) -> str:
    """Pull the readable sentence out of an HTML error page.

    SauceNAO 的错误页开头是一大段 DOCTYPE 和样式，直接截前 120 字符只会截到
    样板文字——第一次接的时候就是这么把真正的报错挡掉的。
    """
    text = _BLOCK_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    return " ".join(text.split())[:limit]


def _explain(status: int, message: str) -> str:
    """Turn SauceNAO's status/message pair into something a group member can act on."""
    lowered = message.casefold()
    if "anonymous" in lowered:
        return (
            "SauceNAO 把这次请求当匿名处理了，key 没被识别。"
            "去 saucenao.com/user.php?page=search-api 重新复制一次 API Key"
            "（复制时容易带空格，或旧 key 已被重置）。"
        )
    if "daily" in lowered and "limit" in lowered:
        return "SauceNAO 今日额度用完了，明天再搜。"
    if "rate" in lowered or "too many" in lowered:
        return "搜得太快了，SauceNAO 限制每 30 秒几次，等一会儿再发。"
    if "api key" in lowered or "invalid" in lowered:
        return "SauceNAO 的 API key 无效，检查 otowa.env 里的 SAUCENAO_API_KEY。"
    if "image" in lowered and ("size" in lowered or "large" in lowered):
        return "图片太大或格式不支持，换张小的再试。"
    if message:
        return f"SauceNAO 拒绝了这次查询（status={status}）：{message[:120]}"
    return f"SauceNAO 拒绝了这次查询（status={status}）。"


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _first_text(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _urls(data: dict[str, Any]) -> list[str]:
    raw = data.get("ext_urls")
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


# Danbooru 系索引（danbooru / gelbooru / yande.re / konachan / sankaku …）的
# `data.characters` 是逗号分隔的下划线 tag：`hatsune_miku, kagamine_rin`。
# 别的索引（Pixiv / Twitter / Anime）根本没有这个字段——所以「认得出是谁」
# 只发生在 booru 系命中上，不能因为缺字段就当解析失败。
_SMALL_TAG_WORDS = {"of", "the", "and", "in", "no", "a", "to"}


def _pretty_tag(raw: str) -> str:
    """`hatsune_miku` -> `Hatsune Miku`；保留 `_(fate)` 这类作品后缀。"""
    text = " ".join(raw.replace("_", " ").split())
    if not text:
        return ""
    words = []
    for word in text.split(" "):
        lowered = word.casefold()
        if lowered in _SMALL_TAG_WORDS:
            words.append(lowered)
        elif word[:1].islower():
            words.append(word[:1].upper() + word[1:])
        else:
            words.append(word)
    return " ".join(words)


def _characters(data: dict[str, Any]) -> list[str]:
    """Parse `characters` in either shape the API uses (string or list)."""
    raw = data.get("characters")
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, list):
        items = [str(item) for item in raw]
    else:
        return []
    found: list[str] = []
    for item in items:
        tag = _pretty_tag(item)
        # SauceNAO 偶尔给 `original` / 空串这类占位，不是角色名。
        if tag and tag.casefold() not in {"original", "unknown"} and tag not in found:
            found.append(tag)
    return found[:4]


# SauceNAO 把索引名和命中文件名拼在一起返回：
#   "Index #5: Pixiv Images - 55087485_p0_master1200.jpg"
# 直接显示会带一串没用的文件名，所以在渲染前拆掉。
_INDEX_PREFIX = re.compile(r"^Index #\d+:\s*")


def _clean_index_name(raw: str) -> str:
    text = _INDEX_PREFIX.sub("", raw.strip())
    head, separator, tail = text.partition(" - ")
    # 只在后半段像文件名（带扩展名）时才截断，避免误伤本来就含 " - " 的站名。
    if separator and "." in tail:
        text = head
    return text.strip()


def _ids(data: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key, label in _ID_KEYS:
        value = data.get(key)
        if value in (None, "", 0):
            continue
        found.append(f"{label} {value}")
    return found


@dataclass
class SauceHit:
    similarity: float = 0.0
    index_id: int = 0
    index_name: str = ""
    title: str = ""
    author: str = ""
    urls: list[str] = field(default_factory=list)
    ids: list[str] = field(default_factory=list)
    thumbnail: str = ""
    # 只有 Pixiv 命中能拿到「看得清的原图」（见 fetch_preview），单独存一份方便判断。
    pixiv_id: str = ""
    # booru 系索引才有：角色 tag（已转成可读形式）与作品名。
    # Pixiv / Twitter / Anime 索引里这两个字段不存在，留空是常态，不是错误。
    characters: list[str] = field(default_factory=list)
    material: str = ""

    @property
    def label(self) -> str:
        """Best available one-line name for this hit."""
        if self.characters:
            return " / ".join(self.characters)
        if self.title:
            return self.title
        if self.ids:
            return self.ids[0]
        return self.index_name or "（无标题）"

    @property
    def low_confidence(self) -> bool:
        return self.similarity < LOW_CONFIDENCE


@dataclass
class SauceResult:
    hits: list[SauceHit] = field(default_factory=list)
    returned: int = 0
    requested: int = 0
    short_remaining: int = -1
    long_remaining: int = -1
    # 这次结果是哪个 key 跑出来的（0 = 主，1 = 备）。日志和排障用。
    key_index: int = 0

    @property
    def empty(self) -> bool:
        return not self.hits


def _parse_hits(results: list[Any]) -> list[SauceHit]:
    """Parse + de-duplicate.

    The same artwork often appears in several indexes at once (Pixiv + Danbooru
    + a booru mirror). Keeping all of them just floods the chat with the same
    picture, so a hit is dropped when its top URL was already seen.

    `characters` is the only field that actually names a person: SauceNAO never
    returns it for Pixiv / Twitter / Anime indexes, so those hits carry the work
    title and nothing else. Dropping it (the original behaviour) left the bot
    able to say "出自 Vocaloid" but unable to answer "这是谁".
    """
    hits: list[SauceHit] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        header = item.get("header") or {}
        data = item.get("data") or {}
        if not isinstance(data, dict):
            continue
        urls = _urls(data)
        key = urls[0] if urls else f"{header.get('index_id')}:{_first_text(data, _TITLE_KEYS)}"
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            SauceHit(
                similarity=_as_float(header.get("similarity"), 0.0),
                index_id=_as_int(header.get("index_id"), 0),
                index_name=_clean_index_name(str(header.get("index_name") or "")),
                title=_first_text(data, _TITLE_KEYS),
                author=_first_text(data, _AUTHOR_KEYS),
                urls=urls,
                ids=_ids(data),
                thumbnail=str(header.get("thumbnail") or "").strip(),
                pixiv_id=str(data.get("pixiv_id") or "").strip(),
                characters=_characters(data),
                material=_first_text(data, ("material",)),
            )
        )
    return hits


# --- 排版 -----------------------------------------------------------------

def _chunk(lines: list[str], limit: int = MESSAGE_CHARS) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def format_result(result: SauceResult, *, prefix: str = "") -> list[str]:
    """Render for a plain-text QQ message. No markdown — QQ shows it verbatim."""
    head = prefix or "SauceNAO"
    if result.empty:
        return [f"{head} 没认出这张图。可能不在它的索引里，或者裁得太碎。"]

    lines = [f"{head} 找到 {len(result.hits)} 条"]
    for index, hit in enumerate(result.hits, 1):
        score = f"{hit.similarity:.1f}%"
        site = hit.index_name or "未知来源"
        mark = "（低置信）" if hit.low_confidence else ""
        lines.append(f"{index}. {site} · {score}{mark}")
        # 角色名排在最前面：群里问「这是谁」，这一行才是答案。
        if hit.characters:
            lines.append(f"   角色：{' / '.join(hit.characters)}")
        # material 也在 _TITLE_KEYS 里，所以 booru 命中时 title 常常就是它——
        # 这时只渲染成「作品」一行，别再重复一次同样的字符串。
        work = _pretty_tag(hit.material) if hit.material else ""
        title_is_work = bool(work) and hit.title.casefold() == hit.material.casefold()
        if work:
            lines.append(f"   作品：{work}")
        if hit.title and not title_is_work:
            lines.append(f"   {hit.title}")
        if hit.author:
            lines.append(f"   作者：{hit.author}")
        if hit.ids and not hit.title:
            # 没有标题时，索引自带的编号是唯一能指认作品的东西。
            lines.append(f"   {' / '.join(hit.ids[:2])}")
        for url in hit.urls[:2]:
            lines.append(f"   {url}")

    # 全都没过阈值时点明这是参考而不是结论，避免「自信地认错」。
    if all(hit.low_confidence for hit in result.hits):
        lines.append("（这几条相似度都不高，当参考看，别当准的）")
    if result.long_remaining >= 0:
        lines.append(f"（今日还剩 {result.long_remaining} 次）")
    return _chunk(lines)


def best_similarity(result: SauceResult) -> float:
    return max((hit.similarity for hit in result.hits), default=0.0)

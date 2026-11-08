"""Text-to-image on a third, isolated provider lane (`#画图`).

The bot now talks to three different vendors, and they are deliberately kept
apart: chat goes to DeepSeek, image *search* goes to Bailian, image
*generation* goes to this OpenAI-compatible gateway. Sharing a key or a base
URL between any two of them has already broken a feature once (see the
`IMAGE_SEARCH_*` block in config.py), so this module only ever reads
`IMAGE_GEN_*`.

What the gateway actually returns was probed, not assumed:
  * `model=gpt-image-2`, `size=1024x1024`, `n=1`  -> ~20s, HTTP 200
  * 2026-10-29 换成 `gpt-image-2.5` 后复测：generate 17–22s、edit 30–33s，
    返回形状不变（仍只有 `data[0].url`），`input_fidelity` 与多图都照旧接受；
    **唯一变化是 `size` 开始生效**——`auto` 给了 1024x1536，而 2.x 一律回方图。
  * `data[0].url` populated, `data[0].b64_json` empty
  * the PNG behind that URL is ~3.7 MB
So both shapes are accepted here (a gateway that flips to b64_json must not
break the command), and the image is always re-encoded before sending.
"""

import base64
import io
import logging
import re
import time
from dataclasses import dataclass

try:  # Pillow 是可选的：没有它也能发图，只是不压缩
    from PIL import Image as _PILImage

    _HAS_PIL = True
except ImportError:  # pragma: no cover - 取决于部署环境
    _PILImage = None
    _HAS_PIL = False

from openai import (
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    RateLimitError,
)

logger = logging.getLogger("cyberyinyu")

# 网关文档给的四个取值。写错时不要原样发过去（那是 400），退回默认。
SUPPORTED_SIZES = ("1024x1024", "1536x1024", "1024x1536", "auto")
DEFAULT_SIZE = "1024x1024"

# 生成图是 3-4 MB 的 PNG。QQ 图片消息本来也会重编码，先压成 JPEG 再发：
# 实测 3.68 MB -> 约 250 KB，上传快一个数量级，肉眼分不出差别。
JPEG_QUALITY = 92
# 下载上限。3.7 MB 是常态，25 MB 足够容忍网关哪天给个 4K 图，又不至于被
# 一个坏地址拖死整个命令。
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024

# --- 图像编辑（#改图）----------------------------------------------------
#
# 实测这个网关的 images/edits：1254x1254 JPEG 直接收（New API 文档里"必须
# 是 4MB 以内的方形 PNG"是旧 DALL·E 的说法），约 24 秒，返回 data[0].url，
# 而且 size 参数实际被忽略——1024x1024 / auto / input_fidelity=high 三种都
# 输出 1254x1254，等于跟着输入走。所以默认发 auto，语义上最诚实。
EDIT_SIZE = "auto"
EDIT_INPUT_FIDELITY = "high"
EDIT_SIZES = ("auto",) + SUPPORTED_SIZES
# 输入图最长边。再大只是上传慢：模型自己也会缩，群里也没人看得出。
EDIT_MAX_EDGE = 1536
# 网关对单文件的限制（文档写 4MB）。留一点余量，超了就逐级降质量。
EDIT_MAX_BYTES = 3 * 1024 * 1024

_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "image/*,*/*;q=0.8",
}


class ImageGenError(Exception):
    """Generation failed in a way worth telling the user about."""

    def __init__(self, message: str, user_message: str) -> None:
        super().__init__(message)
        self._user_message = user_message

    def user_message(self) -> str:
        return self._user_message


@dataclass
class GeneratedImage:
    blob: bytes
    mime: str
    url: str
    elapsed: float
    raw_bytes: int


def availability(enabled: bool, api_key: str) -> tuple[bool, str]:
    """Return (available, reason_when_unavailable)."""
    if not enabled:
        return False, "画图功能关着。"
    if not api_key:
        return False, "画图没配 key（otowa.env 里的 IMAGE_GEN_API_KEY），用不了。"
    return True, ""


def normalize_size(raw: str) -> str:
    size = (raw or "").strip().lower()
    return size if size in SUPPORTED_SIZES else DEFAULT_SIZE


def normalize_edit_size(raw: str) -> str:
    """Edit defaults to `auto` (follow the input), unlike generate's 1024x1024."""
    size = (raw or "").strip().lower()
    return size if size in EDIT_SIZES else EDIT_SIZE


def clean_prompt(raw: str, limit: int) -> str:
    """Collapse whitespace and hard-cap the length.

    The gateway charges per image, not per token, so length is a sanity bound
    rather than a cost control: a 5000-character prompt is almost always a
    paste accident, and it makes the model wander off the actual subject.
    """
    text = re.sub(r"\s+", " ", raw or "").strip()
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _shrink(blob: bytes) -> tuple[bytes, str]:
    """Re-encode to a QQ-sized JPEG; fall back to the original on any trouble."""
    if not _HAS_PIL:
        return blob, "image/png"
    try:
        with _PILImage.open(io.BytesIO(blob)) as image:
            image.load()
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
            converted = buffer.getvalue()
    except Exception:
        logger.warning("画图结果转码失败，按原图发送", exc_info=True)
        return blob, "image/png"
    if len(converted) >= len(blob):
        return blob, "image/png"
    return converted, "image/jpeg"


async def _read_capped(response: object, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise ImageGenError(
                f"生成图超过 {limit} 字节上限", "这张图太大了，发不出来。"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _extract_payload(item: object) -> tuple[str, bytes]:
    """Pull (url, blob) out of one image item, accepting either shape."""
    url = str(getattr(item, "url", "") or "").strip()
    raw_b64 = getattr(item, "b64_json", None)
    if raw_b64:
        try:
            return url, base64.b64decode(raw_b64)
        except (ValueError, TypeError) as exc:
            raise ImageGenError(f"b64_json 解码失败: {exc}", "画出来了但我没解开，再试一次。") from exc
    return url, b""


def _translate(exc: Exception, *, action: str) -> ImageGenError:
    """Turn an SDK exception into one sentence fit for a QQ reply.

    action is only「画图」/「改图」and affects wording; the failure taxonomy
    (auth / rate limit / unreachable / HTTP status / timeout) is shared.
    """
    if isinstance(exc, AuthenticationError):
        return ImageGenError(f"鉴权失败: {exc}", f"{action}的 key 不对，得换一个。")
    if isinstance(exc, RateLimitError):
        return ImageGenError(f"限流: {exc}", f"{action}接口在排队，过一会儿再试。")
    if isinstance(exc, APIConnectionError):
        return ImageGenError(f"连接失败: {exc}", f"连不上{action}接口，待会儿再试。")
    if isinstance(exc, APIStatusError):
        return ImageGenError(
            f"HTTP {exc.status_code}: {exc}",
            f"{action}接口报错了（HTTP {exc.status_code}），换个描述再试试。",
        )
    # 超时走这里：asyncio.TimeoutError / httpx.ReadTimeout 都不在上面的分类里
    return ImageGenError(f"{type(exc).__name__}: {exc}", f"{action}超时了，再试一次。")


async def _resolve_blob(
    item: object, session: object, timeout_seconds: float
) -> tuple[str, bytes]:
    """Turn one response item into (url, bytes), downloading the URL if needed."""
    url, blob = _extract_payload(item)
    if not blob and url:
        try:
            async with session.get(
                url, headers=_IMAGE_HEADERS, timeout=max(30.0, float(timeout_seconds))
            ) as download:
                if download.status != 200:
                    raise ImageGenError(
                        f"下载结果 HTTP {download.status}", "图出来了但下载不下来，再试一次。"
                    )
                blob = await _read_capped(download, MAX_DOWNLOAD_BYTES)
        except ImageGenError:
            raise
        except Exception as exc:
            raise ImageGenError(
                f"下载结果失败: {exc}", "图出来了但下载不下来，再试一次。"
            ) from exc
    if not blob:
        raise ImageGenError("没有 url 也没有 b64", "图出来了但没拿到，再试一次。")
    return url, blob


async def generate(
    client: object,
    session: object,
    *,
    prompt: str,
    model: str,
    size: str = DEFAULT_SIZE,
    quality: str = "",
    timeout_seconds: float = 300.0,
) -> GeneratedImage:
    """Generate one image and return it already re-encoded for QQ.

    Raises ImageGenError with a user-facing sentence on every failure path, so
    the caller never has to translate SDK exceptions itself.
    """
    request: dict[str, object] = {
        "model": model,
        "prompt": prompt,
        "size": normalize_size(size),
        "n": 1,
        "timeout": max(30.0, float(timeout_seconds)),
    }
    # 留空就不发：网关对没见过的参数值会直接 400，宁可不传。
    if quality:
        request["quality"] = quality

    started = time.time()
    logger.info("发起画图: model=%s size=%s prompt=%r", model, request["size"], prompt[:60])
    try:
        response = await client.images.generate(**request)
    except Exception as exc:
        raise _translate(exc, action="画图") from exc

    items = getattr(response, "data", None) or []
    if not items:
        raise ImageGenError("响应里没有图片", "画是画了，但没拿到图，再试一次。")
    url, blob = await _resolve_blob(items[0], session, timeout_seconds)

    raw_bytes = len(blob)
    blob, mime = _shrink(blob)
    elapsed = time.time() - started
    logger.info(
        "画图完成: model=%s 耗时 %.1fs 原始 %d 字节 -> %s %d 字节",
        model,
        elapsed,
        raw_bytes,
        mime,
        len(blob),
    )
    return GeneratedImage(
        blob=blob, mime=mime, url=url, elapsed=elapsed, raw_bytes=raw_bytes
    )


def prepare_edit_input(blob: bytes) -> tuple[bytes, str, str]:
    """Normalize an arbitrary picture into something `images/edit` accepts.

    Measured on this gateway: JPEG uploads are fine, and the New API page's
    "must be a PNG under 4 MB and square" is the old DALL-E rule — a 1254x1254
    JPEG went through untouched. So everything is converted to JPEG, because:

      * alpha would otherwise land on a black background (QQ stickers are PNG
        with transparency; the model would happily "edit" that black backdrop)
      * anything past 1536px only costs upload time; the model downscales anyway
      * quality steps down until the payload fits the gateway's file limit

    Returns (bytes, filename, mime). Without Pillow the original is sent as-is.
    """
    if not _HAS_PIL:
        return blob, "input.png", "image/png"
    try:
        with _PILImage.open(io.BytesIO(blob)) as image:
            image.load()
            has_alpha = image.mode in ("RGBA", "LA") or (
                image.mode == "P" and "transparency" in image.info
            )
            if has_alpha:
                rgba = image.convert("RGBA")
                canvas = _PILImage.new("RGB", rgba.size, (255, 255, 255))
                canvas.paste(rgba, mask=rgba.split()[-1])
                work = canvas
            elif image.mode != "RGB":
                work = image.convert("RGB")
            else:
                work = image.copy()
            source_size = work.size
            if max(source_size) > EDIT_MAX_EDGE:
                work.thumbnail((EDIT_MAX_EDGE, EDIT_MAX_EDGE), _PILImage.LANCZOS)
            data = blob
            for quality in (92, 80, 70):
                buffer = io.BytesIO()
                work.save(buffer, "JPEG", quality=quality, optimize=True)
                data = buffer.getvalue()
                if len(data) <= EDIT_MAX_BYTES:
                    break
            out_size = work.size
    except Exception:
        logger.warning("改图输入归一化失败，按原样上传", exc_info=True)
        return blob, "input.png", "image/png"
    logger.info(
        "改图输入: %dx%d %d 字节 -> %dx%d JPEG %d 字节（透明=%s）",
        source_size[0],
        source_size[1],
        len(blob),
        out_size[0],
        out_size[1],
        len(data),
        has_alpha,
    )
    return data, "input.jpg", "image/jpeg"


async def edit(
    client: object,
    session: object,
    *,
    prompt: str,
    images: list[bytes],
    model: str,
    size: str = EDIT_SIZE,
    input_fidelity: str = EDIT_INPUT_FIDELITY,
    timeout_seconds: float = 300.0,
) -> GeneratedImage:
    """Edit one or more pictures; returns the result re-encoded for QQ.

    Only the request shape differs from generate() (multipart upload of the
    source images) — download, re-encode and error translation are shared, so
    the two paths cannot drift apart.

    **Multiple images are supported but slot order is not a contract.** Measured
    on this gateway: two-image requests return 200 and bill 1530 image tokens
    (exactly 2x765), but swapping the two images produced a near-identical
    result — so “the first one is the main picture” is not something the model
    honours. Callers should describe each picture in the prompt rather than rely
    on position.

    Other measured facts: 1254x1254 JPEG in, ~24s, and `size` is effectively
    ignored (1024x1024, auto and high-fidelity all came back 1254x1254). `auto`
    is still what gets sent, because that is the honest way to say "match the
    input".
    """
    prepared = [prepare_edit_input(blob) for blob in images if blob]
    if not prepared:
        raise ImageGenError("没有可用的原图", "这张图我拿不到，重发一次或者换一张再试。")
    files = [
        (f"image{index}.jpg", data, mime)
        for index, (data, _, mime) in enumerate(prepared)
    ]
    request: dict[str, object] = {
        "model": model,
        "prompt": prompt,
        # 单图传元组、多图传列表：两种形状 SDK 都接受，但单图走元组与
        # 改造前的行为完全一致，少一个回归面。
        "image": files[0] if len(files) == 1 else files,
        "n": 1,
        "size": normalize_edit_size(size),
        "timeout": max(30.0, float(timeout_seconds)),
    }
    if input_fidelity:
        request["input_fidelity"] = input_fidelity

    started = time.time()
    logger.info(
        "发起改图: model=%s size=%s fidelity=%s %d 张图 %s prompt=%r",
        model,
        request["size"],
        input_fidelity or "-",
        len(files),
        "/".join(str(len(data)) for data, _, _ in prepared),
        prompt[:60],
    )
    try:
        response = await client.images.edit(**request)
    except Exception as exc:
        raise _translate(exc, action="改图") from exc

    items = getattr(response, "data", None) or []
    if not items:
        raise ImageGenError("响应里没有图片", "改是改了，但没拿到图，再试一次。")
    url, blob = await _resolve_blob(items[0], session, timeout_seconds)

    raw_bytes = len(blob)
    blob, out_mime = _shrink(blob)
    elapsed = time.time() - started
    logger.info(
        "改图完成: model=%s 耗时 %.1fs 原始 %d 字节 -> %s %d 字节",
        model,
        elapsed,
        raw_bytes,
        out_mime,
        len(blob),
    )
    return GeneratedImage(
        blob=blob, mime=out_mime, url=url, elapsed=elapsed, raw_bytes=raw_bytes
    )

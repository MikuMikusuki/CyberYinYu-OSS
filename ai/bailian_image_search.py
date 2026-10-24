"""Bailian (Model Studio) 图搜图 tool call over the Responses API.

Bailian exposes image search only through the Responses API, and the request
must carry an image, so this module refuses to run without a public image URL.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ai.bailian_web_search import is_bailian_endpoint

logger = logging.getLogger("cyberyinyu")

IMAGE_SEARCH_TOOL: dict[str, str] = {"type": "image_search"}

# The tool only fires when the request actually asks for a search. An image plus
# the persona prompt and nothing else — which is exactly what a bare `#搜索` with
# no trailing text used to produce — makes the model simply describe the picture.
# Measured on qwen3.8-27b with the same image:
#   image + persona prompt only        -> 11.9s, 0 tool calls, "这金毛也太乖了吧"
#   image + persona prompt + directive -> 36.7s, 1 tool call, 10 images
# So the directive is always sent; the user's text is appended to it, not used
# in place of it.
DEFAULT_SEARCH_QUERY = "以图搜图：找出这张图的来源或与之相似的图片。"

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_EMPHASIS = re.compile(r"\*\*|__|`")
_MD_HEADING = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_MD_BULLET = re.compile(r"(?m)^\s*[-*+]\s+")
_MD_BLANK_RUN = re.compile(r"\n{3,}")


def strip_markdown(text: str) -> str:
    """Remove markdown syntax: the reply is delivered to a plain-text chat.

    The model embeds results as markdown (image embeds, bold labels, bullets),
    which QQ shows verbatim. Embedded images are dropped because the searched
    images are sent separately as real QQ image messages.
    """
    text = _MD_IMAGE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = _MD_EMPHASIS.sub("", text)
    text = _MD_BLANK_RUN.sub("\n\n", text)
    return text.strip()

# Bailian documents these families for the image_search tool: Plus/Max and
# qwen3.8-27b are recommended, the Flash families are also supported.
#
# An earlier note here claimed Flash "only calls the tool some of the time and
# takes roughly twice as long (226s vs 108s)". That was measured before the
# request always carried an explicit search directive, and it no longer holds.
# Same image, three runs each, on the same endpoint:
#   qwen3.8-27b              -> 3/3 tool calls, 44.5s average
#   qwen3.7-flash-2026-07-15 -> 3/3 tool calls, 38.2s average
# Callers should still check tool_calls before reporting a result, because tool
# invocation is the model's decision and is never contractually guaranteed.
_RECOMMENDED_MODEL_PREFIXES = (
    "qwen3.8-max",
    "qwen3.7-max",
    "qwen3.8-27b",
    "qwen3.7-plus",
    "qwen3.6-plus",
    "qwen3.5-plus",
)

_WEAKER_MODEL_PREFIXES = (
    "qwen3.8-flash",
    "qwen3.7-flash",
    "qwen3.6-flash",
    "qwen3.5-flash",
)

SUPPORTED_MODEL_PREFIXES = _RECOMMENDED_MODEL_PREFIXES + _WEAKER_MODEL_PREFIXES


def is_recommended_model(model: str) -> bool:
    normalized = model.strip().casefold()
    return bool(normalized) and normalized.startswith(_RECOMMENDED_MODEL_PREFIXES)


def is_image_search_available(*, model: str, base_url: str, enabled: bool) -> tuple[bool, str]:
    """Return (available, reason_when_unavailable)."""
    if not enabled:
        return False, "搜图功能已关闭。"
    if not is_bailian_endpoint(base_url):
        return False, "当前接口不是百炼，搜图用不了。"
    if not supports_image_search(model):
        return False, f"当前搜图模型 {model} 不能用，换成千问 Plus / Max / 27b / Flash 系列。"
    return True, ""


def supports_image_search(model: str) -> bool:
    normalized = model.strip().casefold()
    return bool(normalized) and normalized.startswith(SUPPORTED_MODEL_PREFIXES)


def is_public_image_url(url: str) -> bool:
    """Bailian fetches the image itself, so it must be a reachable public URL."""
    return url.startswith(("https://", "http://"))


@dataclass
class ImageSearchResult:
    text: str = ""
    images: list[dict[str, str]] = field(default_factory=list)
    # How many times the model actually invoked the tool. Zero means it answered
    # from the image alone, so there is nothing to present as a search result.
    tool_calls: int = 0


def _parse_search_images(payload: Any) -> list[dict[str, str]]:
    if not payload:
        return []
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("搜图结果不是合法 JSON，已忽略")
            return []
    if not isinstance(payload, list):
        return []
    images: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        images.append({"title": str(item.get("title") or "").strip(), "url": url})
    return images


def collect_result(response: Any) -> ImageSearchResult:
    """Pull the searched images and the model's own text out of a response."""
    result = ImageSearchResult()
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "image_search_call":
            continue
        result.tool_calls += 1
        status = getattr(item, "status", "")
        if status and status != "completed":
            logger.warning("搜图工具未正常完成: status=%s", status)
        result.images.extend(_parse_search_images(getattr(item, "output", None)))
    text = getattr(response, "output_text", "") or ""
    result.text = strip_markdown(text)
    return result


async def run_image_search(
    client: Any,
    *,
    model: str,
    image_url: str,
    query: str = "",
    instructions: str = "",
    timeout_seconds: float = 300.0,
) -> ImageSearchResult:
    """Search the web for images similar to `image_url` and summarise them.

    The tool is slow — measured 108s on the recommended model and 226s on Flash
    — so an explicit, generous timeout is always sent. Without one the request
    can hang far longer than a chat reply can wait.
    """
    # Always carry an explicit search directive: without one the model treats the
    # call as "describe this photo" and never invokes the tool.
    directive = query.strip() or DEFAULT_SEARCH_QUERY
    content: list[dict[str, str]] = [
        {"type": "input_text", "text": directive},
        {"type": "input_image", "image_url": image_url},
    ]

    request: dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "tools": [IMAGE_SEARCH_TOOL],
        "timeout": max(30.0, float(timeout_seconds)),
    }
    if instructions:
        request["instructions"] = instructions

    logger.info("发起图搜图: model=%s 有补充文本=%s 图片来源=%s", model, bool(query), image_url[:60])
    response = await client.responses.create(**request)
    result = collect_result(response)
    logger.info("图搜图完成: 图片 %d 张，回复 %d 字", len(result.images), len(result.text))
    return result

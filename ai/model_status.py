"""Live health probe for every model channel the bot can call.

`#model` answers "which model is configured"; this answers "is that channel
actually answering right now". They are different questions: a revoked key, a
supplier account out of credit, or a 402 balance error all leave the config
perfectly valid while every reply either fails or silently degrades. Reading
otowa.env cannot tell you that — only a real request can.

Rules this module follows, all of them cost-driven:

* Text channels are really called, with `max_tokens` set low enough that a
  probe costs a fraction of a cent. That is why the command is usable from a
  group chat rather than being a maintenance-only tool.
* Image channels (`#画图` / `#改图`, `#搜索`) are **never** called. They bill
  per image / per search, and a health check that costs money is not a health
  check. They are reported as "not probed" plus whether they are configured.
* One broken channel must not hide the rest, so all probes run concurrently and
  every failure is captured into its own row.

A probe is deliberately *not* routed through `ai.chat_compat`: thinking mode,
search flags and prompt caches would only add latency and fail modes to what we
are trying to measure (transport, credentials, model name).
"""

import asyncio
import base64
import logging
import re
import struct
import time
import zlib
from dataclasses import dataclass

from openai import AsyncOpenAI

from ai.bailian_image_search import is_image_search_available
from ai import image_gen
from config import (
    AI_MODEL,
    DEEPSEEK_TOOLS_API_KEY,
    DEEPSEEK_TOOLS_BASE_URL,
    DEEPSEEK_TOOLS_ENABLED,
    DEEPSEEK_TOOLS_MODEL,
    IMAGE_GEN_API_KEY,
    IMAGE_GEN_ENABLED,
    IMAGE_GEN_MODEL,
    IMAGE_PROMPT_POLISH_ENABLED,
    IMAGE_PROMPT_POLISH_MODEL,
    IMAGE_SEARCH_BASE_URL,
    IMAGE_SEARCH_ENABLED,
    IMAGE_SEARCH_MODEL,
    AI_VISION_MODEL,
)

logger = logging.getLogger("cyberyinyu")

# 8 不是 1：部分网关对 max_tokens=1 直接 400，而我们要测的是通道，不是抠这 7 个 token。
_PING_MAX_TOKENS = 8
# 单路上限。kimi 系开思考时首 token 会慢，给 20 秒；超时按失败报，不干等。
DEFAULT_TIMEOUT_SECONDS = 20.0
# 错误串截断：QQ 一条消息塞不下 4KB 的 traceback，而且首句就够定位。
_ERROR_CHARS = 90


@dataclass
class Probe:
    """One row of the report.

    `skipped` non-empty means "we did not call this channel"; the text is shown
    verbatim after the colon so 未启用 and 按次计费不实测 look different to the
    reader instead of both asking "why is there no latency".
    """

    label: str
    model: str
    skipped: str = ""
    ok: bool = False
    seconds: float = 0.0
    detail: str = ""


def _short(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    # 网关的 401 偶尔会把整串 key 回显出来，进群消息前先遮掉。
    text = re.sub(r"sk-[A-Za-z0-9_\-]{6,}", "sk-***", text)
    return text[:_ERROR_CHARS] + ("…" if len(text) > _ERROR_CHARS else "")


def _tiny_png_data_url() -> str:
    """A 1x1 red PNG as a data URL.

    The vision probe sends a real image instead of plain text: the failure mode
    worth catching is "model accepts text but rejects image parts", which a text
    ping would report as healthy. Generated here rather than hardcoded so there
    is no chance of shipping a corrupt base64 blob.
    """

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8bit, RGB
    body = zlib.compress(b"\x00\xff\x00\x00")  # filter 0 + one red pixel
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", body) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


async def _post_chat(client: AsyncOpenAI, model: str, messages: list, timeout: float):
    started = time.perf_counter()
    try:
        from ai.usage import track_model_call
        await track_model_call(
            lambda: asyncio.wait_for(
                client.chat.completions.create(
                    model=model, messages=messages, max_tokens=_PING_MAX_TOKENS,
                ), timeout,
            ), model=model, operation="health_check",
        )
    except asyncio.TimeoutError:
        return False, time.perf_counter() - started, f"超时（>{timeout:.0f}s 没回）"
    except Exception as exc:
        return False, time.perf_counter() - started, _short(exc)
    return True, time.perf_counter() - started, ""


async def _ping_text(client: AsyncOpenAI, model: str, timeout: float):
    return await _post_chat(client, model, [{"role": "user", "content": "ping"}], timeout)


async def _ping_vision(client: AsyncOpenAI, model: str, timeout: float):
    content = [
        {"type": "text", "text": "ping"},
        {"type": "image_url", "image_url": {"url": _tiny_png_data_url(), "detail": "low"}},
    ]
    return await _post_chat(client, model, [{"role": "user", "content": content}], timeout)


async def _ping_tools(timeout: float):
    """The 联网 channel lives on its own key / host, so it gets its own client.

    Probing it proves key + host + model name; it does NOT spend a search quota
    (search is billed per call, and availability of the tool itself is already
    covered by `deepseek_tools.availability()` in the feature path).
    """
    client = AsyncOpenAI(
        api_key=DEEPSEEK_TOOLS_API_KEY,
        base_url=DEEPSEEK_TOOLS_BASE_URL,
        timeout=timeout,
        max_retries=0,
    )
    try:
        return await _ping_text(client, DEEPSEEK_TOOLS_MODEL, timeout)
    finally:
        await client.close()


async def _fill(target: Probe, call) -> None:
    try:
        target.ok, target.seconds, target.detail = await call
    except Exception as exc:  # 单路炸掉不许拖垮整张表
        target.ok, target.seconds, target.detail = False, 0.0, _short(exc)
    if target.ok:
        logger.info("模型探测: %s %s 正常 %.1fs", target.label, target.model, target.seconds)
    else:
        logger.warning(
            "模型探测: %s %s 失败 %.1fs %s",
            target.label,
            target.model,
            target.seconds,
            target.detail,
        )


def render(probes: list[Probe], elapsed: float) -> str:
    measured = [p for p in probes if not p.skipped]
    good = sum(1 for p in measured if p.ok)
    lines = [f"模型状态 · {good}/{len(measured)} 正常 · 用时 {elapsed:.1f}s"]
    for probe in probes:
        if probe.skipped:
            lines.append(f"· {probe.label} {probe.model}：{probe.skipped}")
        elif probe.ok:
            lines.append(f"· {probe.label} {probe.model}：正常 {probe.seconds:.1f}s")
        else:
            lines.append(f"· {probe.label} {probe.model}：失败 {probe.seconds:.1f}s · {probe.detail}")
    if len(measured) > good:
        lines.append("失败那路先看余额和 key，再看端点有没有指错。")
    return "\n".join(lines)


async def report(
    chat_client: AsyncOpenAI,
    vision_client: AsyncOpenAI,
    polish_client: AsyncOpenAI,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Probe everything we can afford to probe and return the reply text.

    Clients are passed in rather than built here: reusing the objects the bot
    actually serves traffic with is the point — a new client built from the same
    config would prove the config, not the live path.
    """
    started = time.perf_counter()

    chat = Probe("聊天", AI_MODEL)
    vision = Probe("看图", AI_VISION_MODEL)
    # 「润色」单说太含糊：这路只给 #画图 改写提示词（#改图 用它做角色识别，不改写），
    # 和聊天、看图都不是一件事，所以标签写全。
    polish = Probe("画图提示词润色", IMAGE_PROMPT_POLISH_MODEL)
    if not IMAGE_PROMPT_POLISH_ENABLED:
        polish.skipped = "未启用"

    tools = Probe("联网", DEEPSEEK_TOOLS_MODEL)
    if not DEEPSEEK_TOOLS_ENABLED or not DEEPSEEK_TOOLS_API_KEY:
        tools.skipped = "未启用"

    search_ok, _ = is_image_search_available(
        model=IMAGE_SEARCH_MODEL,
        base_url=IMAGE_SEARCH_BASE_URL,
        enabled=IMAGE_SEARCH_ENABLED,
    )
    image_search = Probe(
        "图搜图",
        IMAGE_SEARCH_MODEL,
        skipped=f"不实测（按次计费）· {'已配置' if search_ok else '未配置'}",
    )

    gen_ok, _ = image_gen.availability(IMAGE_GEN_ENABLED, IMAGE_GEN_API_KEY)
    image_gen_row = Probe(
        "画图/改图",
        IMAGE_GEN_MODEL,
        skipped=f"不实测（按张计费）· {'已配置' if gen_ok else '未配置'}",
    )

    probes = [chat, vision, polish, tools, image_search, image_gen_row]

    jobs = [
        _fill(chat, _ping_text(chat_client, AI_MODEL, timeout)),
        _fill(vision, _ping_vision(vision_client, AI_VISION_MODEL, timeout)),
    ]
    if not polish.skipped:
        jobs.append(_fill(polish, _ping_text(polish_client, IMAGE_PROMPT_POLISH_MODEL, timeout)))
    if not tools.skipped:
        jobs.append(_fill(tools, _ping_tools(timeout)))
    await asyncio.gather(*jobs)

    return render(probes, time.perf_counter() - started)

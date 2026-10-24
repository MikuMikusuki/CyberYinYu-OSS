"""Compatibility helpers for OpenAI-shaped chat APIs with model-specific limits."""

import logging
import re
from typing import Any

from openai import AsyncOpenAI, BadRequestError

logger = logging.getLogger("cyberyinyu")

# --- Deep thinking control (Bailian / DashScope) ----------------------------
#
# `enable_thinking` is not an OpenAI standard field, so it always travels inside
# `extra_body`. Bailian's mixed-thinking models default to thinking ON.
#
# Two different questions are involved, so they get two different patterns:
#   * is_flash_tier       - "should we turn thinking OFF by default?" (fast chat)
#   * can_toggle_thinking - "is this model able to switch either way?"
#
# Thinking-only models (deepseek-r1, qwen3.8-2.4t-a95b) match neither, so they
# are never sent the field - it would just be a 400 error.
#
# The "flash" tier exists on more than one vendor, and it thinks by default in
# both cases. Measured on the same short prompt:
#   qwen3.8-flash       : 103 reasoning tokens / 5.0s  ->  0 / 0.8s when off
#   deepseek-v4.1-flash : 332 reasoning tokens / 5.1s  ->  0 / 0.8s when off
# Other *-flash names (gemini-3.8-flash, glm-5.3-flash) are deliberately NOT
# matched: they are not Bailian models and the field's effect there is untested.

_FLASH_TIER = re.compile(r"^(?:qwen[\d.]*(?:-omni)?|deepseek-v[\d.]+)-flash", re.IGNORECASE)
_MIXED_THINKING = re.compile(
    r"^(?:qwen[\d.]*(?:-omni)?-(?:max|plus|flash|27b)|deepseek-v[\d.]+-flash)(?:$|[-.])",
    re.IGNORECASE,
)

THINKING_AUTO = "auto"  # model-aware default: Flash off, anything else untouched
THINKING_ON = "on"  # ask for thinking even on the Flash tier
THINKING_OFF = "off"  # suppress thinking where the model allows it

# Bailian's mixed-thinking models default to thinking ON, so a short chat reply
# can spend its whole token budget on reasoning and come back empty or truncated.
# Small talk gains nothing from deep thinking, so the Flash tier defaults to it
# switched off. Set to False to let those models think by default instead.
DISABLE_FLASH_THINKING = True


def is_flash_tier(model: str) -> bool:
    """True for the fast/cheap tiers that default to thinking ON.

    Covers qwen-flash / qwen3.x-flash / omni-flash / date snapshots, and
    deepseek-v4.1-flash / deepseek-v4-flash / their snapshots.
    """
    return bool(_FLASH_TIER.match(model.strip()))


def can_toggle_thinking(model: str) -> bool:
    """True for models that accept `enable_thinking` either way."""
    return bool(_MIXED_THINKING.match(model.strip()))


# Backwards-compatible aliases (older names, kept so imports elsewhere survive).
is_qwen_flash = is_flash_tier
DISABLE_QWEN_FLASH_THINKING = DISABLE_FLASH_THINKING


def _apply_thinking_switch(options: dict[str, Any], model: str, thinking: str) -> None:
    """Set the deep-thinking switch on `options['extra_body']`, in place.

    `extra_body` may already carry Bailian's `enable_search`, so it is copied and
    merged rather than replaced - assigning would silently drop the web search.

    `auto` reproduces the previous behaviour (Flash off). `on` / `off` force the
    value but only for models documented to accept it.
    """
    mode = thinking if thinking in {THINKING_AUTO, THINKING_ON, THINKING_OFF} else THINKING_AUTO

    if mode == THINKING_AUTO:
        if not DISABLE_FLASH_THINKING or not is_flash_tier(model):
            return
        desired = False
    elif mode == THINKING_ON:
        if not can_toggle_thinking(model):
            return  # already on by default, or not switchable at all
        desired = True
    else:  # THINKING_OFF
        if not can_toggle_thinking(model):
            return
        desired = False

    body = dict(options.get("extra_body") or {})
    body["enable_thinking"] = desired
    options["extra_body"] = body
    if desired is False:
        logger.info("模型 %s：关闭深度思考 (enable_thinking=false)", model.strip())
    else:
        logger.info("模型 %s：开启深度思考 (enable_thinking=true)", model.strip())


async def create_chat_completion(
    client: AsyncOpenAI,
    **request: Any,
) -> Any:
    """Create a chat completion, applying model-specific parameter rules.

    Extra keyword argument:
        thinking: "auto" (default) | "on" | "off"  - deep-thinking policy.
    """
    options = dict(request)
    model = str(options.get("model", ""))
    thinking = str(options.pop("thinking", THINKING_AUTO))

    if "kimi-k3" in model.casefold():
        options.pop("temperature", None)
        if "max_tokens" in options:
            # Kimi K3 always reasons, so a small completion cap can be consumed
            # before it has room to emit the user-facing answer.
            options["max_tokens"] = max(int(options["max_tokens"]), 8192)

    _apply_thinking_switch(options, model, thinking)

    try:
        return await client.chat.completions.create(**options)
    except BadRequestError as exc:
        error_text = str(exc).casefold()
        changed = False

        if (
            "temperature" in options
            and "temperature" in error_text
            and any(marker in error_text for marker in ("not supported", "unsupported", "不支持"))
        ):
            options.pop("temperature", None)
            changed = True
            logger.warning("模型 %s 不支持 temperature，已移除该参数并重试一次", model or "unknown")

        # Guard for endpoints or models that reject Bailian-only fields.
        # Dropping the switch beats failing the reply.
        body = options.get("extra_body")
        if isinstance(body, dict) and "enable_thinking" in body and "enable_thinking" in error_text:
            trimmed = dict(body)
            trimmed.pop("enable_thinking", None)
            if trimmed:
                options["extra_body"] = trimmed
            else:
                options.pop("extra_body", None)
            changed = True
            logger.warning("模型 %s 不支持 enable_thinking，已移除该参数并重试一次", model or "unknown")

        if not changed:
            raise
        return await client.chat.completions.create(**options)

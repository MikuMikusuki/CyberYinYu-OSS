"""Compatibility helpers for OpenAI-shaped chat APIs with model-specific limits."""

import logging
import os
import re
from typing import Any

from openai import AsyncOpenAI, BadRequestError

logger = logging.getLogger("cyberyinyu")

# --- Prompt-cache health ----------------------------------------------------
#
# The provider bills uncached input at many times the cached rate, so a broken
# prefix is a cost incident, not a curiosity. DeepSeek reports
# `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`; providers that
# don't (Bailian) are silently skipped. Thresholds are env-tunable so an
# incident can be investigated without a redeploy.
CACHE_HIT_WARN_RATIO = float(os.getenv("CACHE_HIT_WARN_RATIO", "0.6"))
CACHE_HIT_WARN_MIN_TOKENS = int(os.getenv("CACHE_HIT_WARN_MIN_TOKENS", "2000"))

# --- Deep thinking control (Bailian / DashScope / DeepSeek official) --------
#
# 两家字段不同，写错不会报错、只会静默失效（2026-10-29 实测）：
#   * 百炼 / 网关：`extra_body.enable_thinking = true|false`
#   * api.deepseek.com：`extra_body.thinking = {"type": "enabled"|"disabled"}`
#     同一句「在吗」：默认 111 reasoning token / 1.26s；thinking.type=disabled
#     后 0 / 0.79s；而 enable_thinking=false 被忽略（仍 76 token）。
# 两者都不是 OpenAI 标准字段，所以一律塞在 `extra_body` 里。
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
#   deepseek-flash      : 111 reasoning tokens / 1.3s  ->  0 / 0.8s when off
# Other *-flash names (gemini-3.8-flash, glm-5.3-flash) are deliberately NOT
# matched: they are not Bailian models and the field's effect there is untested.

_FLASH_TIER = re.compile(
    # 裸名 deepseek-flash 也要匹配：2026-10-29 起生产就用这个 id，旧正则只认
    # deepseek-v4.1-flash，导致 on/off 三个模式全被静默丢弃（见 NOTES-runtime 二）。
    r"^(?:qwen[\d.]*(?:-omni)?|deepseek(?:-v[\d.]+)?)-flash",
    re.IGNORECASE,
)
_MIXED_THINKING = re.compile(
    r"^(?:qwen[\d.]*(?:-omni)?-(?:max|plus|flash|27b)|deepseek(?:-v[\d.]+)?-flash)(?:$|[-.])",
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
    deepseek-flash / deepseek-v4.1-flash / deepseek-v4-flash / their snapshots.
    """
    return bool(_FLASH_TIER.match(model.strip()))


def can_toggle_thinking(model: str) -> bool:
    """True for models that accept `enable_thinking` either way."""
    return bool(_MIXED_THINKING.match(model.strip()))


# Backwards-compatible aliases (older names, kept so imports elsewhere survive).
is_qwen_flash = is_flash_tier
DISABLE_QWEN_FLASH_THINKING = DISABLE_FLASH_THINKING


def _apply_thinking_switch(
    options: dict[str, Any], model: str, thinking: str, base_url: str = ""
) -> None:
    """Set the deep-thinking switch on `options['extra_body']`, in place.

    **两家用的字段不一样，混用会静默失效**（2026-10-29 实测，模型 deepseek-flash）：

        端点              正确写法                              错误写法的后果
        ----------------- --------------------------------    --------------------------------
        百炼/网关          extra_body.enable_thinking=false     ——
        api.deepseek.com  extra_body.thinking={"type":"disabled"}  传 enable_thinking=false 被忽略，
                                                                  仍花 76 个 reasoning token

    同一句「在吗」：不传开关 114 token / 1.26s，`thinking.type=disabled` 后 1 token / 0.79s。

    `extra_body` 里可能已经有百炼的 `enable_search`，所以复制合并，绝不整体赋值。
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
    if "deepseek.com" in (base_url or "").casefold():
        body["thinking"] = {"type": "enabled" if desired else "disabled"}
        field = f"thinking.type={'enabled' if desired else 'disabled'}"
    else:
        body["enable_thinking"] = desired
        field = f"enable_thinking={'true' if desired else 'false'}"
    options["extra_body"] = body
    logger.info(
        "模型 %s：%s深度思考 (%s)", model.strip(), "开启" if desired else "关闭", field
    )


def _check_cache_health(response: Any, model: str, options: dict[str, Any]) -> None:
    """Warn when the provider's prompt-cache hit ratio falls below threshold.

    The chat path depends on a stable `[system, *history]` prefix; a drop means
    the prefix broke and every turn is being re-sent at full price. Skipped for
    tool-call rounds (the `working` list grows every round, cold by design) and
    for small/first-turn requests (legitimately cold).
    """
    if "tools" in options:
        return
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    miss = getattr(usage, "prompt_cache_miss_tokens", None)
    if hit is None or miss is None:
        return
    total = int(hit) + int(miss)
    if total < CACHE_HIT_WARN_MIN_TOKENS:
        return
    ratio = int(hit) / total
    if ratio >= CACHE_HIT_WARN_RATIO:
        return
    logger.warning(
        "提示词缓存命中率过低: model=%s hit=%d miss=%d total=%d ratio=%.1f%% (< %.0f%%)"
        "—— 前缀可能被破坏，逐轮全价重发；若确认是会话首条消息可忽略",
        model or "unknown",
        hit,
        miss,
        total,
        ratio * 100,
        CACHE_HIT_WARN_RATIO * 100,
    )


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

    _apply_thinking_switch(
        options, model, thinking, base_url=str(getattr(client, "base_url", "") or "")
    )

    try:
        response = await client.chat.completions.create(**options)
        _check_cache_health(response, model, options)
        return response
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
        response = await client.chat.completions.create(**options)
        _check_cache_health(response, model, options)
        return response

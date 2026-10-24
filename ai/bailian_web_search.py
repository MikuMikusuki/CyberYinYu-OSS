"""Web-search capability checks for Bailian OpenAI-compatible Chat Completions."""

from urllib.parse import urlsplit


_BAILIAN_HOST_SUFFIXES = (
    "dashscope.aliyuncs.com",
    "maas.aliyuncs.com",
)
# 自建或第三方 OpenAI 兼容网关如确需启用 enable_search，
# 在上面追加其域名后缀，并确认该网关确实支持该参数。

# Bailian documents these families for Chat Completions enable_search.
# Kimi K3 and GLM-5.2 are Responses-API-only for web search, while GLM-5.3
# is not listed as supporting web search. Unknown models are deliberately
# excluded so an unsupported non-standard parameter is never sent by default.
_SUPPORTED_MODEL_PREFIXES = (
    "qwen3.8-",
    "qwen3.7-",
    "qwen3.6-",
    "qwen3.5-",
    "qwen3-max",
    "qwen-max",
    "qwen-plus",
    "qwen-flash",
    "qwen-turbo",
    "qwq-plus",
    "deepseek-v4.1-",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek-v3.2",
    "deepseek-v3.1",
    "deepseek-v3",
    "deepseek-r1",
    "moonshot-kimi-k2-instruct",
    "minimax-m2.1",
)


def is_bailian_endpoint(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").casefold().rstrip(".")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _BAILIAN_HOST_SUFFIXES)


def supports_bailian_chat_search(model: str) -> bool:
    normalized = model.strip().casefold()
    return bool(normalized) and normalized.startswith(_SUPPORTED_MODEL_PREFIXES)


def bailian_web_search_options(
    *, model: str, base_url: str, enabled: bool
) -> dict[str, object]:
    """Enable native search only for documented Bailian Chat Completions models."""
    if not enabled or not is_bailian_endpoint(base_url) or not supports_bailian_chat_search(model):
        return {}

    parameters: dict[str, object] = {"enable_search": True}
    normalized = model.strip().casefold()
    if normalized.startswith(("qwen3.5-omni-", "qwen3.8-omni-")):
        parameters["search_options"] = {"search_strategy": "agent"}
    return {"extra_body": parameters}

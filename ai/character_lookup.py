"""Look up a named character's appearance before letting the model draw them.

The image model already knows the famous ones, badly knows the rest, and
invents the obscure ones. So when polishing a request we first ask *who* is
named (that happens in ai/image_prompt.py, one JSON field), and only then — for
a name that was actually uttered — do we go and read the character's official
look: hair, eyes, outfit, signature props.

Two calls, only on the character path:
    search  -> «{name} 外貌 发色 发型 服装 特征»  (DeepSeek 官方搜索)
    ask     -> squeeze those results into one line of objective appearance

Everything here degrades to "" — an unknown character must not stop the picture,
it just means the prompt stays as the user wrote it.
"""

import logging
import re
import time

from ai import deepseek_tools

logger = logging.getLogger("cyberyinyu")

# 角色名短得离谱或长得离谱都不正常：正常的名字不会超过 40 字，超过基本是
# 模型把整句描述塞进了这个字段。
MAX_NAME_CHARS = 40
# 外貌描述的长度上限。再长会盖过用户自己的场景描述，画出来全是"设定"。
MAX_APPEARANCE_CHARS = 180
# 同一个角色在群里会被反复点，查一次就够了。
_CACHE_LIMIT = 50
_cache: dict[str, str] = {}

LOOKUP_SYSTEM = """你是设定资料整理员。只根据给定的资料，写出角色**可画出来的客观外观**。
规则：
- 只写看得见的东西：发色发型、瞳色、服装配色与款式、标志性配件或纹样
- 不写剧情、性格、能力、声优、评价
- 不写「根据资料」「据悉」这类开场白，直接给描述
- 用逗号分隔的短语，不超过 80 字，中文
- 资料里没有的项就跳过，不要编"""

_PREFIX = re.compile(r"^(?:根据(?:资料|搜索结果|以上内容)?[，,：:]?\s*|资料显示[，,：]?\s*)")
_QUOTES = "\"'“”「」『』"


def _clean(text: str) -> str:
    line = " ".join((text or "").split())
    line = line.strip(_QUOTES)
    line = _PREFIX.sub("", line)
    return line[:MAX_APPEARANCE_CHARS].strip(" ，,。.")


def valid_name(name: str) -> str:
    """Return the trimmed name, or "" when it is obviously not a character name."""
    candidate = " ".join((name or "").split()).strip(_QUOTES)
    if not candidate or len(candidate) > MAX_NAME_CHARS:
        return ""
    return candidate


async def describe(session: object, name: str, *, timeout_seconds: float = 20.0) -> str:
    """Return a one-line appearance description, or "" on any failure."""
    candidate = valid_name(name)
    if not candidate:
        return ""
    cached = _cache.get(candidate.casefold())
    if cached is not None:
        logger.info("角色外貌命中缓存: %s", candidate)
        return cached

    started = time.time()
    try:
        result = await deepseek_tools.search(
            session, f"{candidate} 外貌 发色 发型 服装 特征", count=5
        )
        material = deepseek_tools.format_for_model(result, max_items=5)
        if not material.strip():
            logger.info("角色外貌搜索无结果: %s", candidate)
            return ""
        answer = await deepseek_tools.ask(
            session,
            f"描述「{candidate}」的外貌，只写能画出来的客观外观。",
            context=material,
            system=LOOKUP_SYSTEM,
        )
    except deepseek_tools.DeepSeekToolError as exc:
        logger.warning("角色外貌查询失败: %s -> %s", candidate, exc)
        return ""
    except Exception:
        logger.exception("角色外貌查询异常: %s", candidate)
        return ""

    appearance = _clean(answer)
    if not appearance:
        return ""
    if len(_cache) >= _CACHE_LIMIT:
        _cache.pop(next(iter(_cache)))
    _cache[candidate.casefold()] = appearance
    logger.info(
        "角色外貌已查到: %s（%.1fs）%s", candidate, time.time() - started, appearance[:80]
    )
    return appearance

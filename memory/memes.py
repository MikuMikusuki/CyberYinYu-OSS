"""外置梗库：只在当前消息真的命中梗名时才注入到 system prompt。

库文件是 ``memory/memes.json``，**每次生成消息都重新读取**，改动后立刻生效，
不需要重启 bot（跟 ``personality/style.txt`` 一样，区别于只在 import 时读一次
的 ``otowa.env``）。

为什么不全部注入：每个条目几百字，全量塞进每条消息的 prompt 是一笔常驻的
token 开销，而绝大多数消息跟任何梗都无关。所以这里是「命中才注入」。

新增条目只需要改 JSON：复制一条，改 id / name / keywords。keywords 里的词会
做子串匹配，所以**不要放过于通用的短词**（例如「八分钱」「dddd」），否则会
误伤正常聊天。keywords 必须是**数组**，写成字符串会被忽略并打一条 warning。
"""

import json
import logging
from pathlib import Path
from typing import Any


MEME_FILE = Path(__file__).resolve().parent / "memes.json"
# Injected only on a hit, but a single message can mention several memes.
MAX_CONTEXT_CHARS = 1400
MAX_MATCHED_MEMES = 3
MAX_FIELD_CHARS = 220
# Fields rendered into the prompt. `stance` is rendered last on purpose (it is
# the closing instruction) but it is RESERVED first - see _render_entry.
_FIELDS = (
    ("是", "summary"),
    ("出处", "origin"),
    ("用法", "usage"),
    ("分寸", "stance"),
)
_STANCE_FIELD = "stance"
HEADING = (
    "下面是本地梗库中与当前消息相关的条目。"
    "只当作背景知识参考，不要原样背诵或整段复述给用户："
    "别人提及时可以自然接话，但不要由你主动发起这个梗；"
    "若有人明确表示不喜欢，立刻打住不再提。"
)

logger = logging.getLogger(__name__)


def _is_disabled(value: Any) -> bool:
    """`enabled` 只写了 false 才算关，缺省视为开。

    Accepts the literal ``false`` as well as the string/0 forms people actually
    type into JSON ("false", 0, "no", "off") - a silently-ignored disable flag
    is worse than a strict one.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return value.strip().lower() in {"false", "0", "no", "off", ""}
    return not value


def _clean_keywords(raw: Any, name: str) -> list[str]:
    """Turn a JSON value into a lowercase keyword list.

    A bare string is rejected rather than iterated: iterating it yields single
    characters, so ``"keywords": "难受想哭"`` would silently become the one-char
    keywords 难/受/想/哭 and match almost every message.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        logger.warning(
            "梗库条目 %s 的 keywords 不是数组（实际是 %s），已忽略该字段",
            name,
            type(raw).__name__,
        )
        return []
    return [
        keyword.lower()
        for keyword in raw
        if isinstance(keyword, str) and keyword.strip()
    ]


def load_meme_entries(path: Path = MEME_FILE) -> list[dict[str, Any]]:
    """Read and sanitise the meme library. Never raises on missing/bad input."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取梗库失败: path=%s error=%s", path, exc)
        return []
    if not isinstance(data, dict):
        return []
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        return []

    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        if _is_disabled(item.get("enabled")):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        keywords = _clean_keywords(item.get("keywords"), name)
        # A name without a keyword would never be matched by text: fall back to
        # the name itself so a new entry still works before anyone tunes it.
        name_key = name.lower()
        if name_key not in keywords:
            keywords.append(name_key)
        entry_id = item.get("id")
        entry_id = entry_id.strip() if isinstance(entry_id, str) else name_key
        if entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)
        entry: dict[str, Any] = {"id": entry_id, "name": name, "keywords": keywords}
        for _, field in _FIELDS:
            value = item.get(field)
            entry[field] = (
                " ".join(value.split())[:MAX_FIELD_CHARS] if isinstance(value, str) else ""
            )
        entries.append(entry)
    return entries


def match_memes(text: str, path: Path = MEME_FILE) -> list[dict[str, Any]]:
    """Return library entries whose keywords appear in ``text``."""
    if not text or not text.strip():
        return []
    haystack = text.lower()
    hits: list[dict[str, Any]] = []
    for entry in load_meme_entries(path):
        if any(keyword in haystack for keyword in entry["keywords"]):
            hits.append(entry)
            if len(hits) >= MAX_MATCHED_MEMES:
                break
    return hits


def _render_entry(entry: dict[str, Any], room: int) -> str:
    """Render one entry into at most ``room`` characters.

    Never cuts a field in half. `stance` is the safety field, so its space is
    reserved up front and it survives even when the background fields have to be
    dropped - otherwise the entry that teaches the model a slur would also be the
    one that loses the instruction not to use it.
    """
    header = f"【{entry['name']}】"
    stance_line = f"分寸：{entry[_STANCE_FIELD]}" if entry[_STANCE_FIELD] else ""

    used = len(header) + (1 + len(stance_line) if stance_line else 0)
    if used > room:
        # Not even name + stance fits; drop the entry rather than explain a meme
        # without its guardrail.
        return ""

    kept: list[str] = []
    for label, field in _FIELDS:
        if field == _STANCE_FIELD or not entry[field]:
            continue
        line = f"{label}：{entry[field]}"
        if used + 1 + len(line) > room:
            continue
        kept.append(line)
        used += 1 + len(line)

    parts = [header, *kept]
    if stance_line:
        parts.append(stance_line)
    return "\n".join(parts)


def memes_context(text: str, path: Path = MEME_FILE) -> str:
    """Render the meme block for the system prompt, or '' when nothing matches."""
    hits = match_memes(text, path)
    if not hits:
        return ""
    rendered: list[str] = []
    total = len(HEADING)
    for entry in hits:
        room = MAX_CONTEXT_CHARS - total - 2  # 2 = the blank line between blocks
        block = _render_entry(entry, room)
        if not block:
            continue
        rendered.append(block)
        total += len(block) + 2
    if not rendered:
        return ""
    return HEADING + "\n" + "\n\n".join(rendered)

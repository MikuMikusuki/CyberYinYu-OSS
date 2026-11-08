"""外置梗库：只在当前消息真的命中梗名时才注入到 system prompt。

库文件是 ``memory/memes.json``，**每次生成消息都重新读取**（按 mtime 做一层
解析缓存，文件一改缓存立刻失效），改动后立刻生效，不需要重启 bot（跟
``personality/style.txt`` 一样，区别于只在 import 时读一次的 ``otowa.env``）。

为什么不全部注入：每个条目几百字，全量塞进每条消息的 prompt 是一笔常驻的
token 开销，而绝大多数消息跟任何梗都无关。所以这里是「命中才注入」。

命中规则（2026-11-08 重写）
--------------------------
1. **归一化后再比**：全角转半角、去零宽/不可见字符、统一小写（NFKC）。
   所以「ｘｍｌ」「羡８慕」跟它们的半角写法一样能命中。
2. **允许字间插分隔符**：`羡 慕 了`、`羡·慕·了`、`x m l` 都算命中，
   但最多允许 3 个分隔符，不会跨句硬凑。
3. **数字等价**：`八`/`8`、`两`/`2` 视为同一个字，省得每个谐音写法都手写一遍。
4. **少量简繁等价**：内置一张常用 1:1 简繁表（飛/錢/馬/樂…），港台写法也能命中。
5. **ASCII 缩写要词边界**：`xml` 不会再命中 `xmlhttprequest` 这种无关串。
6. **按相关度排序取前 N**：当前消息里命中的条目 > 只在历史里被顺带一提的；
   关键字越长（越具体）分越高；重复出现有小幅加成。旧的「按文件顺序取前 3」
   会让一条通用词条目把真正相关的条目挤掉。
7. **单字关键字直接丢弃**：中文单字几乎等于「无条件命中」，是误伤的源头。

新增条目只需要改 JSON：复制一条，改 id / name / keywords。keywords 是数组，
可以再写一个 ``aliases``（同样合并进来）。落地写法不用穷举谐音，上面的归一化
已经覆盖了大部分。
"""

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any


MEME_FILE = Path(__file__).resolve().parent / "memes.json"
# Injected only on a hit, but a single message can mention several memes.
MAX_CONTEXT_CHARS = 1400
MAX_MATCHED_MEMES = 3
MAX_FIELD_CHARS = 220
# 中文单字关键字≈无条件命中，宁可漏也不放进来。
MIN_KEYWORD_CHARS = 2
# 评分：当前消息命中远重于 8 条历史里的顺带一提。
FOCUS_BASE = 40
HISTORY_BASE = 10
LENGTH_BONUS_CAP = 10
REPEAT_BONUS = 2
MAX_REPEATS = 3
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


# --- 归一化 ---------------------------------------------------------------

# 零宽空格、方向控制符、BOM：肉眼看不见，却会把一个词劈成两段。
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")


def _normalize(text: str) -> str:
    """Fold a message into the form keywords are compared in."""
    if not text:
        return ""
    # NFKC 把全角字母/数字、各种兼容字折叠成标准写法。
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).lower()


# 常用的一对一简繁字（只放 1:1 的，避免 干/乾/幹 这类一对多误伤）。
# 偶数长度，"简繁" 成对排列。
_TRAD_PAIRS = (
    "飞飛钱錢马馬鸟鳥鱼魚猫貓猪豬龙龍凤鳳电電车車铁鐵银銀错錯难難"
    "欢歡乐樂气氣风風时時后後里裡这這说說会會来來过過对對开開关關"
    "门門问問听聽见見学學个個们們样樣种種点點长長张張边邊头頭实實"
    "无無数數话話视視图圖网網声聲"
)
if len(_TRAD_PAIRS) % 2:  # pragma: no cover - 防手滑写漏一个字
    raise ValueError("_TRAD_PAIRS 必须成对，长度应为偶数")
_TRAD = dict(zip(_TRAD_PAIRS[0::2], _TRAD_PAIRS[1::2]))

# 中文数字与阿拉伯数字互换（只做单字，多字的不塞字符类）。
_DIGIT_CLASSES = {
    "零": "0",
    "一": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}

# 允许夹在两个关键字字符之间的分隔符（最多 3 个）。
_SEP = (
    r"[\s\u00a0\u3000·・•．.,，。、!！?？_~～\-—'\"“”‘’()（）\[\]【】<>《》/\\|]{0,3}"
)


def _char_class(ch: str) -> str:
    """One keyword character → a regex atom that accepts its known variants."""
    chars = {ch}
    digit = _DIGIT_CLASSES.get(ch)
    if digit:
        chars.add(digit)
    trad = _TRAD.get(ch)
    if trad:
        chars.add(trad)
    if len(chars) == 1:
        return re.escape(ch)
    return "[" + "".join(re.escape(c) for c in sorted(chars)) + "]"


def _is_ascii_word(keyword: str) -> bool:
    return bool(keyword) and all(c.isascii() and c.isalnum() for c in keyword)


def _compile_keyword(keyword: str) -> re.Pattern[str]:
    """Build the matcher for one keyword (see the module docstring rules)."""
    body = _SEP.join(_char_class(ch) for ch in keyword)
    if _is_ascii_word(keyword):
        # 缩写是字母串：`xml` 不该在 `xmlhttprequest` 里"命中"。
        body = rf"(?<![0-9a-z]){body}(?![0-9a-z])"
    return re.compile(body)


# --- 读取 -----------------------------------------------------------------


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
    """Turn a JSON value into a normalised keyword list.

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
    cleaned: list[str] = []
    for keyword in raw:
        if not isinstance(keyword, str) or not keyword.strip():
            continue
        keyword = _normalize(keyword.strip())
        if len(keyword) < MIN_KEYWORD_CHARS:
            logger.warning(
                "梗库条目 %s 的关键字 %r 太短（少于 %d 字），已跳过："
                "中文单字会命中几乎所有消息",
                name,
                keyword,
                MIN_KEYWORD_CHARS,
            )
            continue
        if keyword not in cleaned:
            cleaned.append(keyword)
    return cleaned


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
        # `aliases` is just a second place to put the same thing; people split
        # them by habit ("正式写法" vs "口语写法") and expect both to work.
        for alias in _clean_keywords(item.get("aliases"), name):
            if alias not in keywords:
                keywords.append(alias)
        # A name without a keyword would never be matched by text: fall back to
        # the name itself so a new entry still works before anyone tunes it.
        name_key = _normalize(name)
        if len(name_key) >= MIN_KEYWORD_CHARS and name_key not in keywords:
            keywords.append(name_key)
        entry_id = item.get("id")
        entry_id = entry_id.strip() if isinstance(entry_id, str) else name_key
        if entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)
        entry: dict[str, Any] = {"id": entry_id, "name": name, "keywords": keywords}
        # (compiled regex, keyword) - the regex is what matching actually uses.
        entry["patterns"] = [(kw, _compile_keyword(kw)) for kw in keywords]
        for _, field in _FIELDS:
            value = item.get(field)
            entry[field] = (
                " ".join(value.split())[:MAX_FIELD_CHARS] if isinstance(value, str) else ""
            )
        entries.append(entry)
    return entries


# mtime/size keyed cache: memes.json is re-read on every reply, but parsing and
# compiling a dozen regexes each time is pointless work. Editing the file bumps
# mtime, so "改完即生效" still holds.
_ENTRY_CACHE: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}


def _entries(path: Path) -> list[dict[str, Any]]:
    try:
        stat = path.stat()
    except OSError:
        return []
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _ENTRY_CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    entries = load_meme_entries(path)
    if len(_ENTRY_CACHE) > 16:
        _ENTRY_CACHE.clear()
    _ENTRY_CACHE[str(path)] = (key, entries)
    return entries


# --- 匹配 -----------------------------------------------------------------


def _score(entry: dict[str, Any], focus: str, haystack: str) -> int:
    """How relevant is this entry to the current turn? 0 means "no hit"."""
    score = 0
    for keyword, pattern in entry["patterns"]:
        weight = min(len(keyword), LENGTH_BONUS_CAP)
        if focus and pattern.search(focus):
            hits = len(pattern.findall(focus))
            score += FOCUS_BASE + weight + REPEAT_BONUS * min(max(hits - 1, 0), MAX_REPEATS)
        elif pattern.search(haystack):
            hits = len(pattern.findall(haystack))
            score += HISTORY_BASE + weight + REPEAT_BONUS * min(max(hits - 1, 0), MAX_REPEATS)
    return score


def match_memes(
    text: str,
    path: Path = MEME_FILE,
    focus_text: str | None = None,
) -> list[dict[str, Any]]:
    """Return the most relevant library entries for ``text``.

    ``text`` is the whole scan window (current message + recent history).
    ``focus_text`` is the current message alone: a hit there outranks a hit that
    only came from something said a few turns ago. When omitted, every hit is
    scored as history - callers that only have one string lose the ranking
    bonus but keep the old "any keyword matches" behaviour.
    """
    if not text or not text.strip():
        return []
    haystack = _normalize(text)
    focus = _normalize(focus_text) if focus_text else ""
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for order, entry in enumerate(_entries(path)):
        score = _score(entry, focus, haystack)
        if score > 0:
            scored.append((score, order, entry))
    if not scored:
        return []
    # Higher score first; equal scores keep file order so tuning stays predictable.
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [entry for _, _, entry in scored[:MAX_MATCHED_MEMES]]


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


def memes_context(
    text: str,
    path: Path = MEME_FILE,
    focus_text: str | None = None,
) -> str:
    """Render the meme block for the system prompt, or '' when nothing matches."""
    hits = match_memes(text, path, focus_text)
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

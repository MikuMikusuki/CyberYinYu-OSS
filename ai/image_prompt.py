"""Prompt polishing for #画图 / #改图.

People type「画个猫」; gpt-image-2 needs subject, scene, framing, light and
style before it draws anything recognisable. This module lets the text model
fill those in.

The hard rule is that **a prompt the user already wrote must come back
untouched**. Two mechanisms enforce it, and they are deliberately separate:

  1. `needs_polish()` decides locally for the obvious cases (long, or already
     an English prompt) without spending a call at all.
  2. For everything else the model answers `{"polish": false, ...}` — and when
     it does, the caller uses the user's original string, *not* the copy the
     model echoed back. "Ask the model to return it unchanged" is not a
     guarantee; never reading its copy is.

Every failure path returns the original text: polishing is a nicety, and a
nicety must never stop a picture from being drawn.

Three speed/quality guards live here as well, all added after reading the
production log (2026-11-13):

  * **结果缓存** — 群里同一句「画个猫」十分钟内会被两个人打一遍；日志里
    「黑色高级轿车…」8 分钟内润色了两次、各 1.1s。命中缓存直接返回，
    一次调用都不花。
  * **画幅提示** — 出图尺寸可配成竖版/横版，模型不知道就会把竖图画成横向
    全景。把 size 折算成一句构图要求塞进 system，比事后重画便宜。
  * **判定为「不用改」时也保留 character** — 之前只要 polish=false 就整条
    丢弃，用户点名了角色却拿不到固定形象，白查一遍设定。
"""

import json
import logging
import re
import time
from collections import OrderedDict

from ai.chat_compat import THINKING_OFF, create_chat_completion

logger = logging.getLogger("cyberyinyu")

# 超过这个长度直接透传，连模型都不调。这个长度的中文描述通常已经把主体、
# 场景、风格说清楚了，再让模型「补」就是画蛇添足，还白花一次调用。
READY_CHARS = 80
# 英文词数达到这个量就当成「已经写好的英文提示词」。生图圈子里的英文 prompt
# 普遍带逗号分隔的风格词，数词比数字符更稳（英文 80 字符才十几个词）。
READY_WORDS = 12

# 缓存多少条润色结果。键里带 kind、张数档位和画幅提示，命中直接返回；
# 进程重启即失效——不落盘是故意的：提示词会随风格调整变化，落盘的旧结果
# 会一直骗人。
CACHE_LIMIT = 64

_ASCII_WORD = re.compile(r"[A-Za-z]{2,}")
# 我们的 schema 没有嵌套对象，所以「不含花括号的 {...}」就够，还不会被模型
# 在 JSON 前后多说的那句话骗到。
_JSON_OBJECT = re.compile(r"\{[^{}]*\}", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
# 模型偶尔给「提示词：xxx」这种前缀。
_LABEL = re.compile(r"^(?:最终)?(?:提示词|prompt)\s*[：:]\s*", re.IGNORECASE)
_QUOTE_PAIRS = {"「": "」", "『": "』", "“": "”", "‘": "’", "《": "》", '"': '"', "'": "'"}
_SIZE = re.compile(r"(\d{3,5})\s*[x×*]\s*(\d{3,5})")

GENERATE_SYSTEM = """你是文生图提示词工程师。用户会给你一句画面描述，你判断它够不够用来出图：不够就补全，够了就原样保留。

【补全】只补缺失的维度，缺什么补什么：
- 主体：外观、动作、数量、材质
- 场景与背景
- 构图与视角
- 光线与时间
- 风格与媒介（写实摄影 / 水彩 / 三维渲染 / 浮世绘 …）

【格式】最终提示词是一整段，用逗号分隔的短句；不要分点、不要编号、不要换行、不要加引号。

【保留】用户原句里的专有名词、数字、外文名和他自己写的形容词必须逐字保留，不要同义替换。

【绝对不要】
- 改变用户的主体意图，或加入用户没提到的具体物件、人物、文字
- 主动要求画面里出现文字、水印、签名、logo；只有用户明确要求时才照他的原话写
- 堆砌「8k、超高清、杰作、best quality」这类空话
- 输出英文（除非用户输入本身是英文）
- 超过 120 字（英文 60 词）

【判定】用户描述若已包含主体 + 场景或风格，就算「已经写好」：polish 设 false，prompt 一字不改地返回用户原句。只有明显缺信息（例如「一只猫」「画个好看的图」）才 polish 设 true。

【角色】用户若**明确点名**了已有的虚构角色或公众人物（动漫 / 游戏 / 影视 / 明星），把这个名字原样填进 character。
泛指（「一个女孩」「一只猫」「赛博朋克机甲」）填空字符串。
用户自己已经把角色长相写清楚了，character 也填空——他要的就是那个样子，不需要再查设定。

【输出】只输出一个 JSON 对象，不要解释、不要 markdown 代码块：
{"polish": true 或 false, "prompt": "最终提示词", "character": "角色名或空字符串"}

示例：
输入：一只猫
输出：{"polish": true, "prompt": "一只橘色虎斑猫坐在木质窗台上，午后阳光从侧面照进来，浅景深，背景是虚化的绿植，写实摄影风格", "character": ""}
输入：黄昏的海边，孤独的灯塔，长曝光，胶片颗粒，冷色调
输出：{"polish": false, "prompt": "黄昏的海边，孤独的灯塔，长曝光，胶片颗粒，冷色调", "character": ""}
输入：画个初音未来在雪山上跳舞
输出：{"polish": true, "prompt": "初音未来在雪山之巅跳舞，夜色蓝调光，雪花飞舞，广角仰拍，动画插画风格", "character": "初音未来"}
输入：画一个穿和服的女孩
输出：{"polish": true, "prompt": "一个穿和服的年轻女孩站在木质回廊上，夕阳斜照，浅景深，日系写真风格", "character": ""}"""

EDIT_SYSTEM = """你是图像编辑指令的改写助手。用户会给你一句「这张图要怎么改」，你判断它能不能让图像编辑模型准确执行：不能就补全，能就原样保留。

【补全】一条好的编辑指令要说清三件事，缺哪件补哪件：
- 改哪里：背景 / 服装 / 天气 / 光线 / 画风 / 局部元素
- 改成什么：具体形态、颜色、材质
- 什么保持不变：主体、人物特征、构图、原图里的文字

【格式】最终指令是一整段，用逗号分隔的短句；不要分点、不要编号、不要换行、不要加引号。

【保留】用户原句里的专有名词、数字、外文名和位置指代必须逐字保留，不要同义替换。

【绝对不要】
- 改动用户没提到的部分，或加入新的物件、人物、文字
- 堆砌空话
- 输出英文（除非用户输入本身是英文）
- 超过 120 字

【判定】用户若已说清「改哪里 + 改成什么」，就算「已经写好」：polish 设 false，prompt 一字不改地返回用户原句。

【角色】用户若**明确点名**了已有的虚构角色或公众人物（例如「把这个人换成初音未来」），把这个名字原样填进 character；泛指填空字符串。

【输出】只输出一个 JSON 对象，不要解释、不要 markdown 代码块：
{"polish": true 或 false, "prompt": "最终提示词", "character": "角色名或空字符串"}

示例：
输入：换成雪景
输出：{"polish": true, "prompt": "把背景换成积雪的冬日场景，天空阴沉、有细雪飘落，地面积雪；画面的主体、人物姿态和构图保持不变", "character": ""}
输入：把背景换成雪景，人物不动，保留原图的招牌文字
输出：{"polish": false, "prompt": "把背景换成雪景，人物不动，保留原图的招牌文字", "character": ""}
输入：把这个人换成初音未来
输出：{"polish": true, "prompt": "把画面中的人物替换为初音未来，保持原有姿态、构图和场景不变", "character": "初音未来"}"""

# 多图时才追加。实测过一次真实的坑：用户说「把第一张的池水改成第二张那样结冰」，
# 润色把它抹成了「把画面中的池水改成结冰」——两张图的分工没了，模型只能瞎猜。
# 所以指代必须逐字保留。
EDIT_MULTI_IMAGE_NOTE = """

【多图】用户这次给了不止一张图。他说的「第一张 / 第二张 / 这张 / 前一张 / 另一张」都是在指代这些图：
这些指代**必须原样保留**，不要改写成「画面中」「图中」「原图」这类模糊说法，否则模型分不清该动哪张。"""

_ASPECT_TALL = """

【画幅】这次出图是竖版（{width}x{height}）。按竖构图写：主体占画面中上部，上下留出空间；不要写「横向展开」「宽幅全景」这类描述。"""
_ASPECT_WIDE = """

【画幅】这次出图是横版（{width}x{height}）。按横构图写：主体偏一侧，另一侧留环境与景深；不要写「竖幅」「全身立绘」这类描述。"""

# (kind, 张数档位, 画幅提示, 原文) -> (prompt, 是否改写, 角色名)
_cache: "OrderedDict[tuple[str, int, str, str], tuple[str, bool, str]]" = OrderedDict()


def _cache_get(key: tuple[str, int, str, str]) -> tuple[str, bool, str] | None:
    value = _cache.get(key)
    if value is None:
        return None
    _cache.move_to_end(key)
    return value


def _cache_put(key: tuple[str, int, str, str], value: tuple[str, bool, str]) -> None:
    _cache[key] = value
    _cache.move_to_end(key)
    while len(_cache) > CACHE_LIMIT:
        _cache.popitem(last=False)


def clear_cache() -> None:
    """Drop memoised decisions — used by the offline regression, not by the bot."""
    _cache.clear()


def aspect_note(size: str) -> str:
    """Turn "1024x1536" into a framing requirement, or "" for square/unknown.

    方形画幅不用提示：模型对正方形最熟，多说一句只会挤掉别的维度。
    """
    match = _SIZE.search(size or "")
    if not match:
        return ""
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return ""
    ratio = width / height
    if ratio <= 0.9:
        return _ASPECT_TALL.format(width=width, height=height)
    if ratio >= 1.1:
        return _ASPECT_WIDE.format(width=width, height=height)
    return ""


def needs_polish(text: str, ready_chars: int = READY_CHARS) -> bool:
    """Cheap local gate: is this request obviously too terse to draw from?"""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if len(stripped) >= max(20, ready_chars):
        return False
    if len(_ASCII_WORD.findall(stripped)) >= READY_WORDS:
        return False
    return True


def _json_candidates(content: str) -> list[str]:
    """Every plausible JSON blob in the reply, most-specific first."""
    text = (content or "").strip()
    if not text:
        return []
    found: list[str] = []
    for match in _FENCE.finditer(text):
        found.append(match.group(1).strip())
    found.append(text)
    # 从后往前：模型常在 JSON 之后又补一句话，真正想给的那个在最后。
    found.extend(match.group(0) for match in reversed(list(_JSON_OBJECT.finditer(text))))
    return found


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "是"}:
            return True
        if lowered in {"false", "no", "0", "否"}:
            return False
    return None


def _strip_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and _QUOTE_PAIRS.get(text[0]) == text[-1]:
        return text[1:-1].strip()
    return text


def _clean_character(value: object) -> str:
    """Trim quotes/spaces; reject anything long enough to be the whole sentence."""
    if not isinstance(value, str):
        return ""
    name = _strip_quotes(" ".join(value.split())).strip("。.,，")
    if not name or len(name) > 40:
        return ""
    return name


def _clean_prompt(value: str) -> str:
    """Normalise the model's prompt field: no fences, no label, no wrapping quotes."""
    line = (value or "").strip()
    fenced = _FENCE.search(line)
    if fenced:
        line = fenced.group(1).strip()
    line = _LABEL.sub("", line)
    # 换行/制表对生图模型没有意义，压成单空格还能省 token。
    line = " ".join(line.split())
    return _strip_quotes(line)


def _parse_decision(content: str) -> dict[str, object] | None:
    for candidate in _json_candidates(content):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        prompt = data.get("prompt")
        if not isinstance(prompt, str):
            continue
        # polish 缺省视为「改写了」：模型既然给了 prompt 就是在回答；真原样
        # 抄回原文时，polish() 里的同一性检查会把它改判成「没改」。
        polish = _as_bool(data.get("polish"))
        if polish is None:
            polish = True
        # character 是后加的字段：老模型漏填时按「没有角色」处理，不算解析失败。
        return {
            "polish": polish,
            "prompt": prompt,
            "character": _clean_character(data.get("character", "")),
        }
    return None


async def polish(
    client: object,
    text: str,
    *,
    kind: str = "generate",
    model: str,
    image_count: int = 1,
    ready_chars: int = READY_CHARS,
    timeout_seconds: float = 15.0,
    max_tokens: int = 400,
    size: str = "",
) -> tuple[str, bool, str]:
    """Return (prompt_to_use, was_rewritten, character_name).

    `kind` picks the instruction shape: "generate" needs picture description,
    "edit" needs change-plus-preserve. `image_count` > 1 adds the rule that
    position references («第一张 / 第二张») must survive polishing verbatim.
    `size` (e.g. "1024x1536") only matters for "generate": it tells the model
    which way the canvas leans, so the framing matches what will be drawn.

    `character` comes back even when nothing was rewritten: a user who names a
    character and writes the rest well still gets the fixed look appended.
    """
    stripped = (text or "").strip()
    if not stripped:
        return text, False, ""

    note = aspect_note(size) if kind == "generate" else ""
    key = (kind, 2 if image_count > 1 else 1, note, stripped)
    cached = _cache_get(key)
    if cached is not None:
        logger.info("提示词命中润色缓存，跳过模型调用（%d 字）", len(stripped))
        return cached

    if not needs_polish(stripped, ready_chars):
        logger.info("提示词已够完整，原样使用（%d 字）", len(stripped))
        return text, False, ""

    system = EDIT_SYSTEM if kind == "edit" else GENERATE_SYSTEM
    if kind == "edit" and image_count > 1:
        system += EDIT_MULTI_IMAGE_NOTE
    if note:
        system += note
    started = time.time()
    try:
        response = await create_chat_completion(
            client,
            model=model,
            operation="image_prompt_polish",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": stripped},
            ],
            max_tokens=max_tokens,
            temperature=0.4,
            timeout=max(5.0, float(timeout_seconds)),
            thinking=THINKING_OFF,
        )
        content = (getattr(response.choices[0].message, "content", "") or "").strip()
    except Exception as exc:
        logger.warning("提示词润色失败，用原文: %s: %s", type(exc).__name__, exc)
        return text, False, ""

    decision = _parse_decision(content)
    if decision is None:
        logger.warning("润色输出不是预期 JSON，用原文: %r", content[:120])
        return text, False, ""

    character = str(decision["character"])
    if not decision["polish"]:
        logger.info(
            "模型判定提示词已够完整，原样使用（%.1fs）", time.time() - started
        )
        result = (text, False, character)
        _cache_put(key, result)
        return result

    polished = _clean_prompt(str(decision["prompt"]))
    if not polished:
        result = (text, False, character)
        _cache_put(key, result)
        return result
    # 长度闸：润色是补细节，不是重写整个场景。跑到原文 4 倍以上（且过百字）
    # 基本可以断定模型理解偏了，宁可要用户那句短的。
    if len(polished) > max(200, len(text) * 4):
        logger.warning(
            "润色结果异常膨胀（%d 字 -> %d 字），用原文", len(text), len(polished)
        )
        result = (text, False, character)
        _cache_put(key, result)
        return result
    # 模型把原文抄了一遍：当作没改，免得调用方以为改写成功。
    if polished == stripped:
        result = (text, False, character)
        _cache_put(key, result)
        return result

    logger.info(
        "提示词已润色（%.1fs%s）: %r -> %r",
        time.time() - started,
        "，含画幅提示" if note else "",
        text,
        polished,
    )
    result = (polished, True, character)
    _cache_put(key, result)
    return result


IDENTIFY_SYSTEM = """判断这句话里有没有**点名**某个已有的虚构角色或公众人物（动漫 / 游戏 / 影视 / 明星）。

只输出一个 JSON 对象，不要解释：
{"character": "名字"} 或 {"character": ""}

规则：
- 只填专有名词：初音未来、阿米娅、芙宁娜、雷姆、某个明星的名字
- 泛指一律留空：一个女孩、一只猫、机器人、赛博朋克角色、古风女子
- 昵称和别称也算：miku、兔子小姐（能对上某个角色时）
- 一句话里出现多个角色，填对画面最要紧的那个
- 名字最多 40 字，不要把这整句话塞进来

示例：
输入：把这个人换成初音未来
输出：{"character": "初音未来"}
输入：把背景换成雪景，人物不动
输出：{"character": ""}
输入：让她穿上阿米娅那套衣服
输出：{"character": "阿米娅"}"""

# #改图 每句都要过一次识别，而绝大多数指令里根本没有角色名。同一句话在群里
# 重发的概率不低（「换成雪景」这种），所以连「没名字」也一起缓存。
_IDENTIFY_CACHE_LIMIT = 64
_identify_cache: "OrderedDict[str, str]" = OrderedDict()


def clear_identify_cache() -> None:
    _identify_cache.clear()


async def identify_character(
    client: object,
    text: str,
    *,
    model: str,
    timeout_seconds: float = 10.0,
    max_tokens: int = 120,
) -> str:
    """Return the character named in `text`, or "" — never rewrites the text.

    改图的路径不润色（用户明确要求原话一个字都不动），但又需要知道有没有点名
    角色，好去查外貌。所以这里只做「认名字」这一件事：一次极短的 JSON 调用，
    和 polish() 相比不碰 prompt、不改写。
    """
    stripped = (text or "").strip()
    if not stripped:
        return ""
    if stripped in _identify_cache:
        _identify_cache.move_to_end(stripped)
        logger.info("角色识别命中缓存: %r", stripped[:30])
        return _identify_cache[stripped]

    started = time.time()
    try:
        response = await create_chat_completion(
            client,
            model=model,
            operation="character_identify",
            messages=[
                {"role": "system", "content": IDENTIFY_SYSTEM},
                {"role": "user", "content": stripped},
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            timeout=max(3.0, float(timeout_seconds)),
            thinking=THINKING_OFF,
        )
        content = (getattr(response.choices[0].message, "content", "") or "").strip()
    except Exception as exc:
        logger.warning("角色识别失败，按无角色处理: %s: %s", type(exc).__name__, exc)
        return ""

    name = None
    for candidate in _json_candidates(content):
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            name = _clean_character(data.get("character", ""))
            break
    if name is None:
        logger.warning("角色识别输出不是预期 JSON: %r", content[:100])
        return ""

    _identify_cache[stripped] = name
    _identify_cache.move_to_end(stripped)
    while len(_identify_cache) > _IDENTIFY_CACHE_LIMIT:
        _identify_cache.popitem(last=False)
    if name:
        logger.info("识别到角色（未改写描述）: %s（%.1fs）", name, time.time() - started)
    else:
        logger.info("这句话没有点名角色（%.1fs）", time.time() - started)
    return name

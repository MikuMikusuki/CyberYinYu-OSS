"""把长期记忆里的人称主体锚到 QQ 号上。

群名片会改、会重名，『他/她/我』离开当轮就再也对不上人。所以凡是要长期保存
的『谁是谁』，主体一律写成 ``QQ12345678（当时的群名片）``：QQ 号是稳定身份，
括号里的名片只是给模型看的辅助信息，改了也不影响指代。

纯函数，不读文件、不碰网络，方便单独回归。
"""

import re
from typing import Mapping

# 正文里已经出现过 QQ 号就不再加锚，避免二次改写（幂等）。
QQ_IN_TEXT = re.compile(r"QQ\s*号?\s*\d{5,12}", re.IGNORECASE)

# 主体 + 系词。主体不允许含空白与常见标点，避免把整句吃进去；
# ``{0,24}?`` 非贪婪，让『我是X』先匹配出主体『我』而不是『我是X』。
_IDENTITY_SPLIT = re.compile(
    r"^\s*(?P<subject>[^\s，,。；;、:：=＝]{0,24}?)\s*"
    r"(?P<verb>(?:其实|真名|名字|外号|本名|绰号)?\s*(?:是|叫|就是|即为|=|＝))"
    r"\s*(?P<rest>\S.*)$"
)

# 『我』类：主体就是本次发言者。
_SELF_PRONOUNS = frozenset({"我", "俺", "本人", "咱", "咱们", "咱家", "本人我"})
# 『他』类：主体是别人，必须另有依据（引用 / @ / 唯一被 @ 的人）才能确定。
_OTHER_PRONOUNS = frozenset(
    {"他", "她", "祂", "牠", "这个人", "那个人", "这人", "那人", "这货", "那货", "ta"}
)
# 『他们』这类复数不该被当成单数『他』处理：不在上面的集合里，自然就不会被改写。


def participant_label(qq: str, name: str = "") -> str:
    """``(12345678, '阿伟')`` → ``QQ12345678（阿伟）``；没名字时只有 QQ。"""
    digits = re.sub(r"\D", "", str(qq or ""))
    if not digits:
        return ""
    clean_name = " ".join(str(name or "").split()).strip("（）()")
    return f"QQ{digits}（{clean_name}）" if clean_name else f"QQ{digits}"


def _normalize_subject(subject: str) -> str:
    return subject.strip().lstrip("@＠").strip().casefold()


def split_identity(content: str) -> tuple[str, str, str] | None:
    """拆出 ``(主体, 系词, 谓语)``；不是『X是Y』这种句式时返回 None。"""
    match = _IDENTITY_SPLIT.match(content)
    if not match:
        return None
    return match.group("subject"), match.group("verb"), match.group("rest")


def has_bare_pronoun_subject(content: str) -> bool:
    """主体是不是没有 QQ 的裸人称代词（『他是X』『我是Y』）。

    存储层用它当最后一道闸：这类条目写进去也永远对不上人，不如不写。
    """
    parts = split_identity(" ".join(str(content or "").split()))
    if parts is None:
        return False
    subject = _normalize_subject(parts[0])
    return subject in _SELF_PRONOUNS or subject in _OTHER_PRONOUNS


def _match_member(
    subject: str, participants: Mapping[str, str]
) -> tuple[str, str] | None:
    """主体正好是某个被 @ 成员（或被叫到的人）的显示名时，反查他的 QQ。"""
    key = _normalize_subject(subject)
    if not key:
        return None
    for qq, name in participants.items():
        if name and _normalize_subject(str(name)) == key:
            return str(qq), str(name)
    return None


def anchor_memory_subject(
    content: str,
    *,
    speaker_qq: str = "",
    speaker_name: str = "",
    participants: Mapping[str, str] | None = None,
    quoted_qq: str = "",
    bot_qq: str = "",
) -> tuple[str, str]:
    """把记忆条目的主体改写成 QQ 号。

    返回 ``(改写后的内容, 状态)``，状态取值：

    - ``anchored``：已经锚到 QQ 号（本次改写成功，或原文本来就带 QQ）。
    - ``unchanged``：不是『谁是谁』句式，原样返回。
    - ``ambiguous``：主体是『他/她』这类代词，但本条消息里没有任何依据能确定
      是谁。调用方应当拒写并回一句追问，而不是把『他是X』存进去。

    只做确定性改写，不猜：宁可留原样，也不把代词安到不相干的人头上。
    """
    text = " ".join(str(content or "").split())
    if not text:
        return text, "unchanged"

    parts = split_identity(text)
    if parts is None:
        return text, "unchanged"
    subject, verb, rest = parts

    # 已经带 QQ 号：幂等返回。
    if QQ_IN_TEXT.search(text):
        return text, "anchored"

    key = _normalize_subject(subject)
    candidates = [
        (str(qq), str(name or ""))
        for qq, name in (participants or {}).items()
        if str(qq).isdigit() and str(qq) != str(bot_qq)
    ]

    label = ""
    status = "unchanged"

    if key in _SELF_PRONOUNS:
        label = participant_label(speaker_qq, speaker_name)
        status = "anchored" if label else "unchanged"
    elif key in _OTHER_PRONOUNS:
        if quoted_qq and str(quoted_qq).isdigit() and str(quoted_qq) != str(bot_qq):
            label = participant_label(quoted_qq, (participants or {}).get(quoted_qq, ""))
            status = "anchored" if label else "ambiguous"
        elif len(candidates) == 1:
            label = participant_label(*candidates[0])
            status = "anchored"
        else:
            status = "ambiguous"
    elif not key:
        # 主体整个丢了：多半是「记住 @某人 是X」里的 @ 段被剥走。
        if len(candidates) == 1:
            label = participant_label(*candidates[0])
            status = "anchored"
        elif len(candidates) > 1:
            status = "ambiguous"
    else:
        hit = _match_member(subject, {qq: name for qq, name in candidates})
        if hit is None and speaker_name and _normalize_subject(speaker_name) == key:
            hit = (str(speaker_qq), speaker_name) if speaker_qq else None
        if hit is not None:
            label = participant_label(*hit)
            status = "anchored"

    if status != "anchored" or not label:
        return text, status
    return f"{label}{verb}{rest}", "anchored"

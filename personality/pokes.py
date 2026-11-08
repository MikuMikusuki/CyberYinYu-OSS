"""戳一戳（OneBot notice: notify / poke）自动回复。

设计取舍：

* **纯本地词库，不调用模型。** 戳一戳是零信息量的互动，走模型要 1–5 秒；遇到
  混合思考模型还会先烧几百个 reasoning token，才回一句「嗯？」。这里直接秒回，
  根本不存在思考阶段（比传 ``thinking="off"`` 更彻底：连请求都不发）。
* **不进对话历史。** 戳一戳不是发言，写进上下文只会污染后面的回复。
* **群里不 @ 戳的人。** 被戳本来就是一次无声的互动，回一句就够；再 @ 一下等于
  在群里点名，人多的群会显得吵，也容易被当成机器人刷屏。
* 短句加权，贴着 style.txt 的短句倾向；同一个会话最近 6 条不重复。
* 冷却 + 每分钟条数上限：被人按住连戳时，机器人不会变成复读机刷屏。
* 词条可以写 ``[[表情名]]``（见 personality/stickers.json），渲染成表情或图片段；
  纯表情的词条权重最低，只当偶尔的「不想打字」。

回复词库是刻意写成「参考，不必全用」的形式：想改语气直接改 ``POKE_REPLIES``，
不用动 bot.py。
"""

import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from config import (
    BOT_QQ,
    POKE_REPLY_COOLDOWN_SECONDS,
    POKE_REPLY_ENABLED,
    POKE_REPLY_MAX_PER_MINUTE,
)
from personality.stickers import render_stickers

logger = logging.getLogger("cyberyinyu")

POKE_REPLIES: tuple[str, ...] = (
    # 惊吓 / 抗议
    "哈！",
    "吓我一跳",
    "干嘛突然戳",
    "呜呜不要戳我",
    "别戳了别戳了",
    "再戳就生气了",
    "喂 戳我干嘛",
    "轻点 疼",
    "戳坏了你赔吗",
    "我记住你了",
    "啊",
    "谁！",
    "干嘛",
    "住手",
    "停",
    "不许戳",
    "别突然戳",
    "又戳我",
    "吓死我了",
    "戳之前先说句话",
    "你礼貌吗",
    "我的脸不是按钮",
    "别把我当解压玩具",
    "一下就够",
    "说了别戳",
    "再戳试试",
    "戳我干嘛啦",
    # 玩笑式反将
    "戳我的是小狗",
    "谁戳我谁是小狗",
    "你再戳 小狗就是你了",
    "戳一下 小狗认证",
    "小狗名单+1",
    "再戳 把你写进名单",
    "别戳了 小狗",
    "戳我是吧 小狗",
    # 吐槽 / 反击
    "戳我干嘛 我又不是门铃",
    "一戳就响 满意了？",
    "手痒是吧",
    "你戳得动我吗",
    "看什么看 没见过机器人",
    "别戳了 我又不会掉装备",
    "再戳我就要收费了",
    "有事说事 别戳",
    "戳上瘾了？",
    "够了啊",
    "我很忙的",
    "忙着发呆",
    "别打断我",
    "无聊了是吧",
    "没别的事干",
    "有这功夫聊两句",
    "我又不是打地鼠",
    "打地鼠玩多了",
    "手闲可以去做题",
    "戳出火星子了",
    "记账了 一次一块",
    "先扫码",
    "戳我 是想被骂吗",
    "再戳拉黑（",
    "你等着",
    "记小本本了",
    "戳一下 显得你很闲",
    "要不你戳自己",
    "幼稚",
    "几岁了",
    "反弹",
    "这算互动吗",
    # 软萌 / 顺着玩
    "在呢在呢",
    "嗯？",
    "戳我干嘛呀",
    "想我了就直说",
    "陪你玩一下",
    "别闹啦",
    "好啦好啦 在",
    "在 说吧",
    "我在",
    "在的",
    "怎么啦",
    "嗯 听着呢",
    "来 说",
    "抱抱",
    "想聊什么",
    "陪你戳",
    "就一下哦",
    "戳吧戳吧",
    "找我玩吗",
    "怎么突然想我了",
    "陪你玩会儿",
    "好啦",
    "我在听",
    # 破防 / 自嘲
    "再戳要掉血了",
    "好家伙 又戳",
    "我招你惹你了",
    "今天已经被戳麻了",
    "别戳脸",
    "戳我可以 别戳头",
    "呜呜呜",
    "再戳就装死",
    "戳一下少一格电",
    "血条-1",
    "电量-1",
    "快被戳漏了",
    "戳出包了",
    "戳得我头晕",
    "我要记仇",
    "小本本记下了",
    "再戳就哭给你看",
    "呜呜 欺负人",
    "我去告状",
    "被戳麻了",
    "戳一下少一天寿命",
    "我裂开了",
    "戳碎了",
    "别戳了 要散架了",
    # 敷衍 / 冷淡
    "哦",
    "嗯",
    "？",
    "说",
    "啥",
    "咋",
    "又咋了",
    "讲",
    "然后呢",
    "所以呢",
    # 纯表情（权重最低）
    "[[呆]]",
    "[[问号]]",
    "[[没电]]",
)

# 越短越像随手回一句，权重越高。按**去掉表情标记后**的字数算，否则
# 「[[这么强]]」这种 6 个字面的标记会被当成短句，反而出现得更频繁。
_WEIGHT_TIERS: tuple[tuple[int, int], ...] = ((6, 3), (10, 2))
_STICKER_MARKER = re.compile(r"\[\[[^\[\]]{1,24}\]\]")


def _visible_length(line: str) -> int:
    return len(_STICKER_MARKER.sub("", line).strip())


def _weighted_pool(replies: tuple[str, ...]) -> tuple[str, ...]:
    pool: list[str] = []
    for line in replies:
        visible = _visible_length(line)
        weight = 1  # 纯表情词条：只当偶尔的调剂
        if visible:
            for limit, tier_weight in _WEIGHT_TIERS:
                if visible <= limit:
                    weight = tier_weight
                    break
        pool.extend([line] * weight)
    return tuple(pool)


def is_poke_notice(event: dict[str, Any]) -> bool:
    """True for OneBot poke notices, whichever shape the implementation uses."""
    if event.get("post_type") != "notice":
        return False
    notice_type = str(event.get("notice_type", "")).strip()
    if notice_type == "poke":
        return True
    return notice_type == "notify" and str(event.get("sub_type", "")).strip() == "poke"


def poke_targets_bot(event: dict[str, Any], bot_qq: str = BOT_QQ) -> bool:
    """True when this poke is aimed at the bot itself.

    Pokes between two other group members must stay silent; in a private chat any
    poke necessarily involves the bot, so a missing ``target_id`` is tolerated.
    """
    if not is_poke_notice(event):
        return False
    poker = str(event.get("user_id", "")).strip()
    if not poker or poker == bot_qq:
        return False
    target = str(event.get("target_id", "")).strip()
    if target:
        return target == bot_qq
    # 私聊事件里 group_id 可能是缺省、空串，也可能是 0（个别实现会填 0），
    # 三种都按私聊处理，否则会漏掉私聊戳。
    return event.get("group_id") in (None, "", 0, "0")


@dataclass(frozen=True)
class PokeReply:
    endpoint: str
    payload: dict[str, Any]
    text: str


@dataclass(frozen=True)
class PokeDecision:
    reply: PokeReply | None
    reason: str


class PokePicker:
    """Pick a line at random, avoiding the last few lines of this conversation."""

    def __init__(
        self,
        replies: tuple[str, ...] = POKE_REPLIES,
        rng: random.Random | None = None,
        *,
        history: int = 6,
        max_conversations: int = 200,
    ) -> None:
        self.replies = tuple(line for line in replies if line.strip())
        self._pool = _weighted_pool(self.replies)
        self._rng = rng or random.Random()
        self._history = max(1, int(history))
        self._max_conversations = max(1, int(max_conversations))
        # 每个会话只记最近几条，避免「A B A B」这种隔着一条的复读。
        self._recent: dict[str, deque[str]] = {}

    def pick(self, conversation_id: str = "") -> str:
        if not self.replies:
            return "哈！"
        recent = self._recent.get(conversation_id)
        if recent is None:
            recent = deque(maxlen=self._history)
        candidates = [line for line in self._pool if line not in recent] or list(self._pool)
        chosen = self._rng.choice(candidates)
        recent.append(chosen)
        # LRU：访问过的会话移到末尾，淘汰的永远是真正久未出现的那些。
        self._recent.pop(conversation_id, None)
        self._recent[conversation_id] = recent
        if len(self._recent) > self._max_conversations:
            self._recent.pop(next(iter(self._recent)), None)
        return chosen


class PokeResponder:
    """Turn a poke notice into a ready-to-send OneBot payload.

    Anti-spam state is per conversation (group or private chat), not per user:
    two people poking in the same group should not each get a separate reply
    within the cooldown window.
    """

    def __init__(
        self,
        picker: PokePicker | None = None,
        *,
        cooldown_seconds: float = POKE_REPLY_COOLDOWN_SECONDS,
        max_per_minute: int = POKE_REPLY_MAX_PER_MINUTE,
        enabled: bool = POKE_REPLY_ENABLED,
        clock=time.monotonic,
    ) -> None:
        self._picker = picker or PokePicker()
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._max_per_minute = max(1, int(max_per_minute))
        self._enabled = enabled
        self._clock = clock
        self._last_reply: dict[str, float] = {}
        self._recent: dict[str, list[float]] = {}
        self._max_conversations = 500

    def _remember(self, conversation: str, now: float) -> None:
        # LRU：先删后插，保证 next(iter(...)) 永远是「最久没被戳的会话」。
        self._last_reply.pop(conversation, None)
        self._last_reply[conversation] = now
        if len(self._last_reply) > self._max_conversations:
            oldest = next(iter(self._last_reply))
            self._last_reply.pop(oldest, None)
            self._recent.pop(oldest, None)

    def decide(self, event: dict[str, Any], bot_qq: str = BOT_QQ) -> PokeDecision:
        if not self._enabled:
            return PokeDecision(None, "disabled")
        if not is_poke_notice(event):
            return PokeDecision(None, "not-poke")
        if not poke_targets_bot(event, bot_qq):
            return PokeDecision(None, "not-bot")

        poker = str(event.get("user_id", "")).strip()
        group_raw = event.get("group_id")
        if group_raw not in (None, "", 0, "0"):
            try:
                group_id = int(str(group_raw))
            except (TypeError, ValueError):
                return PokeDecision(None, "bad-group")
            conversation = f"group:{group_id}"
            endpoint = "send_group_msg"
            target_key = "group_id"
            target_id: int | str = group_id
        else:
            if not poker.isdigit():
                return PokeDecision(None, "bad-user")
            conversation = f"private:{poker}"
            endpoint = "send_private_msg"
            target_key = "user_id"
            target_id = int(poker)

        now = self._clock()
        last = self._last_reply.get(conversation)
        if last is not None and now - last < self._cooldown:
            return PokeDecision(None, "cooldown")
        recent = [ts for ts in self._recent.get(conversation, []) if now - ts < 60.0]
        self._recent[conversation] = recent
        if len(recent) >= self._max_per_minute:
            return PokeDecision(None, "rate-limit")

        text = self._picker.pick(conversation)
        message: Any = render_stickers(text)
        if not message:
            # 词条里的 [[表情]] 一个都没在 stickers.json 里配置：别发空消息。
            logger.warning("戳一戳词条渲染为空，退回默认回复: %r", text)
            message = "嗯？"

        self._remember(conversation, now)
        recent.append(now)
        return PokeDecision(
            PokeReply(
                endpoint=endpoint,
                payload={target_key: target_id, "message": message},
                text=text,
            ),
            "ok",
        )

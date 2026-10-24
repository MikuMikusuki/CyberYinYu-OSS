"""Greetings used when someone pokes the bot without saying anything.

The lines stay short and mostly drop end punctuation, matching the persona in
personality/style.txt: no exclamation marks, no assistant-style pleasantries
like "有什么可以帮您". A bare "?" appears a few times because the style profile
sends a lone "?" rather than a punctuated question.
"""

import random

GREETINGS: tuple[str, ...] = (
    # 关心式
    "怎么啦",
    "咋了",
    "在呢 怎么了",
    "在呢在呢",
    "怎么了这是",
    "嗯？出什么事了",
    "在的 有事吗",
    # 轻松幽默式
    "被你@到了 说吧",
    "嘿 叫我干嘛",
    "叫我干嘛",
    "来了来了 说",
    "咋 想我了",
    "戳我一下是什么意思",
    "说吧 我听着呢",
    "干嘛 找我有事",
    # 简短直接式
    "嗯？",
    "？",
    "叫我？",
    "说",
    "在",
    "啥事",
    "嗯 你说",
)

# Longer lines are the ones the persona would use least often, so weight the
# pool towards the short ones instead of picking uniformly.
_LONG_THRESHOLD = 8
_SHORT_WEIGHT = 3


def _weighted_pool(greetings: tuple[str, ...]) -> tuple[str, ...]:
    pool: list[str] = []
    for line in greetings:
        pool.extend([line] * (1 if len(line) > _LONG_THRESHOLD else _SHORT_WEIGHT))
    return tuple(pool)


class GreetingPicker:
    """Picks a greeting at random, never repeating the previous one."""

    def __init__(self, greetings: tuple[str, ...] = GREETINGS, rng: random.Random | None = None):
        self.greetings = tuple(line for line in greetings if line.strip())
        self._pool = _weighted_pool(self.greetings)
        self._rng = rng or random.Random()
        self._last: dict[str, str] = {}
        self._max_conversations = 200

    def pick(self, conversation_id: str = "") -> str:
        if not self.greetings:
            return "在呢"
        previous = self._last.get(conversation_id, "")
        candidates = [line for line in self._pool if line != previous] or list(self._pool)
        chosen = self._rng.choice(candidates)
        if len(self._last) >= self._max_conversations and conversation_id not in self._last:
            self._last.pop(next(iter(self._last)))
        self._last[conversation_id] = chosen
        return chosen

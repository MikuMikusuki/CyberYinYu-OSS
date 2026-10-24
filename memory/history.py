"""Bounded, in-memory conversation history for short-term chat context."""

from collections import OrderedDict
from time import monotonic


class ConversationHistory:
    def __init__(self, max_turns: int = 8, ttl_seconds: int = 86_400, max_conversations: int = 200):
        self.max_turns = max(1, max_turns)
        self.ttl_seconds = max(60, ttl_seconds)
        self.max_conversations = max(1, max_conversations)
        self._items: OrderedDict[str, tuple[list[dict[str, str]], float]] = OrderedDict()

    def get(self, conversation_id: str) -> list[dict[str, str]]:
        now = monotonic()
        entry = self._items.pop(conversation_id, None)
        if entry is None:
            return []

        messages, last_active = entry
        if now - last_active > self.ttl_seconds:
            return []

        self._items[conversation_id] = (messages, now)
        return list(messages)

    def remember(self, conversation_id: str, user_message: str, assistant_message: str) -> None:
        now = monotonic()
        entry = self._items.pop(conversation_id, None)
        if entry is None or now - entry[1] > self.ttl_seconds:
            messages: list[dict[str, str]] = []
        else:
            messages = entry[0]

        messages.extend(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ]
        )
        messages = messages[-self.max_turns * 2 :]
        self._items[conversation_id] = (messages, now)

        while len(self._items) > self.max_conversations:
            self._items.popitem(last=False)

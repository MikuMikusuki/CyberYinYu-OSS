"""Bounded, in-memory conversation history for short-term chat context."""

from collections import OrderedDict
from time import monotonic


class ConversationHistory:
    def __init__(self, max_turns: int = 8, ttl_seconds: int = 86_400, max_conversations: int = 200):
        self.max_turns = max(1, max_turns)
        self.ttl_seconds = max(60, ttl_seconds)
        self.max_conversations = max(1, max_conversations)
        self._items: OrderedDict[str, tuple[list[dict[str, str]], float]] = OrderedDict()
        self._serial = 0
        self._versions: dict[str, tuple[int, int, int]] = {}

    def status(self, conversation_id: str) -> tuple[int, int, int]:
        entry = self._items.get(conversation_id)
        if entry is None or monotonic() - entry[1] > self.ttl_seconds:
            self._items.pop(conversation_id, None)
            self._versions.pop(conversation_id, None)
            return (0, 0, 0)
        return self._versions[conversation_id]

    def get(self, conversation_id: str) -> list[dict[str, str]]:
        now = monotonic()
        entry = self._items.pop(conversation_id, None)
        if entry is None:
            return []

        messages, last_active = entry
        if now - last_active > self.ttl_seconds:
            self._versions.pop(conversation_id, None)
            return []

        self._items[conversation_id] = (messages, now)
        return [dict(message) for message in messages]

    def clear(self, conversation_id: str) -> None:
        self._items.pop(conversation_id, None)
        self._versions.pop(conversation_id, None)

    def remember(self, conversation_id: str, user_message: str, assistant_message: str) -> None:
        now = monotonic()
        entry = self._items.pop(conversation_id, None)
        self._serial += 1
        if entry is None or now - entry[1] > self.ttl_seconds:
            messages: list[dict[str, str]] = []
            generation, turns = self._serial, 0
        else:
            messages = entry[0]
            generation, _, turns = self._versions[conversation_id]
        self._versions[conversation_id] = (generation, self._serial, turns + 1)

        messages.extend(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ]
        )
        messages = messages[-self.max_turns * 2 :]
        self._items[conversation_id] = (messages, now)

        while len(self._items) > self.max_conversations:
            evicted, _ = self._items.popitem(last=False)
            self._versions.pop(evicted, None)

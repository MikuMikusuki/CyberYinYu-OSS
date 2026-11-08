"""Run replies for different conversations in parallel, one at a time per chat.

Before this module the WebSocket loop awaited every event, so a single slow
model call (5-30s) froze every other chat behind it. Two guarantees replace
that, and they are independent:

* different conversations overlap - a private chat no longer waits for a busy
  group;
* the same conversation stays strictly ordered - replies and the history of one
  chat must not interleave, otherwise message 2 would be answered from a context
  that skips message 1, and the two replies could arrive reversed.

`submit()` never suspends, so the WebSocket reader stays responsive even while
the queue is full: taking a message in and doing the work for it are decoupled.

Book-keeping lives in one place: every task is registered in ``_tasks`` and
settled from its done-callback. That matters because a coroutine body that was
cancelled before its first step never runs ``finally`` - settling there would
leak the in-flight counters until the gate refused every message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("cyberyinyu")

Job = Callable[[], Awaitable[None]]


class ConversationGate:
    """Serialises jobs per conversation; lets different ones overlap.

    Three limits, all in messages:

    * ``max_concurrency`` - how many replies may talk to the model at once.
      Beyond this a job waits for a slot, so a burst of groups cannot open
      unbounded API calls.
    * ``max_pending_per_conversation`` - in-flight messages allowed for one chat,
      counting the one being answered. Someone sending five lines during a slow
      reply gets the first few answered; the excess is dropped instead of
      answered half an hour later, out of context.
    * ``max_inflight`` - global backlog across all chats, so a flood cannot
      grow the task list without bound.

    Ordering is enforced with one ``asyncio.Lock`` per conversation instead of a
    chain of "wait for the previous task". A chain breaks as soon as its tail is
    cancelled (the next submit would see a finished task and start immediately,
    while an earlier job is still running); a lock cannot.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        max_pending_per_conversation: int = 3,
        max_inflight: int = 64,
    ) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.max_pending_per_conversation = max(1, int(max_pending_per_conversation))
        self.max_inflight = max(1, int(max_inflight))

        # Created at import time on purpose: asyncio.Semaphore no longer binds a
        # loop in its constructor (3.10+), and this keeps bot.py free of
        # start-up ordering concerns.
        self._slots = asyncio.Semaphore(self.max_concurrency)
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._inflight = 0

        self.active = 0
        self.peak_active = 0
        self.completed = 0
        self.cancelled = 0
        self.dropped = 0

    # -- observability ----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        return {
            "limit": self.max_concurrency,
            "active": self.active,
            "peak_active": self.peak_active,
            "inflight": self._inflight,
            "waiting": max(0, self._inflight - self.active),
            "conversations": len(self._pending),
            "completed": self.completed,
            "cancelled": self.cancelled,
            "dropped": self.dropped,
        }

    # -- submission -------------------------------------------------------
    def pending_for(self, conversation_id: str) -> int:
        """How many jobs of this chat are queued or running right now."""
        return self._pending.get(conversation_id, 0)

    def submit(self, conversation_id: str, job: Job) -> bool:
        """Queue `job` behind every earlier job of the same conversation.

        Returns False when a backlog limit is hit; the caller decides what to
        tell the user (usually nothing). Never blocks.
        """
        if self._inflight >= self.max_inflight:
            self.dropped += 1
            logger.warning(
                "并发队列已满（%d 条在途，上限 %d），丢弃本条消息: conversation=%s",
                self._inflight,
                self.max_inflight,
                conversation_id,
            )
            return False

        queued = self._pending.get(conversation_id, 0)
        if queued >= self.max_pending_per_conversation:
            self.dropped += 1
            logger.warning(
                "会话积压 %d 条（上限 %d），丢弃本条消息: conversation=%s",
                queued,
                self.max_pending_per_conversation,
                conversation_id,
            )
            return False

        lock = self._locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[conversation_id] = lock

        task = asyncio.create_task(
            self._run(conversation_id, lock, job),
            name=f"reply:{conversation_id}",
        )
        self._tasks.add(task)
        self._pending[conversation_id] = queued + 1
        self._inflight += 1
        task.add_done_callback(lambda done, cid=conversation_id: self._on_done(cid, done))
        return True

    async def _run(
        self, conversation_id: str, lock: asyncio.Lock, job: Job
    ) -> None:
        cancelled = False
        # Lock first, slot second: every job of this chat runs alone and in
        # submission order, and a job only holds a model slot while it works.
        async with lock:
            async with self._slots:
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
                try:
                    await job()
                except asyncio.CancelledError:
                    cancelled = True
                    raise
                except Exception:
                    # 兜底：job 自己没接住的异常不能变成"task 异常未取出"，
                    # 更不能让同一会话后面排队的消息跟着一起死。
                    logger.exception("会话任务失败: conversation=%s", conversation_id)
                finally:
                    self.active -= 1
                    if not cancelled:
                        self.completed += 1

    def _on_done(self, conversation_id: str, task: asyncio.Task[None]) -> None:
        """Settle a job however it ended: finished, failed, or never started.

        ``Task.cancel()`` before the first step means the coroutine body never
        runs, so this callback - not a ``finally`` inside the body - is the only
        place guaranteed to see every job.
        """
        self._tasks.discard(task)
        self._inflight -= 1
        if task.cancelled():
            self.cancelled += 1

        remaining = self._pending.get(conversation_id, 1) - 1
        if remaining > 0:
            self._pending[conversation_id] = remaining
            return
        self._pending.pop(conversation_id, None)
        # The lock is dropped only when the chat is idle; popping it while a
        # waiter is still queued would hand the next job a brand-new lock.
        lock = self._locks.get(conversation_id)
        if lock is not None and not lock.locked():
            self._locks.pop(conversation_id, None)

    # -- shutdown ---------------------------------------------------------
    def snapshot_tasks(self) -> list[asyncio.Task[None]]:
        """Every live job, not just the newest one per chat."""
        return [task for task in self._tasks if not task.done()]

    def cancel_all(self) -> None:
        for task in self.snapshot_tasks():
            task.cancel()

"""Local, group-scoped long-term memory loaded into that group's prompt only."""

import json
import logging
import re
import threading
import uuid
from datetime import date
from pathlib import Path
from typing import Any


GROUP_MEMORY_DIR = Path(__file__).resolve().parent / "groups"
ALLOWED_CATEGORIES = {
    "group_context": "群背景",
    "shared_context": "共同背景",
    "recurring_topics": "常聊话题",
    "inside_jokes": "群内梗",
    "group_preferences": "群偏好",
    "ongoing_threads": "持续话题",
}
MAX_MEMORIES = 200
MAX_MEMORY_CHARS = 300
# NOTE: this is the *injection* cap, not the storage cap. context_for() renders
# entries in stored order and then truncates at MAX_PROMPT_CHARS, so only the
# first few entries actually reach the model — raising MAX_MEMORIES alone means
# the extra entries are kept but never shown. Raise this too if the older
# entries need to stay visible.
MAX_PROMPT_CHARS = 2400
ALLOWED_CATEGORY_IDS = tuple(ALLOWED_CATEGORIES)
SENSITIVE_PATTERN = re.compile(
    r"手机号|电话号码|身份证|住址|家庭住址|密码|验证码|银行卡|信用卡|病史|诊断|处方|用药|自伤|银行卡号|真实姓名",
    re.IGNORECASE,
)
ALIAS_RELATION_PATTERN = re.compile(
    r"^\s*([A-Za-z0-9\u4e00-\u9fff_.-]{1,32})\s*(?:是|叫|=|＝|即为)\s*"
    r"([A-Za-z0-9\u4e00-\u9fff_.-]{1,32})\s*[。.!！]?\s*$"
)
logger = logging.getLogger(__name__)


def _derived_alias_chains(memories: list[dict[str, Any]]) -> list[str]:
    """Derive only unambiguous, multi-step name/alias chains from group facts."""
    edges: dict[str, dict[str, str]] = {}
    displays: dict[str, str] = {}
    for item in memories:
        if item.get("category") != "shared_context":
            continue
        content = item.get("content")
        if not isinstance(content, str):
            continue
        match = ALIAS_RELATION_PATTERN.fullmatch(content)
        if not match:
            continue
        source, target = (part.strip() for part in match.groups())
        source_key, target_key = source.casefold(), target.casefold()
        if source_key == target_key:
            continue
        displays.setdefault(source_key, source)
        displays.setdefault(target_key, target)
        edges.setdefault(source_key, {})[target_key] = displays[target_key]

    incoming = {target for outgoing in edges.values() for target in outgoing}
    derived: list[str] = []
    for start in edges:
        if start in incoming:
            continue
        chain = [displays[start]]
        current = start
        seen = {start}
        while current in edges and len(edges[current]) == 1:
            next_key, next_display = next(iter(edges[current].items()))
            if next_key in seen:
                break
            chain.append(next_display)
            seen.add(next_key)
            current = next_key
        if len(chain) >= 3 and current not in edges:
            names = " = ".join(chain)
            derived.append(
                f"- 可传递的称呼链（同一指代）：{names}；询问链中前项时可沿链回答末项。"
            )
    return derived


class GroupMemoryStore:
    def __init__(self, directory: Path = GROUP_MEMORY_DIR) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        # Per-group locks serialising the read-modify-write window in
        # apply_decision plus the init/repair writes in _load. Locking lives
        # here (not at the call sites) so the judge / save / owner-confirm
        # paths cannot overwrite each other from stale snapshots.
        # threading.RLock (not asyncio.Lock) is deliberate: the critical
        # sections hold no awaits — pure local file I/O — so a synchronous
        # lock never stalls the event loop, apply_decision may re-enter via
        # _load, and existing callers (bot.py, deploy probes) need no changes.
        # Every code path runs on the single event-loop thread, where
        # dict.setdefault is atomic; do NOT call this store from other threads.
        self._locks: dict[str, threading.RLock] = {}

    def _lock_for(self, group_id: str) -> threading.RLock:
        return self._locks.setdefault(str(group_id), threading.RLock())

    @staticmethod
    def _fresh_profile(group_id: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "group_id": str(group_id),
            "group_name": "",
            "memories": [],
        }

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)

    @property
    def allowed_categories(self) -> tuple[str, ...]:
        return ALLOWED_CATEGORY_IDS

    def _path_for(self, group_id: str) -> Path | None:
        group_id = str(group_id)
        if not re.fullmatch(r"\d+", group_id):
            return None
        return self.directory / f"{group_id}.json"

    def context_for(self, group_id: str) -> str:
        data = self._load(group_id)
        if data is None:
            return ""

        memories = data.get("memories", [])
        if not isinstance(memories, list):
            return ""

        lines: list[str] = []
        valid_memories: list[dict[str, Any]] = []
        for item in memories[:MAX_MEMORIES]:
            if not isinstance(item, dict):
                continue
            category = item.get("category")
            content = item.get("content")
            if (
                not isinstance(category, str)
                or category not in ALLOWED_CATEGORIES
                or not isinstance(content, str)
            ):
                continue
            content = " ".join(content.split())[:MAX_MEMORY_CHARS]
            if content:
                lines.append(f"- {ALLOWED_CATEGORIES[category]}：{content}")
                valid_memories.append({"category": category, "content": content})

        if not lines:
            return ""
        heading = f"本群（群号 {group_id}）的长期背景候选项。只在与当前消息相关时参考：\n"
        derived = _derived_alias_chains(valid_memories)
        sections = []
        if derived:
            sections.append("可传递称呼关系（由下方记忆推导）：\n" + "\n".join(derived))
        sections.append("记忆条目：\n" + "\n".join(lines))
        rendered = heading + "\n\n".join(sections)
        return rendered[:MAX_PROMPT_CHARS]

    def _load(self, group_id: str) -> dict[str, Any] | None:
        path = self._path_for(group_id)
        if path is None:
            return None
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = None  # corrupted → locked repair path below
            except OSError as exc:
                logger.warning("读取群记忆文件失败: group=%s path=%s error=%s", group_id, path, exc)
                return None
            if isinstance(data, dict) and str(data.get("group_id", "")) == str(group_id):
                return data
        with self._lock_for(group_id):
            if not path.exists():
                # Create an isolated, empty profile for a newly encountered
                # group. Atomic (tmp + replace) and locked, so racing creators
                # (e.g. main reply vs. #搜索) cannot interleave half-written
                # JSON.
                data = self._fresh_profile(group_id)
                self._atomic_write(path, data)
                return data
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                # Corrupted or truncated file: rebuild an empty profile once,
                # otherwise apply_decision would fail on _load forever and
                # this group's memory writes stay silently dead.
                logger.warning(
                    "群记忆文件损坏，重建为空档案: group=%s path=%s error=%s",
                    group_id, path, exc,
                )
                data = self._fresh_profile(group_id)
                self._atomic_write(path, data)
            except OSError as exc:
                logger.warning("读取群记忆文件失败: group=%s path=%s error=%s", group_id, path, exc)
                return None
        if not isinstance(data, dict):
            return None
        if str(data.get("group_id", "")) != str(group_id):
            return None
        return data

    def memories_for_judging(self, group_id: str) -> list[dict[str, Any]]:
        data = self._load(group_id)
        if data is None or not isinstance(data.get("memories"), list):
            return []
        return [
            item for item in data["memories"]
            if isinstance(item, dict)
            and item.get("category") in ALLOWED_CATEGORIES
            and isinstance(item.get("content"), str)
        ][:MAX_MEMORIES]

    def apply_decision(
        self,
        group_id: str,
        decision: dict[str, Any],
        source: str = "DeepSeek自动归纳",
    ) -> bool:
        """Apply a validated DeepSeek decision to this group's local profile."""
        path = self._path_for(group_id)
        if path is None:
            return False
        # Serialise the whole read-modify-write window per group. A writer
        # always re-reads under the lock, so a concurrent judge/save/confirm
        # can interleave but never silently overwrite (no lost update).
        with self._lock_for(group_id):
            data = self._load(group_id)
            if data is None:
                return False
            memories = data.get("memories")
            if not isinstance(memories, list):
                return False

            action = decision.get("action")
            if not isinstance(action, str):
                return False
            if action == "none":
                return False
            if decision.get("confidence") != "high":
                return False

            if action == "delete":
                memory_id = decision.get("memory_id")
                updated = [item for item in memories if not (
                    isinstance(item, dict) and item.get("id") == memory_id
                )]
                if len(updated) == len(memories):
                    return False
                data["memories"] = updated
            elif action in {"add", "update"}:
                category = decision.get("category")
                content = decision.get("content")
                confidence = decision.get("confidence")
                if (
                    not isinstance(category, str)
                    or category not in ALLOWED_CATEGORIES
                    or not isinstance(content, str)
                    or confidence != "high"
                ):
                    return False
                content = " ".join(content.split()).strip()[:MAX_MEMORY_CHARS]
                if not content or SENSITIVE_PATTERN.search(content):
                    return False

                entry = {
                    "id": str(decision.get("memory_id") or uuid.uuid4().hex[:12]),
                    "category": category,
                    "content": content,
                    "confidence": confidence,
                    "source": source[:80],
                    "updated_at": date.today().isoformat(),
                }
                if action == "update":
                    memory_id = decision.get("memory_id")
                    index = next((i for i, item in enumerate(memories)
                                  if isinstance(item, dict) and item.get("id") == memory_id), None)
                    if index is None:
                        return False
                    memories[index] = entry
                else:
                    if len(memories) >= MAX_MEMORIES:
                        return False
                    normalized = content.casefold()
                    if any(
                        isinstance(item, dict)
                        and item.get("category") == category
                        and isinstance(item.get("content"), str)
                        and " ".join(item["content"].split()).casefold() == normalized
                        for item in memories
                    ):
                        return False
                    memories.append(entry)
                data["memories"] = memories
            else:
                return False

            self._atomic_write(path, data)
            return True

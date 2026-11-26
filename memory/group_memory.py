"""Local, group-scoped long-term memory loaded into that group's prompt only."""

import json
import logging
import re
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from memory.subject import QQ_IN_TEXT, has_bare_pronoun_subject


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
# 称呼链只对「X是Y」这种纯别名句生效。X/Y 允许是带 QQ 锚的形式
# （QQ12345678（阿伟）），否则锚定后的条目会静默掉出链条推导。
_RELATION_TERM = (
    r"(?:QQ\s*号?\s*\d{5,12}(?:（[^）]{1,32}）)?|[A-Za-z0-9\u4e00-\u9fff_.-]{1,32})"
)
ALIAS_RELATION_PATTERN = re.compile(
    rf"^\s*({_RELATION_TERM})\s*(?:是|叫|=|＝|即为)\s*"
    rf"({_RELATION_TERM})\s*[。.!！]?\s*$"
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


# 注入时的实时昵称改写。存储层永远是「QQ号（当时的名片）」这个稳定锚，
# 渲染成提示词时才把括号里的名字换成群成员列表里查到的最新名片——模型
# 回答「谁是谁」时用人话说称呼，而不是念一串 QQ 号。
QQ_ANCHOR_RE = re.compile(
    r"QQ\s*号?\s*(\d{5,12})(?:（([^）]{0,64})）)?",
    re.IGNORECASE,
)


def render_with_current_nicknames(
    text: str, nicknames: Mapping[str, str] | None
) -> str:
    """把条目里每个 QQ 锚的括号名换成最新群名片；查不到的保持原样。

    纯函数、无副作用：不改存储，只改喂给模型（或 #记忆 列表）的那一份。
    """
    if not nicknames:
        return text

    def _sub(match: re.Match[str]) -> str:
        qq = match.group(1)
        current = " ".join(str(nicknames.get(qq, "")).split())
        if not current:
            return match.group(0)
        return f"QQ{qq}（{current}）"

    return QQ_ANCHOR_RE.sub(_sub, text)


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
            # 群记忆开关：缺省视为开启。只由群主 / 管理员 / owner通过
            # #关闭记忆 / #开启记忆 修改，权限判定在 bot.py，存储层只执行。
            "memory_enabled": True,
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

    def memory_enabled(self, group_id: str) -> bool:
        """本群记忆是否开启。读不到档案时按开启处理（别把功能意外关掉）。"""
        data = self._load(group_id)
        if data is None:
            return True
        return data.get("memory_enabled") is not False

    def set_memory_enabled(
        self,
        group_id: str,
        enabled: bool,
        *,
        operator_qq: str = "",
        operator_label: str = "",
        source: str = "",
    ) -> tuple[bool, bool]:
        """开/关群记忆，返回 ``(是否写入成功, 状态是否真的变了)``。

        权限判定不在这里——调用方（bot.py 的命令层）已经确认过是群主、
        管理员或owner。存储层只负责原子落盘并留痕。
        """
        path = self._path_for(group_id)
        if path is None:
            return False, False
        with self._lock_for(group_id):
            data = self._load(group_id)
            if data is None:
                return False, False
            current = data.get("memory_enabled") is not False
            if current == bool(enabled):
                return True, False
            data["memory_enabled"] = bool(enabled)
            data["memory_toggled_by"] = str(operator_qq)
            data["memory_toggled_name"] = str(operator_label)[:40]
            data["memory_toggled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data["memory_toggle_source"] = str(source)[:80]
            self._atomic_write(path, data)
            return True, True

    def context_for(
        self, group_id: str, nicknames: Mapping[str, str] | None = None
    ) -> str:
        """渲染本群长期记忆；nicknames 是 {qq: 最新群名片}，可选。"""
        data = self._load(group_id)
        if data is None:
            return ""
        if data.get("memory_enabled") is False:
            # 关掉就是关掉：既不注入也不提示，模型当它不存在。
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
                content = render_with_current_nicknames(content, nicknames)
                lines.append(f"- {ALLOWED_CATEGORIES[category]}：{content}")
                valid_memories.append({"category": category, "content": content})

        if not lines:
            return ""
        heading = f"本群（群号 {group_id}）的长期背景候选项。只在与当前消息相关时参考：\n"
        if any(QQ_IN_TEXT.search(line) for line in lines):
            if nicknames:
                heading += (
                    "（条目里的 QQ号 只是内部身份锚点，括号里已经换成最新的群名片；"
                    "提到人时用群名片称呼，不要把 QQ 号念出来。）\n"
                )
            else:
                heading += (
                    "（条目里的 QQ12345678 是稳定身份，括号里是当时的群名片，"
                    "可能已经改过；提到人时优先用括号里的名片称呼，"
                    "不要把 QQ 号念出来。）\n"
                )
        derived = _derived_alias_chains(valid_memories)
        sections = []
        if derived:
            rendered_derived = [
                render_with_current_nicknames(line, nicknames) for line in derived
            ]
            sections.append("可传递称呼关系（由下方记忆推导）：\n" + "\n".join(rendered_derived))
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

    # -- 人工编辑（Web 控制台 /admin 的「记忆」页）-------------------------
    # 校验规则和 apply_decision 完全一致，区别只在返回「原因」：从 apply_decision
    # 的一个 False 里看不出是哪条规矩挡的，人工编辑必须把原因回显给人看。
    def upsert_entry(
        self,
        group_id: str,
        *,
        category: str,
        content: str,
        entry_id: str = "",
        source: str = "WebUI 人工编辑",
    ) -> tuple[bool, str]:
        """新增或改写一条记忆，返回 ``(是否写入, 原因)``。"""
        path = self._path_for(group_id)
        if path is None:
            return False, "群号只能是纯数字"
        if category not in ALLOWED_CATEGORIES:
            return False, f"分类必须是 {('/'.join(ALLOWED_CATEGORY_IDS))} 之一"
        text = " ".join(str(content or "").split()).strip()
        if not text:
            return False, "内容不能为空"
        if len(text) > MAX_MEMORY_CHARS:
            return False, f"内容 {len(text)} 字，超过 {MAX_MEMORY_CHARS} 字上限"
        if SENSITIVE_PATTERN.search(text):
            return False, "命中隐私词（手机号/身份证/密码/住址等），记忆里不存这类信息"
        if has_bare_pronoun_subject(text):
            return False, "主语是「他/她/我」这类代词，离开当轮就对不上人；请写成「QQ12345678（昵称）」"

        with self._lock_for(group_id):
            data = self._load(group_id)
            if data is None:
                return False, "读不到这个群的记忆档案"
            if data.get("memory_enabled") is False:
                return False, "本群记忆已关闭，先在右上角打开再写入"
            memories = data.get("memories")
            if not isinstance(memories, list):
                return False, "记忆档案结构不对（memories 不是列表）"

            today = date.today().isoformat()
            if entry_id:
                index = next(
                    (
                        i
                        for i, item in enumerate(memories)
                        if isinstance(item, dict) and item.get("id") == entry_id
                    ),
                    None,
                )
                if index is None:
                    return False, "这条记忆已经不在了（可能刚被删掉），刷新一下"
                entry = dict(memories[index])
                entry["category"] = category
                entry["content"] = text
                entry["source"] = source[:80]
                entry["updated_at"] = today
                memories[index] = entry
            else:
                if len(memories) >= MAX_MEMORIES:
                    return False, f"已经到 {MAX_MEMORIES} 条上限，先删几条再写"
                normalized = text.casefold()
                if any(
                    isinstance(item, dict)
                    and item.get("category") == category
                    and isinstance(item.get("content"), str)
                    and " ".join(item["content"].split()).casefold() == normalized
                    for item in memories
                ):
                    return False, "同分类下已经有一模一样的一条"
                memories.append(
                    {
                        "id": uuid.uuid4().hex[:12],
                        "category": category,
                        "content": text,
                        "source": source[:80],
                        "updated_at": today,
                    }
                )
            data["memories"] = memories
            self._atomic_write(path, data)
        return True, "已保存"

    def delete_entry(self, group_id: str, entry_id: str) -> tuple[bool, str]:
        """按 id 删一条。记忆关着也放行——关着的时候正是清理的时机。"""
        path = self._path_for(group_id)
        if path is None:
            return False, "群号只能是纯数字"
        if not entry_id:
            return False, "缺少条目 id"
        with self._lock_for(group_id):
            data = self._load(group_id)
            if data is None:
                return False, "读不到这个群的记忆档案"
            memories = data.get("memories")
            if not isinstance(memories, list):
                return False, "记忆档案结构不对（memories 不是列表）"
            updated = [
                item
                for item in memories
                if not (isinstance(item, dict) and item.get("id") == entry_id)
            ]
            if len(updated) == len(memories):
                return False, "没找到这条记忆"
            data["memories"] = updated
            self._atomic_write(path, data)
        return True, "已删除"

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

            if action in {"add", "update"} and data.get("memory_enabled") is False:
                # 关掉的群一律不写入新内容；删除仍然放行，方便关着的时候清理。
                logger.info(
                    "群记忆已关闭，拒绝写入: group=%s action=%s", group_id, action
                )
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
                if has_bare_pronoun_subject(content):
                    # 兜底闸：「他是X」「我是Y」离开当轮就再也对不上人。
                    # 上层（bot.process_memory_request）本该先锚成 QQ 号，
                    # 走到这里还没锚住说明确实无法确定主体，宁可不写。
                    logger.warning(
                        "拒绝写入主语不明的群记忆: group=%s content=%s",
                        group_id,
                        content[:40],
                    )
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

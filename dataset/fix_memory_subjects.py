"""存量群记忆修复：把裸人称主体补成 QQ 号，并清掉「记住 了…」留下的残句前缀。

默认只体检、不写入（dry-run）。确认后再加 --apply 落盘。

可自动修的只有「我」——`source` 里带着发言者 QQ（形如 `成员请求经审核:1000000001`），
主体就是那个人。`他是X` 无法从库里反推出「他」是谁，只能列出来让人重说一遍。

用法：
  python3 dataset/fix_memory_subjects.py           # 体检
  python3 dataset/fix_memory_subjects.py --apply   # 落盘（会先备份）
"""
import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory.group_memory import GroupMemoryStore  # noqa: E402
from memory.subject import (  # noqa: E402
    anchor_memory_subject,
    has_bare_pronoun_subject,
    split_identity,
)

GROUPS = ROOT / "memory" / "groups"
SOURCE_QQ = re.compile(r":\s*(\d{5,12})\s*$")
# 「记住 了xxx」→ 内容首字是「了」，且去掉后确实是一句完整的事实。
STRAY_LEADING = re.compile(r"^了(?=.{2,})")


def classify(entry: dict[str, Any]) -> tuple[str, str, str]:
    """→ (结论, 新内容, 说明)。结论：ok / fix / manual / stray。"""
    content = " ".join(str(entry.get("content") or "").split())
    if not content:
        return "ok", content, "空条目"

    match = SOURCE_QQ.search(str(entry.get("source") or ""))
    source_qq = match.group(1) if match else ""

    if has_bare_pronoun_subject(content):
        subject = (split_identity(content) or ("",))[0]
        if subject.strip().lstrip("@＠") in {"我", "俺", "本人", "咱"} and source_qq:
            fixed = anchor_memory_subject(
                content, speaker_qq=source_qq, speaker_name=""
            )[0]
            if fixed != content:
                return "fix", fixed, f"「我」→ QQ{source_qq}（取自 source）"
        return "manual", content, "主体是「他/她」，库里没有任何线索能确定是谁"

    if STRAY_LEADING.match(content) and split_identity(content[1:]):
        return "stray", content[1:], "去掉「记住 了…」留下的前缀「了」"

    return "ok", content, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真的写回（默认只看）")
    args = parser.parse_args()

    if not GROUPS.is_dir():
        print(f"没有群记忆目录: {GROUPS}")
        return 1

    store = GroupMemoryStore(directory=GROUPS)
    todo: list[tuple[str, dict[str, Any], str, str, str]] = []
    stats = {"ok": 0, "fix": 0, "manual": 0, "stray": 0}

    for path in sorted(GROUPS.glob("*.json")):
        group_id = path.stem
        if not group_id.isdigit():
            continue
        for entry in store.memories_for_judging(group_id):
            verdict, new_content, reason = classify(entry)
            stats[verdict] += 1
            if verdict in {"fix", "stray"}:
                todo.append((group_id, entry, verdict, new_content, reason))
            elif verdict == "manual":
                print(f"[需人工] 群 {group_id}  id={entry.get('id')}")
                print(f"         内容: {entry.get('content')!r}")
                print(f"         原因: {reason}")
                print(f"         处理: 让本人重说一次（@他 或带 QQ 号），"
                      f"或 #删除记忆 {entry.get('id')}")

    print("\n" + "=" * 70)
    print(f"体检结果: 正常 {stats['ok']} / 可自动修 {stats['fix']} / "
          f"残句 {stats['stray']} / 需人工 {stats['manual']}")
    for group_id, entry, verdict, new_content, reason in todo:
        print(f"\n[{'修' if verdict == 'fix' else '清理'}] 群 {group_id}  id={entry.get('id')}")
        print(f"   原: {entry.get('content')!r}")
        print(f"   新: {new_content!r}")
        print(f"   因: {reason}")

    if not args.apply:
        print("\n（dry-run：没有写入。确认无误后加 --apply）")
        return 0
    if not todo:
        print("\n没有需要写入的条目。")
        return 0

    backup = GROUPS.parent / f"groups.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copytree(GROUPS, backup)
    print(f"\n备份: {backup}")

    saved = 0
    for group_id, entry, verdict, new_content, _ in todo:
        existing = [
            item for item in store.memories_for_judging(group_id)
            if item.get("id") != entry.get("id")
            and " ".join(str(item.get("content") or "").split()) == new_content
        ]
        if existing:
            # 修完会变成重复条目（多为「记住 了…」留下的残句），直接删掉残留。
            ok = store.apply_decision(
                group_id,
                {"action": "delete", "memory_id": entry.get("id"), "confidence": "high"},
                source="主体锚定修复:去重",
            )
            print(f"  {'OK  ' if ok else 'SKIP'} 群 {group_id} {entry.get('id')} → 重复，已删除")
            saved += 1 if ok else 0
            continue
        ok = store.apply_decision(
            group_id,
            {
                "action": "update",
                "memory_id": entry.get("id"),
                "category": entry.get("category"),
                "content": new_content,
                "confidence": "high",
            },
            source=f"主体锚定修复:{entry.get('source') or '?'}",
        )
        print(f"  {'OK  ' if ok else 'SKIP'} 群 {group_id} {entry.get('id')} → {new_content!r}")
        saved += 1 if ok else 0
    print(f"\n写入 {saved}/{len(todo)} 条。回滚: cp -a {backup}/* {GROUPS}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

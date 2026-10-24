"""Clean every QQChatExporter export in a folder into local style corpora.

Extends clean_chat_export.py to a whole export folder:
  * tolerant loading (strict JSON -> trailing-comma repair -> object salvage)
  * multiple target accounts, deduplicated across overlapping re-exports
  * keeps the text of quoted-reply / mixed messages instead of dropping them
  * tags scene (friend/group) and chat name, masks sensitive numbers

Raw exports stay untouched; output is written under dataset/cleaned/.
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = PROJECT_DIR / "dataset" / "import"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "dataset" / "cleaned"

# 目标账号通过 --target QQ=昵称 指定；不设默认值，避免把任何真实账号写进代码。
DEFAULT_TARGETS: dict[str, str] = {}

TRAILING_COMMA = re.compile(r",(\s*[\]}])")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
LONG_DIGITS = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
REPLY_PREFIX = re.compile(r"^\[回复消息\]")

# Non-text payload segments: the message may still carry usable text.
MEDIA_TYPES = {
    "image", "sticker", "mface", "face", "video", "file", "audio",
    "forward", "json", "music", "ptt", "record", "ark", "markdown",
}
# Quoted-message segments carry the *other* person's words; never treat as ours.
QUOTE_TYPES = {"reply"}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def salvage_messages(text: str) -> list[dict[str, Any]]:
    """Recover message objects from a truncated or malformed export."""
    decoder = json.JSONDecoder()
    recovered: list[dict[str, Any]] = []
    cursor = 0
    while True:
        start = text.find('{"id":', cursor)
        if start < 0:
            return recovered
        try:
            obj, end = decoder.raw_decode(text, start)
        except ValueError:
            cursor = start + 1
            continue
        if isinstance(obj, dict) and "sender" in obj and "type" in obj:
            recovered.append(obj)
        cursor = end


def load_export(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Return (messages, load_mode). Never raises for a malformed export."""
    text = read_text(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"], "strict"

    repaired = TRAILING_COMMA.sub(r"\1", text)
    if repaired != text:
        try:
            candidate = json.loads(repaired)
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict) and isinstance(candidate.get("messages"), list):
            return candidate["messages"], "comma-repair"

    return salvage_messages(text), "salvage"


def sender_uin(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    if not isinstance(sender, dict):
        return ""
    for key in ("uin", "userId", "id"):
        value = sender.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def extract_text(content: Any) -> tuple[str, bool]:
    """Join the message's own text segments; report whether media was attached."""
    if not isinstance(content, dict):
        return "", False

    pieces: list[str] = []
    has_media = False
    elements = content.get("elements")
    if isinstance(elements, list) and elements:
        for element in elements:
            if not isinstance(element, dict):
                continue
            kind = str(element.get("type") or "")
            if kind == "text":
                data = element.get("data")
                if isinstance(data, dict) and isinstance(data.get("text"), str):
                    pieces.append(data["text"])
            elif kind in QUOTE_TYPES:
                continue
            else:
                has_media = True
    else:
        raw = content.get("text")
        if isinstance(raw, str):
            pieces.append(REPLY_PREFIX.sub("", raw))

    text = "".join(pieces)
    text = re.sub(r"[ \t\u3000]+", " ", text).strip()
    return text, has_media


def mask_sensitive(text: str) -> tuple[str, int]:
    """Mask phone numbers, ID cards and long digit runs in place."""
    hits = 0

    def _mask(pattern: re.Pattern[str], label: str, value: str) -> str:
        nonlocal hits
        result, count = pattern.subn(label, value)
        hits += count
        return result

    text = _mask(PHONE, "[手机号]", text)
    text = _mask(ID_CARD, "[身份证]", text)
    text = _mask(LONG_DIGITS, "[长数字]", text)
    return text, hits


def chat_label(filename: str) -> tuple[str, str]:
    """Derive (scene, chat_name) from an export filename."""
    stem = filename[:-5] if filename.endswith(".json") else filename
    scene = "friend" if stem.startswith("friend_") else "group"
    body = stem.split("_", 1)[1] if "_" in stem else stem
    # Drop the trailing _<chatid>_<yyyymmdd>_<suffix> noise.
    body = re.sub(r"_\d{6,}_\d{8}(_\d+)?$", "", body)
    return scene, body.replace("_", " ").strip() or scene


def iter_records(
    directory: Path,
    targets: dict[str, str],
    do_mask: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    files = sorted(p for p in directory.glob("*.json") if p.is_file())
    seen_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "files": [],
        "per_uin": Counter(),
        "per_scene": Counter(),
        "skipped_non_target": 0,
        "skipped_recalled_system": 0,
        "skipped_no_text": 0,
        "duplicates": 0,
        "mixed_with_media": 0,
        "masked": 0,
    }

    for path in files:
        try:
            messages, mode = load_export(path)
        except OSError as exc:
            stats["files"].append({"file": path.name, "mode": "error", "error": str(exc)})
            continue
        scene, chat = chat_label(path.name)
        kept = 0
        duplicates = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            uin = sender_uin(message)
            if uin not in targets:
                stats["skipped_non_target"] += 1
                continue
            if message.get("recalled") or message.get("system"):
                stats["skipped_recalled_system"] += 1
                continue
            text, has_media = extract_text(message.get("content"))
            if not text:
                stats["skipped_no_text"] += 1
                continue
            if do_mask:
                text, hits = mask_sensitive(text)
                stats["masked"] += hits
            if not text.strip():
                stats["skipped_no_text"] += 1
                continue

            message_id = str(message.get("id") or "").strip()
            dedup_key = message_id or f"{message.get('time')}|{uin}|{text}"
            if dedup_key in seen_ids:
                stats["duplicates"] += 1
                duplicates += 1
                continue
            seen_ids.add(dedup_key)

            if has_media:
                stats["mixed_with_media"] += 1
            stats["per_uin"][uin] += 1
            stats["per_scene"][f"{scene}/{chat}"] += 1
            kept += 1
            records.append(
                {
                    "time": message.get("time") or message.get("timestamp"),
                    "qq_id": uin,
                    "nickname": targets[uin],
                    "text": text,
                    "scene": scene,
                    "chat": chat,
                    "has_media": has_media,
                    "source": path.name,
                }
            )
        stats["files"].append(
            {
                "file": path.name,
                "mode": mode,
                "scene": scene,
                "chat": chat,
                "messages_in_file": len(messages),
                "kept": kept,
                "duplicates_dropped": duplicates,
            }
        )

    records.sort(key=lambda item: str(item.get("time") or ""))
    return records, stats


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary.replace(path)


def preview_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    lengths = [len(str(r["text"])) for r in records]
    lengths.sort()
    total = len(lengths)

    def percentile(p: float) -> int:
        if not total:
            return 0
        return lengths[min(total - 1, int(total * p))]

    endings = Counter()
    for record in records:
        text = str(record["text"])
        if text and text[-1] in "。.．":
            endings["句号结尾"] += 1
        elif text and text[-1] in "？?":
            endings["问号结尾"] += 1
        else:
            endings["无句末标点"] += 1

    return {
        "count": total,
        "len_p50": percentile(0.5),
        "len_p90": percentile(0.9),
        "len_max": lengths[-1] if lengths else 0,
        "under_10_chars": sum(1 for n in lengths if n <= 10),
        "endings": dict(endings),
        "with_parentheses": sum(1 for r in records if "(" in str(r["text"]) or "（" in str(r["text"])),
    }


def write_report(path: Path, records: list[dict[str, Any]], stats: dict[str, Any]) -> None:
    names = {str(r["qq_id"]): str(r["nickname"]) for r in records}
    lines: list[str] = ["# 聊天记录清洗报告", ""]
    lines.append(f"- 导出目录：`{DEFAULT_INPUT_DIR}`")
    lines.append(f"- 原始文件数：{len(stats['files'])}")
    lines.append(f"- 保留发言总数：**{len(records)}** 条（去重后）")
    lines.append("")

    lines.append("## 各文件处理情况")
    lines.append("")
    lines.append("| 文件 | 读取方式 | 场景 | 文件内消息 | 保留 | 去重丢弃 |")
    lines.append("|---|---|---|---|---|---|")
    for item in stats["files"]:
        if item.get("mode") == "error":
            lines.append(f"| `{item['file']}` | 读取失败 | - | - | - | - |")
            continue
        lines.append(
            f"| `{item['file']}` | {item['mode']} | {item['scene']}/{item['chat']} | "
            f"{item['messages_in_file']} | {item['kept']} | {item['duplicates_dropped']} |"
        )
    lines.append("")

    lines.append("## 按账号")
    lines.append("")
    lines.append("| QQ 号 | 昵称 | 条数 |")
    lines.append("|---|---|---|")
    for uin, count in stats["per_uin"].most_common():
        lines.append(f"| {uin} | {names.get(uin, "")} | {count} |")
    lines.append("")

    lines.append("## 按会话")
    lines.append("")
    lines.append("| 场景 | 条数 |")
    lines.append("|---|---|")
    for name, count in stats["per_scene"].most_common():
        lines.append(f"| {name} | {count} |")
    lines.append("")

    lines.append("## 过滤与脱敏")
    lines.append("")
    lines.append(f"- 非目标账号：跳过 {stats['skipped_non_target']} 条")
    lines.append(f"- 撤回/系统消息：跳过 {stats['skipped_recalled_system']} 条")
    lines.append(f"- 无可读文本：跳过 {stats['skipped_no_text']} 条")
    lines.append(f"- 跨文件重复（同 id）：丢弃 {stats['duplicates']} 条")
    lines.append(f"- 含图片/表情等非文本段但保留正文：{stats['mixed_with_media']} 条")
    lines.append(f"- 脱敏替换次数：{stats['masked']}")
    lines.append("")

    lines.append("## 风格分布（供写 style.txt 参考）")
    lines.append("")
    for uin in sorted({r["qq_id"] for r in records}):
        subset = [r for r in records if r["qq_id"] == uin]
        preview = preview_stats(subset)
        lines.append(f"### {names.get(uin, uin)}（{uin}）")
        lines.append("")
        lines.append(f"- 条数：{preview['count']}")
        lines.append(
            f"- 长度：中位数 {preview['len_p50']} 字，P90 {preview['len_p90']} 字，最长 {preview['len_max']} 字"
        )
        lines.append(f"- 10 字以内：{preview['under_10_chars']} 条")
        lines.append(f"- 含括号：{preview['with_parentheses']} 条")
        share = preview["endings"]
        total = max(1, preview["count"])
        rendered = "、".join(
            f"{key} {value}（{value * 100 // total}%）" for key, value in share.items()
        )
        lines.append(f"- 句末：{rendered}")
        lines.append("")

    lines.append("## 输出文件")
    lines.append("")
    for uin in sorted({r["qq_id"] for r in records}):
        lines.append(f"- `style_corpus_{uin}.jsonl`")
    lines.append("- `style_corpus_merged.jsonl`（所有目标账号合并）")
    lines.append("")
    lines.append("字段：`time` / `qq_id` / `nickname` / `text` / `scene` / `chat` / `has_media` / `source`")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量清洗 QQChatExporter 导出，生成风格语料。")
    parser.add_argument("input_dir", nargs="?", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        metavar="QQ=昵称",
        help="目标账号，可重复；格式 QQ=昵称（必填）",
    )
    parser.add_argument("--no-mask", action="store_true", help="关闭手机号/证件号脱敏")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    targets = dict(DEFAULT_TARGETS)
    if args.target:
        targets = {}
        for entry in args.target:
            uin, _, nickname = entry.partition("=")
            targets[uin.strip()] = nickname.strip() or uin.strip()
    if not targets:
        print("请用 --target QQ=昵称 指定至少一个目标账号。", file=sys.stderr)
        return 1
    if not args.input_dir.is_dir():
        print(f"找不到导出目录：{args.input_dir}", file=sys.stderr)
        return 1

    records, stats = iter_records(args.input_dir, targets, not args.no_mask)
    if not records:
        print("没有匹配到任何目标账号发言，未生成输出。", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for uin, nickname in targets.items():
        subset = [r for r in records if r["qq_id"] == uin]
        if subset:
            write_jsonl(args.output_dir / f"style_corpus_{uin}.jsonl", subset)
    write_jsonl(args.output_dir / "style_corpus_merged.jsonl", records)
    write_report(args.output_dir / "CLEANING_REPORT.md", records, stats)

    print(f"输入目录：{args.input_dir}")
    print(f"保留发言：{len(records)} 条（去重后）")
    for uin, count in stats["per_uin"].most_common():
        print(f"  {uin} {targets.get(uin, '')}: {count}")
    print(f"跨文件重复丢弃：{stats['duplicates']}")
    print(f"脱敏替换：{stats['masked']} 处")
    print(f"输出目录：{args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Keep only one user's plain-text messages from a QQChatExporter JSON export."""

import argparse
import json
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = PROJECT_DIR / "dataset" / "import" / "export.json"
DEFAULT_OUTPUT = PROJECT_DIR / "dataset" / "cleaned" / "style_corpus.jsonl"
TARGET_USER_ID = ""
TARGET_USER_NAME = "TargetUser"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="筛选指定 QQ 号发送的纯文本消息，输出本地 JSONL。"
    )
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT, help="QQChatExporter 导出的 JSON 文件")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出 JSONL 路径")
    parser.add_argument("--user-id", default=TARGET_USER_ID, help="只保留该 QQ 号的发言")
    parser.add_argument("--user-name", default=TARGET_USER_NAME, help="输出 JSONL 中使用的昵称")
    return parser.parse_args()


def sender_id(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    if not isinstance(sender, dict):
        return ""
    for key in ("uin", "userId", "id"):
        value = sender.get(key)
        if value is not None:
            return str(value)
    return ""


def is_plain_text_message(message: dict[str, Any]) -> bool:
    if message.get("type") != "text" or message.get("recalled") or message.get("system"):
        return False

    content = message.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("text"), str):
        return False

    # Exclude a message that embeds an image, sticker, video, reply, or other segment,
    # even if QQChatExporter labels its outer message type as "text".
    elements = content.get("elements")
    if isinstance(elements, list):
        for element in elements:
            if isinstance(element, dict) and element.get("type") != "text":
                return False
    return bool(content["text"].strip())


def clean_export(input_path: Path, output_path: Path, user_id: str, user_name: str) -> dict[str, int]:
    if not input_path.is_file():
        raise FileNotFoundError(f"找不到输入文件：{input_path}")
    if input_path.resolve() == output_path.resolve():
        raise ValueError("输出路径不能与原始聊天记录相同。")

    data = json.loads(input_path.read_text(encoding="utf-8-sig"))
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        raise ValueError("JSON 结构中没有 messages 数组。")

    counts = {
        "total": len(messages),
        "from_target": 0,
        "kept": 0,
        "skipped_non_text_or_mixed": 0,
        "skipped_empty": 0,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output:
            for message in messages:
                if not isinstance(message, dict) or sender_id(message) != str(user_id):
                    continue
                counts["from_target"] += 1
                if not is_plain_text_message(message):
                    counts["skipped_non_text_or_mixed"] += 1
                    continue

                content = message["content"]["text"].strip()
                if not content:
                    counts["skipped_empty"] += 1
                    continue
                record = {
                    "time": message.get("time") or message.get("timestamp"),
                    "user": user_name,
                    "text": content,
                }
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                counts["kept"] += 1
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return counts


def main() -> None:
    args = parse_args()
    if not args.user_id:
        raise SystemExit("请用 --user-id 指定要筛选的 QQ 号（例如 --user-id 1000000000）。")
    counts = clean_export(args.input, args.output, args.user_id, args.user_name)
    print(f"清理完成：{args.output}")
    print(
        "总消息 {total} 条；目标用户 {from_target} 条；保留纯文本 {kept} 条；"
        "跳过非文本/混合消息 {skipped_non_text_or_mixed} 条。".format(**counts)
    )


if __name__ == "__main__":
    main()

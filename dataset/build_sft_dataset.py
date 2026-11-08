"""Build a supervised fine-tuning set from chunked group exports.

The 547 reply pairs are too few and too clean to train on - they become the
held-out eval set. Training examples are rebuilt from the raw timeline:

    several other people speaking  ->  the owner's next burst of messages

Bursts matter: she habitually fires 2-4 short lines in a row, and the deployed
bot splits replies the same way, so a burst is one assistant turn.

Output follows the OpenAI chat SFT shape (`messages` array), which Baidu/Qwen/
DeepSeek-style trainers all accept.
"""

import argparse
import json
import os
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

from clean_chunked_corpus import (
    BOT_UIN,
    COMMAND,
    TARGETS,
    MASK_TOKEN,
    PLACEHOLDER_TOKEN,
    SENSITIVE,
    clean_text,
    iter_messages,
    load_manifest,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
BOT_NAME = os.getenv("CYBERYINYU_BOT_NAME", "CyberYinYu").strip() or "CyberYinYu"

# Kept short on purpose: a 4k-character system prompt swallows the signal and
# costs tokens on every training example.
SYSTEM_PROMPT = (
    f"你是「{BOT_NAME}」，一个在 QQ 群里聊天的虚拟角色。\n"
    "说话极短：多用 1-8 字，偶尔十几字，几乎不用句号。想问就问一个「？」或「啥」。\n"
    "评价直接抛结论，不解释、不分析；赞同就说「对」「嗯」「是」。\n"
    "不说「理解你的感受」「你辛苦了」「加油」这类安慰话，情绪上来就说「唉」「难绷」，或者只发个表情。\n"
    "不用书面语，不用敬语，不主动声明自己是 AI。"
)

BURST_GAP_MS = 30_000       # messages closer than this are one burst
CONTEXT_GAP_MS = 20_000     # only replies this fast count as "answering" it
MAX_CONTEXT_LINES = 1       # production replies to one @message, so train on one

STICKER_FILE = PROJECT_DIR / "personality" / "stickers.json"


def configured_stickers() -> set[str]:
    """Aliases the sender can actually render; others get silently dropped."""
    try:
        data = json.loads(STICKER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return set(data) if isinstance(data, dict) else set()


CONFIGURED = configured_stickers()
FACE_ALIAS = re.compile(r"\[\[([^\[\]]{1,16})\]\]")


def load_timeline(directory: Path) -> tuple[str, list[dict[str, Any]]]:
    info = load_manifest(directory).get("chatInfo") or {}
    chat = str(info.get("name") or directory.name)
    rows: list[dict[str, Any]] = []
    for message in iter_messages(directory):
        sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), dict) else {}
        rows.append({
            "id": str(message.get("id") or ""),
            "t": int(message.get("timestamp") or 0),
            "uin": str(sender.get("uin") or "").strip(),
            "name": str(sender.get("name") or "").strip(),
            "type": str(message.get("type") or ""),
            "raw": content.get("text") if isinstance(content.get("text"), str) else "",
            "system": bool(message.get("system")) or message.get("type") == "system",
            "recalled": bool(message.get("recalled")),
        })
    rows.sort(key=lambda r: r["t"])
    return chat, rows


def usable_text(raw: str, counters: Counter, *, limit: int) -> str:
    """Cleaned text, or '' when it carries nothing worth training on."""
    if not raw:
        return ""
    text = clean_text(raw, counters)
    # Unconfigured [[aliases]] are swallowed at send time, so training on them
    # would teach the model to emit tokens that vanish.
    text = FACE_ALIAS.sub(lambda m: m.group(0) if m.group(1).strip() in CONFIGURED else "", text)
    # Bot commands (#model / #记忆) sit in the same burst as real speech; drop the
    # line, keep the rest.
    lines = [ln for ln in text.split("\n") if not COMMAND.match(ln.strip())]
    text = "\n".join(lines)
    text = re.sub(r"[ \t\u3000]{2,}", " ", text).strip()
    bare = MASK_TOKEN.sub("", PLACEHOLDER_TOKEN.sub("", text)).strip()
    if not bare or not re.search(r"[\w\u4e00-\u9fff]", bare):
        return ""
    if SENSITIVE.search(raw) and re.search(r"\d{4,}", raw):
        return ""
    if len(text) > limit:
        return ""
    return text


def build(chat: str, rows: list[dict[str, Any]], eval_ids: set[str],
          counters: Counter) -> tuple[list[dict], list[dict]]:
    burst_examples: list[dict] = []
    eval_examples: list[dict] = []
    n = len(rows)
    i = 0
    while i < n:
        row = rows[i]
        if row["uin"] not in TARGETS or row["system"] or row["recalled"]:
            i += 1
            continue

        # --- one burst of the owner's own messages
        burst = [row]
        j = i + 1
        while (j < n and rows[j]["uin"] in TARGETS and not rows[j]["system"]
               and not rows[j]["recalled"]
               and rows[j]["t"] - burst[-1]["t"] <= BURST_GAP_MS):
            burst.append(rows[j])
            j += 1

        burst_lines = [t for t in (usable_text(b["raw"], counters, limit=120) for b in burst) if t]
        burst_ids = [b["id"] for b in burst]

        # --- the other people who spoke right before it
        context: list[str] = []
        k = i - 1
        while k >= 0 and len(context) < MAX_CONTEXT_LINES:
            prev = rows[k]
            if burst[0]["t"] - prev["t"] > CONTEXT_GAP_MS:
                break
            if (prev["uin"] and prev["uin"] not in TARGETS and prev["uin"] != BOT_UIN
                    and not prev["system"] and not prev["recalled"]):
                text = usable_text(prev["raw"], counters, limit=200)
                if text:
                    context.append(text)
            k -= 1
        context.reverse()

        if burst_lines and context:
            reply = "\n".join(burst_lines)
            prompt = "\n".join(context)
            if 2 <= len(reply) <= 120 and 2 <= len(prompt) <= 300:
                example = {
                    "chat": chat,
                    "time": burst[0]["t"],
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": reply},
                    ],
                }
                # Reply pairs are the cleanest signal: hold them out entirely.
                if any(bid in eval_ids for bid in burst_ids):
                    eval_examples.append(example)
                else:
                    burst_examples.append(example)
            else:
                counters["长度不符"] += 1
        else:
            counters["无可用上下文" if not context else "回复为空"] += 1

        i = j
    return burst_examples, eval_examples


def dedup(rows: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for row in rows:
        msgs = row["messages"]
        key = (msgs[1]["content"], msgs[2]["content"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="从 chunked 导出构造 SFT 数据集。")
    parser.add_argument("dirs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--eval-pairs", type=Path, default=None,
                        help="高质量 reply 配对，作为测试集且不参与训练；不传就跳过")
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=27)
    args = parser.parse_args()
    if not TARGETS or not BOT_UIN:
        parser.error(
            "请先用环境变量指定待清洗账号：CYBERYINYU_TARGETS=\"QQ=名字,...\" "
            "与 CYBERYINYU_BOT_QQ=\"机器人QQ\"（见文件头说明）"
        )

    eval_ids: set[str] = set()
    reply_eval: list[dict] = []
    if args.eval_pairs and args.eval_pairs.is_file():
        for line in args.eval_pairs.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            eval_ids.add(str(row.get("reply_id") or ""))
            reply_eval.append({
                "chat": row.get("chat"),
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": row["prompt"]},
                    {"role": "assistant", "content": row["reply"]},
                ],
            })

    counters: Counter = Counter()
    train: list[dict] = []
    val: list[dict] = []
    for directory in args.dirs:
        chat, rows = load_timeline(directory)
        burst_examples, eval_from_burst = build(chat, rows, eval_ids, counters)
        train.extend(burst_examples)
        val.extend(eval_from_burst)

    train = dedup(train)
    val = dedup(val)
    random.Random(args.seed).shuffle(train)
    random.Random(args.seed).shuffle(val)

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    def dump(path: Path, rows: list[dict]) -> int:
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps({"messages": row["messages"]}, ensure_ascii=False) + "\n")
        return len(rows)

    n_train = dump(out / "train.jsonl", train)
    n_val = dump(out / "val.jsonl", val)
    n_eval = dump(out / "eval_reply_pairs.jsonl", reply_eval)

    reply_len = [len(r["messages"][2]["content"]) for r in train]
    prompt_len = [len(r["messages"][1]["content"]) for r in train]
    stats = {
        "train": n_train,
        "val": n_val,
        "eval_reply_pairs": n_eval,
        "reply_len_p50": sorted(reply_len)[len(reply_len) // 2] if reply_len else 0,
        "reply_len_p90": sorted(reply_len)[int(len(reply_len) * 0.9)] if reply_len else 0,
        "prompt_len_p50": sorted(prompt_len)[len(prompt_len) // 2] if prompt_len else 0,
        "system_chars": len(SYSTEM_PROMPT),
        "tokens_est": sum(len(r["messages"][1]["content"]) + len(r["messages"][2]["content"]) for r in train),
    }

    lines = ["# SFT 数据集", ""]
    lines.append(f"- 训练 {n_train} / 验证 {n_val} / 测试（reply 配对）{n_eval}")
    lines.append(f"- system 长度 {stats['system_chars']} 字；回复中位 {stats['reply_len_p50']} 字、P90 {stats['reply_len_p90']} 字")
    lines.append(f"- 前文中位 {stats['prompt_len_p50']} 字")
    lines.append(f"- 粗略 token 量：约 {stats['tokens_est'] // 1000}k（不含 system 重复计入）")
    lines.append("")
    lines.append("## 格式")
    lines.append("")
    lines.append("每行一条 OpenAI chat SFT 样本：")
    lines.append("")
    lines.append("```json")
    lines.append('{"messages":[{"role":"system","content":"..."},{"role":"user","content":"群友的话"},{"role":"assistant","content":"她的回复"}]}')
    lines.append("```")
    lines.append("")
    lines.append("assistant 里的换行代表她连发多条，部署时对应 `[[QQ_MSG]]` 拆条。")
    lines.append("")
    lines.append("## 划分逻辑")
    lines.append("")
    lines.append("- 训练集：时间序列重建（前 1-3 条他人消息 → 她的下一段连发），覆盖面广但噪声略高")
    lines.append("- 测试集：`dialogues.jsonl` 里的 reply 配对，有明确引用关系，质量最高；**同一 burst 已被整段排除出训练**")
    lines.append("")
    lines.append("## 待办")
    lines.append("")
    lines.append("- DeepSeek 官方与 Kimi 当前都没有公开微调入口，训练要么换百炼 Qwen，要么自建 LoRA")
    lines.append("- 上线前要拿 `eval_reply_pairs.jsonl` 做 A/B：微调前后各跑一遍，人工看像不像")
    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"train {n_train} / val {n_val} / eval {n_eval}")
    print("丢弃明细:", dict(counters.most_common(6)))
    print(f"输出：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

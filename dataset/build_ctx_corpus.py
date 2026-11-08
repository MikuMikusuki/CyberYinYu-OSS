"""Build a context-aware corpus from QQChatExporter chunked exports.

与 `clean_chunked_corpus.py` 的分工：
  * 那个脚本只留「本人发言」，他人发言统计完就丢，用来做 style 语料；
  * 本脚本**保留所有 speaker**，因为要训练的是「看懂群聊上下文再回」，
    他人那 7.5 万条正是上下文素材。

产出（默认 dataset/cleaned/群聊全量_20261029/）：
  all_messages.jsonl  全量消息，脱敏后，按群+时间排序
  ctx_pairs.jsonl     训练样本三元组：ctx[] -> trigger -> reply
  REPORT.md           统计

复用前一个脚本已验证的脱敏与分类规则，不另写一套。
"""

import argparse
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clean_chunked_corpus import (  # noqa: E402
    BOT_UIN,
    TARGETS,
    classify,
    clean_text,
    iter_messages,
    load_manifest,
    write_jsonl,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_DIR / "dataset" / "cleaned" / "群聊全量_20261029"
# 机器人自己的显示名，只用于把 bot 行的 name 标出来；按需改环境变量即可。
BOT_NAME = os.getenv("CYBERYINYU_BOT_NAME", "CyberYinYu").strip() or "CyberYinYu"

# 上下文窗口与话题断点：超过这个静默时长就认为换话题了，前文不再带。
CTX_MAX_MESSAGES = 10
TOPIC_GAP = timedelta(minutes=30)


def parse_time(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def speaker_of(message: dict[str, Any]) -> tuple[str, str]:
    sender = message.get("sender") or {}
    uin = str(sender.get("uin") or "")
    name = str(sender.get("nickname") or sender.get("name") or uin or "?")
    return uin, name[:24]


def collect(directory: Path, redact: list[str], counters: Counter) -> list[dict[str, Any]]:
    """Normalize every message in one export into a flat, sorted list."""
    info = (load_manifest(directory).get("chatInfo") or {})
    chat = str(info.get("name") or directory.name)
    rows: list[dict[str, Any]] = []

    for raw in iter_messages(directory):
        if not isinstance(raw, dict):
            continue
        msg_type = str(raw.get("type") or "")
        raw_text = str((raw.get("content") or {}).get("text") or "")
        text = clean_text(raw_text, counters)
        for name in redact:
            if name and name in text:
                text = text.replace(name, "[人名]")
                counters["显式脱敏"] += 1
        kind = classify(text, raw_text, msg_type, raw, 400)
        uin, nick = speaker_of(raw)
        ts = parse_time(raw.get("time"))
        if ts is None:
            counters["无时间戳"] += 1
            continue

        if uin in TARGETS:
            role = "owner"
        elif uin == BOT_UIN:
            role = "bot"
        else:
            role = "other"

        rows.append(
            {
                "t": ts.isoformat(),
                "chat": chat,
                "uin": uin,
                "name": nick if role != "bot" else BOT_NAME,
                "role": role,
                "text": text,
                "kind": kind,
                "speaker_raw": raw_text[:400] if kind == "sensitive" else "",
                "mid": str(raw.get("id") or ""),
            }
        )
        counters[f"kind:{kind}"] += 1
        counters[f"role:{role}"] += 1

    rows.sort(key=lambda r: r["t"])
    return rows


MEDIA_ONLY = re.compile(
    r"^(?:\s*(?:\[[^\[\]]{1,8}\]|\[\[[^\[\]]{1,16}\]\]|@\S{1,12}\s*))+$"
)
TEXT_LIKE = re.compile(r"[\w\u4e00-\u9fff]")


def build_pairs(rows: list[dict[str, Any]], counters: Counter) -> Iterable[dict[str, Any]]:
    """每一条本人发言配一份前文，作为「看懂上下文再回」的训练样本。"""
    usable = [r for r in rows if r["kind"] in ("text", "face", "media", "emoji") and r["text"]]
    for i, row in enumerate(usable):
        if row["role"] != "owner":
            continue
        if not row["text"].strip():
            continue
        # 纯图片 / 纯表情 / 纯 @ 的回复没有学习价值，只是噪音。
        if MEDIA_ONLY.match(row["text"]) or not TEXT_LIKE.search(row["text"]):
            counters["样本_跳过_纯媒体回复"] += 1
            continue
        if len(row["text"]) > 60:
            counters["样本_跳过_超长回复"] += 1
            continue
        # 最后一道闸：clean_text 漏掉的长数字串一律不进训练集。
        if re.search(r"\d{11,}", row["text"]):
            counters["样本_跳过_残留长数字"] += 1
            continue

        ctx: list[dict[str, str]] = []
        prev_ts = parse_time(row["t"])
        for back in range(i - 1, -1, -1):
            cand = usable[back]
            ts = parse_time(cand["t"])
            if prev_ts and ts and (prev_ts - ts) > TOPIC_GAP:
                counters["上下文_因话题断开截断"] += 1
                break
            # 本人连续发言也算上下文（一个人连发几条是常态），只是不重复记 reply
            if cand["role"] == "owner" and not ctx:
                prev_ts = ts
                continue
            ctx.append({"name": cand["name"], "text": cand["text"][:120], "role": cand["role"]})
            prev_ts = ts
            if len(ctx) >= CTX_MAX_MESSAGES:
                break

        if not ctx:
            counters["样本_无上下文"] += 1
            continue
        if not any(TEXT_LIKE.search(c["text"]) for c in ctx):
            counters["样本_跳过_纯媒体上下文"] += 1
            continue
        if any(re.search(r"\d{11,}", c["text"]) for c in ctx):
            counters["样本_跳过_上下文残留长数字"] += 1
            continue
        ctx.reverse()
        counters["样本_含上下文"] += 1
        counters[f"样本_ctx{min(len(ctx), CTX_MAX_MESSAGES)}条"] += 1

        yield {
            "chat": row["chat"],
            "time": row["t"],
            "ctx": ctx,
            "reply": row["text"],
            "reply_len": len(row["text"]),
            "ctx_has_other": any(c["role"] == "other" for c in ctx),
        }


SFT_SYSTEM = (
    f"你是「{BOT_NAME}」，一个在 QQ 群里聊天的虚拟角色。\n"
    "下面是一段群聊记录，最后一条是需要你接的话。像群里的人一样自然接，"
    "不解释、不总结、不复述对方的话，也不声明自己是 AI。\n"
    "说话极短：多用 1-8 字，偶尔十几字，几乎不用句号。\n"
    "评价直接抛结论，不分析；赞同就说「对」「嗯」「是」。\n"
    "不说「理解你的感受」「你辛苦了」「加油」这类安慰话。\n"
    "不用书面语，不用敬语。"
)


def to_sft(pair: dict[str, Any]) -> dict[str, Any]:
    """一条样本转成 SFT 多轮格式：user=群聊流，assistant=本人当时那句。"""
    lines = [f"{c['name']}：{c['text']}" for c in pair["ctx"]]
    return {
        "messages": [
            {"role": "system", "content": SFT_SYSTEM},
            {"role": "user", "content": "\n".join(lines)},
            {"role": "assistant", "content": pair["reply"]},
        ],
        "meta": {"chat": pair["chat"], "time": pair["time"], "ctx_len": len(pair["ctx"])},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dirs", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--redact", default="", help="逗号分隔的真实姓名，替换为 [人名]")
    args = parser.parse_args()
    if not TARGETS or not BOT_UIN:
        parser.error(
            "请先用环境变量指定待清洗账号：CYBERYINYU_TARGETS=\"QQ=名字,...\" "
            "与 CYBERYINYU_BOT_QQ=\"机器人QQ\"（见文件头说明）"
        )

    redact = [x.strip() for x in args.redact.split(",") if x.strip()]
    counters: Counter = Counter()
    all_rows: list[dict[str, Any]] = []

    for directory in args.dirs:
        rows = collect(directory, redact, counters)
        print(f"  {directory.name}: {len(rows)} 条")
        all_rows.extend(rows)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    n_all = write_jsonl(args.output_dir / "all_messages.jsonl", all_rows)
    pairs = list(build_pairs(all_rows, counters))
    n_pairs = write_jsonl(args.output_dir / "ctx_pairs.jsonl", pairs)

    # SFT 格式：user 侧就是「群聊流」，模型只学「看到这些之后怎么接」。
    # 推理时必须注入同样形状的上下文，否则训练白做——这一点写在 REPORT 里。
    rng = random.Random(20261029)
    shuffled = pairs[:]
    rng.shuffle(shuffled)
    cut = int(len(shuffled) * 0.9)
    sft_train = [to_sft(p) for p in shuffled[:cut]]
    sft_eval = [to_sft(p) for p in shuffled[cut:]]
    n_train = write_jsonl(args.output_dir / "sft_train.jsonl", sft_train)
    n_eval = write_jsonl(args.output_dir / "sft_eval.jsonl", sft_eval)

    role_counts = {k.split(":", 1)[1]: v for k, v in counters.items() if k.startswith("role:")}
    kind_counts = {k.split(":", 1)[1]: v for k, v in counters.items() if k.startswith("kind:")}
    lens = sorted(p["reply_len"] for p in pairs)
    p50 = lens[len(lens) // 2] if lens else 0
    p90 = lens[int(len(lens) * 0.9)] if lens else 0
    with_other = sum(1 for p in pairs if p["ctx_has_other"])

    lines = [
        "# 群聊全量语料 + 上下文训练样本",
        "",
        f"- 输入：{len(args.dirs)} 个 chunked 目录",
        *[f"  - `{d.name}`" for d in args.dirs],
        f"- 全量消息：**{n_all}** → `all_messages.jsonl`",
        f"- 上下文样本：**{n_pairs}** → `ctx_pairs.jsonl`",
        f"- SFT 切分：**{n_train} / {n_eval}** → `sft_train.jsonl` / `sft_eval.jsonl`",
        "",
        "> 注意：SFT 的 user 侧是「群聊流」，推理时也必须注入同形状的上下文，",
        "> 否则模型学到的「看前文接话」在线上用不上。",
        "",
        "## 按角色",
        "",
        "| 角色 | 条数 |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(role_counts.items(), key=lambda x: -x[1])],
        "",
        "## 按类型",
        "",
        "| 类型 | 条数 |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(kind_counts.items(), key=lambda x: -x[1])[:14]],
        "",
        "## 训练样本",
        "",
        f"- 回复长度 p50={p50} p90={p90}",
        f"- 上下文里含他人发言：{with_other}/{n_pairs}"
        f"（{with_other / max(1, n_pairs) * 100:.1f}%）",
        f"- 无上下文被跳过：{counters['样本_无上下文']}",
        f"- 质量过滤跳过：纯媒体回复 {counters['样本_跳过_纯媒体回复']}、"
        f"超长回复 {counters['样本_跳过_超长回复']}、纯媒体上下文 {counters['样本_跳过_纯媒体上下文']}",
        f"- 因话题超时截断：{counters['上下文_因话题断开截断']}",
        "",
        "## 脱敏",
        "",
        f"- 显式姓名替换：{counters['显式脱敏']} 处",
        f"- 手机号/身份证/邮箱/长数字：{counters['手机号']}/{counters['身份证']}"
        f"/{counters['邮箱']}/{counters['长数字']}",
        f"- 疑似凭据整条丢弃：{kind_counts.get('sensitive', 0)} 条",
    ]
    (args.output_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print()
    print(f"全量 {n_all} 条 / 训练样本 {n_pairs} 条 -> {args.output_dir}")
    print(f"角色: {role_counts}")
    print(f"回复长度 p50={p50} p90={p90}；含他人上下文 {with_other}/{n_pairs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

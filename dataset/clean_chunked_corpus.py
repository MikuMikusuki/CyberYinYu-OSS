"""Clean chunked QQChatExporter exports into style corpora + reply-pair dialogues.

Handles the newer `*_chunked_jsonl/` layout (manifest.json + chunks/*.jsonl),
where every message from every speaker is present - unlike the older single
self-perspective dump that clean_monologue_export.py handles.

Two outputs matter:
  * the owner's own speech (accounts given via env, see TARGETS) -> a style corpus
  * reply pairs (quoted message -> the owner's answer) -> dialogue material

The bot's own account is excluded everywhere: learning from what it already
said would make it imitate itself.
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "dataset" / "cleaned"

# 待清洗的「本人」账号：QQ -> 显示名。本仓库**不附带任何真实账号**，必须自己传：
#   CYBERYINYU_TARGETS="1000000001=示例名,1000000002=第二个账号"
#   CYBERYINYU_BOT_QQ="1000000000"     # 机器人自己的账号，全流程排除
# 同一个人有多个号时全部列进 TARGETS 即可，语料会合并。
def _parse_targets(raw: str) -> dict[str, str]:
    targets: dict[str, str] = {}
    for entry in re.split(r"[,，;；\s]+", raw.strip()):
        if not entry:
            continue
        uin, _, name = entry.partition("=")
        uin = uin.strip()
        if uin.isdigit():
            targets[uin] = name.strip() or uin
    return targets


TARGETS = _parse_targets(os.getenv("CYBERYINYU_TARGETS", ""))
# The bot itself; never quote it back into the corpus.
BOT_UIN = os.getenv("CYBERYINYU_BOT_QQ", "").strip()

REPLY_PREFIX = re.compile(r"^\[回复消息\]")
MEDIA_PLACEHOLDER = re.compile(r"\[(图片|视频|文件|语音|动画表情|表情|音乐)(?::[^\]]*)?\]")
FORWARD_MARK = re.compile(r"\[(?:转发消息|聊天记录)[^\]]*\]")
MENTION_UIN = re.compile(r"\[([^\[\]()]{1,24})\((\d{5,12})\)\]")
MENTION = re.compile(r"@[^\s@\[\]]{1,24}")
FACE_ONLY = re.compile(r"^(?:\[\[[^\[\]]{1,16}\]\]\s*)+$")
FACE_TOKEN = re.compile(r"\[\[([^\[\]]{1,16})\]\]")
PLACEHOLDER_TOKEN = re.compile(r"\[(?:图片|视频|文件|语音|表情|音乐|链接)\]")
# Masked stand-ins carry nothing on their own: "[手机号]" alone is not speech.
MASK_TOKEN = re.compile(r"\[(?:手机号|身份证|数字|邮箱|人名|链接)\]")
FORWARD_HEAD = re.compile(r"^\s*\[(?:转发消息|聊天记录)")
COMMAND = re.compile(r"^[#＃/／][A-Za-z\u4e00-\u9fff]{1,12}(?=\s|$)")
BOT_LOG = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,.]\d{3}\s+(?:INFO|WARN|WARNING|ERROR|DEBUG|TRACE)\b",
    re.IGNORECASE,
)
TRACEBACK = re.compile(r"^\s*(?:Traceback \(most recent call last\)|\s*File \"[^\"]+\", line \d+)")

URL = re.compile(r"(?:https?://|www\.)[^\s\u3000]+")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
QQ_LIKE = re.compile(r"(?<![\d.])\d{7,11}(?![\d.])")
SENSITIVE = re.compile(
    r"(短信密码|验证码|校验码|动态码|动态密码|支付密码|交易密码"
    r"|银行卡|卡号|尾号\d{4}|身份证号|护照号|开房记录)"
)

ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
SPACES = re.compile(r"[ \t\u3000]+")
AI_MARKERS = re.compile(
    r"(\*\*[^*\n]{1,30}\*\*|^#{1,4}\s|^\s*[-*]\s{1,2}\S|^\s*\d+[.、]\s{1,2}\S"
    r"|首先|其次|综上|总的来说|值得注意的是|一方面|另一方面)",
    re.MULTILINE,
)
# The exporter writes these when the quoted message could not be resolved.
PLACEHOLDER_TEXT = {"原消息", "消息", "原文", "[原消息]"}

LEN_BUCKETS = [(1, 3), (4, 6), (7, 10), (11, 20), (21, 30), (31, 10**9)]
BUCKET_NAMES = ["1-3 字", "4-6 字", "7-10 字", "11-20 字", "21-30 字", "31 字以上"]


# ------------------------------------------------------------------ loading
def load_manifest(directory: Path) -> dict[str, Any]:
    path = directory / "manifest.json"
    if not path.is_file():
        raise SystemExit(f"缺少 manifest.json：{directory}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def iter_messages(directory: Path) -> Iterable[dict[str, Any]]:
    files = sorted((directory / "chunks").glob("*.jsonl"))
    if not files:
        raise SystemExit(f"chunks 目录为空：{directory}")
    for path in files:
        with path.open(encoding="utf-8-sig", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    yield message


def baseline_keys(paths: Iterable[Path]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for path in paths:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pairs.add((str(row.get("time") or ""), str(row.get("text") or "")))
    return pairs


# ------------------------------------------------------------------ cleaning
def clean_text(raw: str, counters: Counter) -> str:
    text = REPLY_PREFIX.sub("", raw)
    text = ZERO_WIDTH.sub("", text)
    text = MEDIA_PLACEHOLDER.sub(
        lambda m: f"[{m.group(1) if m.group(1) != '动画表情' else '表情'}]", text
    )

    text, n = MENTION_UIN.subn(lambda m: f"@{m.group(1)}", text)
    counters["提及_带号码"] += n
    mentions = MENTION.findall(text)
    if mentions:
        counters["提及_已剥离"] += len(mentions)
        text = MENTION.sub("", text)

    urls = URL.findall(text)
    if urls:
        counters["链接"] += len(urls)
        text = URL.sub("[链接]", text)

    for pattern, label, key in (
        (EMAIL, "[邮箱]", "邮箱"),
        (PHONE, "[手机号]", "手机号"),
        (ID_CARD, "[身份证]", "身份证"),
        (QQ_LIKE, "[数字]", "长数字"),
    ):
        text, n = pattern.subn(label, text)
        if n:
            counters[key] += n

    text = SPACES.sub(" ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip(" \n")


def classify(text: str, raw: str, msg_type: str, message: dict[str, Any], max_chars: int) -> str:
    if message.get("system") or msg_type == "system":
        return "system"
    if message.get("recalled"):
        return "recalled"
    if msg_type == "forward":
        return "forward"
    if msg_type in ("json", "ark", "music", "markdown", "type_19", "type_10"):
        return "card"
    if msg_type in ("video", "file", "audio", "ptt"):
        return "attachment"
    body = text.strip()
    if not body:
        return "empty"
    # Must look at the raw text: masking runs first and would hide the digits.
    if SENSITIVE.search(raw) and re.search(r"\d{4,}", raw):
        return "sensitive"
    if COMMAND.match(body):
        return "command"
    if BOT_LOG.match(body) or TRACEBACK.match(body):
        return "log"
    if FACE_ONLY.match(body):
        return "face"
    residue = MASK_TOKEN.sub("", PLACEHOLDER_TOKEN.sub("", body)).strip()
    if not residue:
        return "media" if PLACEHOLDER_TOKEN.search(body) else "redacted-only"
    if not re.search(r"[\w\u4e00-\u9fff]", residue):
        return "emoji" if any(unicodedata.category(c) in ("So", "Sk", "Mn") for c in residue) else "noise"
    if len(text) > max_chars:
        return "long"
    if FORWARD_HEAD.match(text):
        return "forward-preview"
    if AI_MARKERS.search(text) and (text.count("\n") >= 2 or len(text) > 80):
        return "ai-paste"
    return "text"


def quoted_of(message: dict[str, Any]) -> dict[str, str] | None:
    """Pull the message this one replies to (speaker + text), if present."""
    content = message.get("content")
    if not isinstance(content, dict):
        return None
    for element in content.get("elements") or []:
        if isinstance(element, dict) and element.get("type") == "reply":
            data = element.get("data") or {}
            return {
                "uin": str(data.get("senderUin") or ""),
                "name": str(data.get("senderName") or ""),
                "content": str(data.get("content") or ""),
                "id": str(data.get("referencedMessageId") or ""),
            }
    return None


# ------------------------------------------------------------------ reporting
def length_profile(texts: list[str]) -> dict[str, Any]:
    lengths = sorted(len(t) for t in texts)
    total = len(lengths)
    if not total:
        return {"count": 0}
    buckets = Counter()
    for n in lengths:
        for (low, high), name in zip(LEN_BUCKETS, BUCKET_NAMES):
            if low <= n <= high:
                buckets[name] += 1
                break
    endings = Counter()
    for t in texts:
        if t[-1] in "。.．":
            endings["句号"] += 1
        elif t[-1] in "？?":
            endings["问号"] += 1
        elif t[-1] in "！!":
            endings["感叹号"] += 1
        else:
            endings["无句末标点"] += 1
    return {
        "count": total,
        "p50": lengths[total // 2],
        "p90": lengths[min(total - 1, int(total * 0.9))],
        "max": lengths[-1],
        "mean": round(sum(lengths) / total, 1),
        "buckets": {name: buckets.get(name, 0) for name in BUCKET_NAMES},
        "endings": dict(endings),
        "paren": sum(1 for t in texts if "(" in t or "（" in t),
    }


def top_phrases(texts: list[str], limit: int = 30) -> list[tuple[str, int]]:
    whole = Counter(t for t in texts if 1 <= len(t) <= 8)
    return [(t, c) for t, c in whole.most_common(limit) if c >= 3]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


# ------------------------------------------------------------------ main
def main() -> int:
    parser = argparse.ArgumentParser(description="清洗 chunked 导出为风格语料与对话配对。")
    parser.add_argument("dirs", nargs="+", type=Path, help="*_chunked_jsonl 目录，可多个")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--baseline", action="append", default=[],
                        help="已有语料 jsonl，用于跨文件去重；可重复")
    parser.add_argument("--max-chars", type=int, default=150)
    parser.add_argument("--max-prompt-chars", type=int, default=200,
                        help="对话配对里的前文超过这个长度就丢弃")
    parser.add_argument("--redact", action="append", default=[], metavar="词语")
    args = parser.parse_args()
    if not TARGETS or not BOT_UIN:
        parser.error(
            "请先用环境变量指定待清洗账号：CYBERYINYU_TARGETS=\"QQ=名字,...\" "
            "与 CYBERYINYU_BOT_QQ=\"机器人QQ\"（见文件头说明）"
        )

    redact_words = [w.strip() for entry in args.redact for w in entry.split(",") if w.strip()]
    base = baseline_keys([Path(p) for p in args.baseline])

    counters: Counter = Counter()
    corpus: list[dict[str, Any]] = []
    faces: list[dict[str, Any]] = []
    long_rows: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    dialogues: list[dict[str, Any]] = []
    self_talk: list[dict[str, Any]] = []
    to_bot: list[dict[str, Any]] = []
    per_chat = Counter()
    per_uin = Counter()
    seen: set[tuple[str, str]] = set()

    for directory in args.dirs:
        manifest = load_manifest(directory)
        info = manifest.get("chatInfo") or {}
        chat = str(info.get("name") or directory.name)
        per_chat[chat] = 0
        for message in iter_messages(directory):
            msg_type = str(message.get("type") or "")
            sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
            uin = str(sender.get("uin") or "").strip()
            content = message.get("content") if isinstance(message.get("content"), dict) else {}
            raw = content.get("text") if isinstance(content.get("text"), str) else ""

            if uin == BOT_UIN:
                counters["bot_self"] += 1
                continue
            if uin not in TARGETS:
                counters["非目标账号"] += 1
                continue

            cleaned = clean_text(raw, counters)
            for word in redact_words:
                if word and word in cleaned:
                    counters["人名脱敏"] += cleaned.count(word)
                    cleaned = cleaned.replace(word, "[人名]")

            kind = classify(cleaned, raw, msg_type, message, args.max_chars)
            if kind not in ("text", "face", "emoji", "long"):
                counters[kind] += 1
                dropped.append({"chat": chat, "id": message.get("id"),
                                "reason": kind, "raw": raw[:80]})
                continue

            record = {
                "id": message.get("id"),
                "time": message.get("time") or "",
                "qq_id": uin,
                "nickname": TARGETS[uin],
                "text": cleaned,
                "kind": kind,
                "scene": "group",
                "chat": chat,
                "has_media": bool(re.search(r"\[(?:图片|视频|文件|表情)\]", cleaned)),
                "source": directory.name,
            }
            pair = (record["time"], cleaned)
            if pair in seen:
                counters["重复_本轮"] += 1
                continue
            seen.add(pair)
            record["in_baseline"] = pair in base
            if record["in_baseline"]:
                counters["重复_旧语料"] += 1

            if kind in ("face", "emoji"):
                record["faces"] = FACE_TOKEN.findall(cleaned)
                faces.append(record)
            elif kind == "long":
                record["chars"] = len(cleaned)
                record["ai_like"] = bool(AI_MARKERS.search(cleaned))
                long_rows.append(record)
            else:
                corpus.append(record)
            per_uin[uin] += 1
            per_chat[chat] += 1

            # Reply pair: what someone said -> what the owner answered.
            quote = quoted_of(message)
            if quote and kind in ("text", "emoji"):
                prompt = clean_text(quote["content"], counters)
                for word in redact_words:
                    prompt = prompt.replace(word, "[人名]")
                prompt = prompt.strip()
                bare = MASK_TOKEN.sub("", PLACEHOLDER_TOKEN.sub("", prompt)).strip()
                usable = (
                    bool(prompt)
                    and prompt not in PLACEHOLDER_TEXT
                    and len(prompt) <= args.max_prompt_chars
                    and not SENSITIVE.search(quote["content"])
                    and bool(re.search(r"[\w\u4e00-\u9fff]", bare))
                )
                if usable:
                    row = {
                        "time": record["time"],
                        "qq_id": uin,
                        "nickname": TARGETS[uin],
                        "chat": chat,
                        "prompt": prompt,
                        "prompt_from": quote["name"],
                        "prompt_uin": quote["uin"],
                        "reply": cleaned,
                        "reply_id": record["id"],
                        "source": directory.name,
                    }
                    if quote["uin"] == BOT_UIN:
                        to_bot.append(row)
                    elif quote["uin"] in TARGETS:
                        self_talk.append(row)
                    else:
                        dialogues.append(row)
                else:
                    counters["配对_丢弃"] += 1

    corpus.sort(key=lambda r: r["time"])
    faces.sort(key=lambda r: r["time"])
    long_rows.sort(key=lambda r: r["time"])
    dialogues.sort(key=lambda r: r["time"])
    self_talk.sort(key=lambda r: r["time"])
    to_bot.sort(key=lambda r: r["time"])

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    n_corpus = write_jsonl(out / "corpus.jsonl", corpus)
    n_new = write_jsonl(out / "corpus_new.jsonl", [r for r in corpus if not r["in_baseline"]])
    n_faces = write_jsonl(out / "faces.jsonl", faces)
    n_long = write_jsonl(out / "long.jsonl", long_rows)
    n_dialog = write_jsonl(out / "dialogues.jsonl", dialogues)
    n_self = write_jsonl(out / "dialogues_self.jsonl", self_talk)
    n_tobot = write_jsonl(out / "dialogues_to_bot.jsonl", to_bot)
    n_drop = write_jsonl(out / "dropped.jsonl", dropped)

    texts = [r["text"] for r in corpus]
    prof = length_profile(texts)
    face_counts = Counter(f for r in faces for f in r["faces"])
    per_name = {TARGETS[u]: c for u, c in per_uin.items()}

    lines = ["# 群聊语料清洗报告（chunked 导出）", ""]
    lines.append(f"- 输入：{len(args.dirs)} 个 chunked 目录")
    for d in args.dirs:
        lines.append(f"  - `{d.name}`")
    lines.append(f"- 目标账号：{'、'.join(f'{n}（{u}）' for u, n in TARGETS.items())}")
    lines.append(f"- 已排除自身 bot 账号 {BOT_UIN} 的发言：{counters['bot_self']} 条")
    lines.append("")
    lines.append("## 产出")
    lines.append("")
    lines.append("| 文件 | 条数 | 说明 |")
    lines.append("|---|---|---|")
    lines.append(f"| `corpus.jsonl` | {n_corpus} | 本人发言主语料；`in_baseline` 标出旧语料已收 |")
    lines.append(f"| `corpus_new.jsonl` | {n_new} | 净新增，合并用这个 |")
    lines.append(f"| `dialogues.jsonl` | {n_dialog} | 回复配对：别人说什么 → 本人怎么接 |")
    lines.append(f"| `dialogues_self.jsonl` | {n_self} | 两个号互相引用（自问自答），单独放，不进主配对 |")
    lines.append(f"| `dialogues_to_bot.jsonl` | {n_tobot} | 本人回复 bot 的片段，用于看「她怎么跟 bot 说话」 |")
    lines.append(f"| `faces.jsonl` | {n_faces} | 表情包/emoji 单发 |")
    lines.append(f"| `long.jsonl` | {n_long} | 超 {args.max_chars} 字长文，待人工复核 |")
    lines.append(f"| `dropped.jsonl` | {n_drop} | 丢弃明细 |")
    lines.append("")
    lines.append("## 按群 / 按账号")
    lines.append("")
    lines.append("| 群 | 保留条数 |")
    lines.append("|---|---|")
    for chat, count in per_chat.items():
        lines.append(f"| {chat} | {count} |")
    lines.append("")
    lines.append("| 账号 | 保留条数 |")
    lines.append("|---|---|")
    for name, count in per_name.items():
        lines.append(f"| {name} | {count} |")
    lines.append("")
    reason_label = {
        "system": "系统消息（撤回/入群等）",
        "recalled": "已撤回",
        "forward": "合并转发",
        "forward-preview": "以转发摘要开头",
        "card": "JSON/小程序卡片",
        "attachment": "视频/文件/语音",
        "empty": "空文本",
        "media": "纯图片/视频占位",
        "redacted-only": "脱敏后只剩占位符（如只发了一个手机号）",
        "noise": "纯符号",
        "command": "机器人指令（#开头）",
        "sensitive": "含验证码/银行卡等凭据",
        "log": "机器人日志/堆栈",
        "ai-paste": "疑似 AI 生成或粘贴",
        "非目标账号": "非本人（他人发言，仅用于配对前文）",
    }
    lines.append("## 过滤")
    lines.append("")
    for key in ("非目标账号", "bot_self", "system", "recalled", "forward", "forward-preview",
                "card", "attachment", "empty", "media", "redacted-only", "noise", "command",
                "sensitive", "log", "ai-paste", "重复_本轮"):
        if counters.get(key):
            lines.append(f"- {reason_label.get(key, key)}：{counters[key]} 条")
    lines.append("")
    lines.append("## 去重与脱敏")
    lines.append("")
    lines.append(f"- 与旧语料重叠（仍保留在 `corpus.jsonl`，只标记）：{counters.get('重复_旧语料', 0)} 条")
    lines.append(f"- 本轮内部重复（已丢）：{counters.get('重复_本轮', 0)} 条")
    for key, label in (("提及_已剥离", "@提及剥离"), ("提及_带号码", "带 QQ 号提及转 @昵称"),
                       ("链接", "链接→[链接]"), ("手机号", "手机号"), ("身份证", "身份证"),
                       ("邮箱", "邮箱"), ("长数字", "7-11 位数字（疑似 QQ 号）"),
                       ("人名脱敏", "指定词→[人名]")):
        if counters.get(key):
            lines.append(f"- {label}：{counters[key]} 处")
    if redact_words:
        lines.append(f"- 本次屏蔽词：{'、'.join(redact_words)}")
    if counters.get("sensitive"):
        lines.append("")
        lines.append(f"- ⚠️ **{counters['sensitive']} 条**含验证码/银行卡等信息，已整条丢弃，未写入任何输出")
    lines.append("")
    lines.append("## 长度分布（主语料）")
    lines.append("")
    lines.append(f"- 条数 {prof['count']}，均值 {prof.get('mean')} 字，中位数 {prof.get('p50')}，P90 {prof.get('p90')}，最长 {prof.get('max')}")
    lines.append("")
    lines.append("| 区间 | 条数 | 占比 |")
    lines.append("|---|---|---|")
    for name in BUCKET_NAMES:
        count = prof.get("buckets", {}).get(name, 0)
        lines.append(f"| {name} | {count} | {count * 100 / max(1, prof['count']):.0f}% |")
    lines.append("")
    endings = prof.get("endings", {})
    lines.append("- 句末：" + "、".join(f"{k} {v}" for k, v in endings.items()))
    lines.append(f"- 含括号：{prof.get('paren', 0)} 条")
    lines.append("")
    lines.append("## 高频整句")
    lines.append("")
    lines.append("、".join(f"「{t}」×{c}" for t, c in top_phrases(texts, 40)) or "（无）")
    lines.append("")
    lines.append("## 表情")
    lines.append("")
    lines.append(f"- 单发 {n_faces} 次，共 {len(face_counts)} 种")
    lines.append("- " + "、".join(f"`[[{n}]]`×{c}" for n, c in face_counts.most_common(15)))
    lines.append("")
    if dialogues:
        prompts = [len(d["prompt"]) for d in dialogues]
        lines.append("## 对话配对")
        lines.append("")
        lines.append(f"- 主配对 {n_dialog} 对，前文中位长度 {sorted(prompts)[len(prompts)//2]} 字")
        lines.append(f"- 来源分布：{'、'.join(f'{k} {v}' for k, v in Counter(d['chat'] for d in dialogues).most_common())}")
        lines.append("")
        lines.append("示例：")
        lines.append("")
        for d in dialogues[:3]:
            lines.append(f"- `{d['prompt_from']}`：{d['prompt'][:50]}  →  **{d['reply'][:50]}**")
        lines.append("")
    if self_talk:
        lines.append(f"两号互引 {n_self} 对，示例：")
        lines.append("")
        for d in self_talk[:2]:
            lines.append(f"- `{d['prompt_from']}`：{d['prompt'][:45]}  →  **{d['reply'][:45]}**")
        lines.append("")
    if to_bot:
        lines.append(f"回复 bot {n_tobot} 对，示例：")
        lines.append("")
        for d in to_bot[:2]:
            lines.append(f"- bot：{d['prompt'][:45]}  →  **{d['reply'][:45]}**")
        lines.append("")
    lines.append("## 字段")
    lines.append("")
    lines.append("corpus：`id/time/qq_id/nickname/text/kind/scene/chat/has_media/in_baseline/source`")
    lines.append("")
    lines.append("dialogues：`time/qq_id/nickname/chat/prompt/prompt_from/prompt_uin/reply/reply_id/source`")
    lines.append("")
    (out / "CLEANING_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"主语料 {n_corpus}（净新增 {n_new}）/ 配对 {n_dialog} / 表情 {n_faces} / 长文 {n_long} / 丢弃 {n_drop}")
    print(f"排除 bot 自身发言 {counters['bot_self']} 条")
    print(f"输出：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Turn a self-perspective QQChatExporter dump into a clean style corpus.

Unlike clean_all_exports.py (which keeps every target speaker in a folder),
this handles one "monologue" export: a chat exported from the owner's side,
where almost every message is the target account's own speech.

Pipeline per message:
  classify -> strip reply prefix -> replace media placeholders -> drop
  forwarded/quote payloads -> strip @mentions -> mask PII -> normalise
  -> route to corpus / faces / long / dropped

Nothing is written over an existing corpus: output goes to a fresh directory.
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "dataset" / "cleaned"
# 默认不加基线语料：本仓库不附带任何真实导出的产物，要对比就自己用 --baseline 传。
DEFAULT_BASELINE = None
STICKERS = PROJECT_DIR / "personality" / "stickers.json"

# ---------------------------------------------------------------- patterns
REPLY_PREFIX = re.compile(r"^\[回复消息\]")
# [图片:AB12....jpg] / [视频:...mp4] / [文件:name.ext] / [动画表情]
MEDIA_PLACEHOLDER = re.compile(
    r"\[(图片|视频|文件|语音|动画表情|表情|音乐)(?::[^\]]*)?\]"
)
# Forward previews carry *other people's* words and must not enter the corpus.
FORWARD_MARK = re.compile(r"\[(?:转发消息|聊天记录)[^\]]*\]")
# [某人(1000000001)] -> @某人  (mention with inline uin)
MENTION_UIN = re.compile(r"\[([^\[\]()]{1,24})\((\d{5,12})\)\]")
MENTION = re.compile(r"@[^\s@\[\]]{1,24}")
FACE_ONLY = re.compile(r"^(?:\[\[[^\[\]]{1,16}\]\]\s*)+$")
FACE_TOKEN = re.compile(r"\[\[([^\[\]]{1,16})\]\]")
# Placeholders left after cleaning; they carry no words of the speaker.
PLACEHOLDER_TOKEN = re.compile(r"\[(?:图片|视频|文件|语音|表情|音乐|链接)\]")
# A forward preview always *starts* the message; a mention of it mid-text is fine.
FORWARD_HEAD = re.compile(r"^\s*\[(?:转发消息|聊天记录)")
COMMAND = re.compile(r"^[#＃/／][A-Za-z\u4e00-\u9fff]{1,12}$")
# 注意：`xml` 是「羡慕了」的拼音缩写（见 personality/slang.md），
# 不是 XML 残留，也不是无意义英文 token，别当技术噪声过滤掉。
# Bot logs / tracebacks pasted into chat.
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
# Whole messages that are credentials, not speech: drop them outright.
SENSITIVE = re.compile(
    r"(短信密码|验证码|校验码|动态码|动态密码|支付密码|交易密码"
    r"|银行卡|卡号|尾号\d{4}|身份证号|护照号|开房记录)"
)

ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
SPACES = re.compile(r"[ \t\u3000]+")

# Markdown / structured prose => almost certainly pasted from a model,
# not typed by hand into QQ.
AI_MARKERS = re.compile(
    r"(\*\*[^*\n]{1,30}\*\*|^#{1,4}\s|^\s*[-*]\s{1,2}\S|^\s*\d+[.、]\s{1,2}\S"
    r"|首先|其次|综上|总的来说|值得注意的是|一方面|另一方面)",
    re.MULTILINE,
)

LEN_BUCKETS = [(1, 3), (4, 6), (7, 10), (11, 20), (21, 30), (31, 10**9)]
BUCKET_NAMES = ["1-3 字", "4-6 字", "7-10 字", "11-20 字", "21-30 字", "31 字以上"]


# ---------------------------------------------------------------- loading
def load_messages(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return data["messages"]
    except json.JSONDecodeError:
        pass
    repaired = re.sub(r",(\s*[\]}])", r"\1", text)
    try:
        data = json.loads(repaired)
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return data["messages"]
    except json.JSONDecodeError:
        pass
    raise SystemExit(f"无法解析导出文件：{path}")


def sender_uin(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    if not isinstance(sender, dict):
        return ""
    for key in ("uin", "userId", "id"):
        value = sender.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def baseline_keys(paths: Iterable[Path]) -> tuple[set[tuple[str, str]], set[str]]:
    """(time, text) pairs and whitespace-free texts already in the corpus."""
    pairs: set[tuple[str, str]] = set()
    texts: set[str] = set()
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
                text = str(row.get("text") or "")
                pairs.add((str(row.get("time") or ""), text))
                texts.add(re.sub(r"\s+", "", text))
    return pairs, texts


def load_baseline_rows(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Existing corpus rows, normalised so they can sit next to fresh ones."""
    rows: list[dict[str, Any]] = []
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
                row.setdefault("kind", "text")
                row.setdefault("in_baseline", True)
                rows.append(row)
    return rows


# ---------------------------------------------------------------- cleaning
def clean_text(raw: str, uins: set[str], counters: Counter) -> str:
    text = REPLY_PREFIX.sub("", raw)
    text = ZERO_WIDTH.sub("", text)

    text = MEDIA_PLACEHOLDER.sub(lambda m: f"[{m.group(1) if m.group(1) != '动画表情' else '表情'}]", text)

    # [昵称(123456)] -> @昵称 ; then drop every mention token itself.
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

    # Everything before the first real character is usually a mention gap.
    text = SPACES.sub(" ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip(" \n")


def classify(text: str, msg_type: str, message: dict[str, Any], max_chars: int) -> str:
    if message.get("system") or msg_type == "system":
        return "system"
    if message.get("recalled"):
        return "recalled"
    if msg_type == "forward":
        return "forward"
    if msg_type in ("json", "ark", "music", "markdown"):
        return "card"
    if msg_type in ("video", "file", "audio", "ptt"):
        return "attachment"
    body = text.strip()
    if not body:
        return "empty"
    # Credential-bearing messages (bank SMS codes, card numbers) never belong
    # in a style corpus, masked or not.
    if SENSITIVE.search(body) and re.search(r"\d{4,}", body):
        return "sensitive"
    # Bot commands (#model / #记忆 ...) are operational, never style material.
    if COMMAND.match(body):
        return "command"
    if BOT_LOG.match(body) or TRACEBACK.match(body):
        return "log"
    if FACE_ONLY.match(body):
        return "face"
    # Judge content after placeholders are removed: "[图片]" is not speech.
    residue = PLACEHOLDER_TOKEN.sub("", body).strip()
    if not residue:
        return "media"
    if not re.search(r"[\w\u4e00-\u9fff]", residue):
        return "emoji" if any(unicodedata.category(c) in ("So", "Sk", "Mn") for c in residue) else "noise"
    if len(text) > max_chars:
        return "long"
    if FORWARD_HEAD.match(text):
        return "forward-preview"
    if AI_MARKERS.search(text) and (text.count("\n") >= 2 or len(text) > 80):
        return "ai-paste"
    return "text"


# ---------------------------------------------------------------- reporting
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
        "newline": sum(1 for t in texts if "\n" in t),
    }


def top_ngrams(texts: list[str], limit: int = 30) -> list[tuple[str, int]]:
    counter: Counter = Counter()
    for text in texts:
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            for i in range(len(chunk) - 1):
                counter[chunk[i:i + 2]] += 1
    stop = {"什么", "这个", "那个", "就是", "可以", "没有", "一个", "不是", "真的", "然后"}
    return [(g, c) for g, c in counter.most_common(400) if c >= 3 and g not in stop][:limit]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def default_dir_name(export: Path, chat_name: str) -> str:
    """`<chat>_<yyyymmdd>` taken from the exporter's filename stamp."""
    match = re.search(r"_(\d{8})_\d{6,}", export.name)
    stamp = match.group(1) if match else "export"
    safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", chat_name).strip("_")
    return f"{safe}_{stamp}"


# ---------------------------------------------------------------- main flow
def main() -> int:
    parser = argparse.ArgumentParser(description="清洗本人视角的 QQ 导出为风格语料。")
    parser.add_argument("export", type=Path, help="QQChatExporter 导出的 json 文件")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="输出目录，默认 dataset/cleaned/<群名>_<日期>")
    parser.add_argument("--qq", default="", help="目标 QQ 号，默认取 chatInfo.selfUin")
    parser.add_argument("--nickname", default="", help="目标昵称，默认取 chatInfo.selfName")
    parser.add_argument("--baseline", action="append", default=[],
                        help="已有的语料 jsonl，用于跨文件去重；可重复")
    parser.add_argument("--max-chars", type=int, default=150,
                        help="超过该长度的消息单独归入 long，不进主语料")
    parser.add_argument("--redact", action="append", default=[], metavar="词语",
                        help="需要屏蔽的词语（真实姓名等），替换为 [人名]；可重复或逗号分隔")
    parser.add_argument("--exclude-id", action="append", default=[], metavar="消息id",
                        help="人工判定要剔除的消息 id（如转述他人的发言）；可重复或逗号分隔")
    args = parser.parse_args()

    if not args.export.is_file():
        print(f"找不到导出文件：{args.export}", file=sys.stderr)
        return 1

    messages = load_messages(args.export)
    meta_text = args.export.read_text(encoding="utf-8-sig", errors="replace")
    chat_info: dict[str, Any] = {}
    try:
        chat_info = json.loads(meta_text).get("chatInfo") or {}
    except json.JSONDecodeError:
        pass
    qq = args.qq or str(chat_info.get("selfUin") or "")
    nickname = args.nickname or str(chat_info.get("selfName") or qq)
    chat_name = str(chat_info.get("name") or args.export.stem)
    scene = "group" if str(chat_info.get("type") or "group") == "group" else "friend"

    out_dir = args.output_dir or (DEFAULT_OUTPUT_DIR / default_dir_name(args.export, chat_name))
    baseline_files = [Path(p) for p in args.baseline]
    if not baseline_files and DEFAULT_BASELINE:
        baseline_files = [Path(DEFAULT_BASELINE)]
    base_pairs, base_texts = baseline_keys(baseline_files)

    counters: Counter = Counter()
    corpus: list[dict[str, Any]] = []
    faces: list[dict[str, Any]] = []
    long_rows: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_texts: set[str] = set()
    uins = {qq} if qq else set()
    redact_words = [w.strip() for entry in args.redact for w in entry.split(",") if w.strip()]
    exclude_ids = {w.strip() for entry in args.exclude_id for w in entry.split(",") if w.strip()}

    for message in messages:
        if not isinstance(message, dict):
            continue
        msg_type = str(message.get("type") or "")
        if str(message.get("id") or "") in exclude_ids:
            counters["人工排除"] += 1
            dropped.append({"id": message.get("id"), "reason": "manual", "raw": ""})
            continue
        content = message.get("content") if isinstance(message.get("content"), dict) else {}
        raw = content.get("text") if isinstance(content.get("text"), str) else ""
        own = sender_uin(message)
        if own and qq and own != qq:
            counters["非本人"] += 1
            dropped.append({"id": message.get("id"), "reason": "非本人", "raw": raw[:80]})
            continue

        cleaned = clean_text(raw, uins, counters)
        for word in redact_words:
            if word and word in cleaned:
                counters["人名脱敏"] += cleaned.count(word)
                cleaned = cleaned.replace(word, "[人名]")
        kind = classify(cleaned, msg_type, message, args.max_chars)
        if kind not in ("text", "face", "emoji", "long"):
            counters[kind] += 1
            dropped.append({"id": message.get("id"), "reason": kind, "raw": raw[:80]})
            continue

        record = {
            "id": message.get("id"),
            "time": message.get("time") or "",
            "qq_id": qq,
            "nickname": nickname,
            "text": cleaned,
            "kind": kind,
            "scene": scene,
            "chat": chat_name,
            "has_media": bool(re.search(r"\[(?:图片|视频|文件|表情)\]", cleaned)),
            "source": args.export.name,
        }

        pair = (record["time"], cleaned)
        # Same speaker repeating "难绷" is style evidence, not noise: only
        # drop true re-exports (same timestamp + same text within this batch).
        if pair in seen_pairs:
            counters["重复_本轮"] += 1
            dropped.append({"id": record["id"], "reason": "duplicate", "raw": cleaned[:80]})
            continue
        seen_pairs.add(pair)
        record["in_baseline"] = pair in base_pairs
        if record["in_baseline"]:
            counters["重复_旧语料"] += 1

        if kind in ("face", "emoji"):
            record["faces"] = FACE_TOKEN.findall(cleaned)
            faces.append(record)
        elif kind == "long":
            record["chars"] = len(cleaned)
            record["ai_like"] = bool(AI_MARKERS.search(cleaned))
            record["newlines"] = cleaned.count("\n")
            long_rows.append(record)
        else:
            corpus.append(record)

    corpus.sort(key=lambda r: r["time"])
    faces.sort(key=lambda r: r["time"])
    long_rows.sort(key=lambda r: r["time"])

    out_dir.mkdir(parents=True, exist_ok=True)
    n_corpus = write_jsonl(out_dir / "corpus.jsonl", corpus)
    n_new = write_jsonl(out_dir / "corpus_new.jsonl", [r for r in corpus if not r["in_baseline"]])
    merged = load_baseline_rows(baseline_files) + [r for r in corpus if not r["in_baseline"]]
    merged.sort(key=lambda r: str(r.get("time") or ""))
    n_merged = write_jsonl(out_dir / "corpus_merged.jsonl", merged)
    n_faces = write_jsonl(out_dir / "faces.jsonl", faces)
    n_long = write_jsonl(out_dir / "long.jsonl", long_rows)
    n_drop = write_jsonl(out_dir / "dropped.jsonl", dropped)

    # ---------------- report
    texts = [r["text"] for r in corpus]
    prof = length_profile(texts)
    configured = {}
    if STICKERS.is_file():
        try:
            configured = json.loads(STICKERS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            configured = {}
    face_counts = Counter(f for r in faces for f in r["faces"])
    face_in_text = Counter(f for r in corpus for f in FACE_TOKEN.findall(r["text"]))
    all_faces = face_counts + face_in_text
    total_msgs = len(messages)

    lines = ["# 单人语料清洗报告", ""]
    lines.append(f"- 源文件：`{args.export.name}`")
    lines.append(f"- 会话：{chat_name}（{scene}）")
    lines.append(f"- 目标账号：{nickname}（{qq}）")
    lines.append(f"- 原始消息：{total_msgs} 条（本人 {total_msgs - counters['非本人']}，他人 {counters['非本人']}）")
    if counters["非本人"] == 0:
        lines.append("- 说明：这是**本人视角**的导出，只有自己的发言，对方消息未包含，因此只能做风格语料，做不了对话配对")
    if args.export.stat().st_size:
        lines.append(f"- 文件大小：{args.export.stat().st_size / 1024 / 1024:.1f} MB")
    lines.append("")
    lines.append("## 产出")
    lines.append("")
    lines.append(f"| 文件 | 条数 | 说明 |")
    lines.append("|---|---|---|")
    lines.append(f"| `corpus.jsonl` | {n_corpus} | 主语料全量：可读自然文本，已清洗脱敏；`in_baseline` 标出旧语料已收录的 |")
    lines.append(f"| `corpus_new.jsonl` | {n_new} | 仅本次净新增（`in_baseline=false`），**合并用这个**，不会和旧语料重复 |")
    lines.append(f"| `corpus_merged.jsonl` | {n_merged} | 旧语料 + 本次净新增，已经可以直接当全量语料用 |")
    lines.append(f"| `faces.jsonl` | {n_faces} | 表情包/emoji 单发，不计入文字长度分布 |")
    lines.append(f"| `long.jsonl` | {n_long} | 超过 {args.max_chars} 字的整段，疑似粘贴/AI 生成，需人工过 |")
    lines.append(f"| `dropped.jsonl` | {n_drop} | 被丢弃的条目及原因，供审计 |")
    lines.append("")
    lines.append("## 丢弃原因")
    lines.append("")
    reason_label = {
        "system": "系统消息（撤回/入群等）",
        "recalled": "已撤回",
        "forward": "合并转发",
        "forward-preview": "以转发摘要开头（他人话语）",
        "card": "JSON/小程序卡片",
        "attachment": "纯视频/文件/语音",
        "empty": "空文本",
        "media": "纯图片/视频占位，无正文",
        "noise": "纯符号",
        "command": "机器人指令（#开头）",
        "sensitive": "含验证码/银行卡等凭据，整条拦截",
        "log": "机器人日志/报错堆栈",
        "manual": "人工指定剔除（如转述他人发言）",
        "duplicate": "本轮内同时间同文本重复",
        "非本人": "非目标账号",
    }
    for key in ("system", "recalled", "forward", "forward-preview", "card", "attachment",
                "empty", "media", "noise", "command", "sensitive", "log", "manual",
                "duplicate", "非本人"):
        if counters.get(key):
            lines.append(f"- {reason_label.get(key, key)}：{counters[key]} 条")
    if counters.get("sensitive"):
        lines.append("")
        lines.append("## 敏感凭据拦截")
        lines.append("")
        lines.append(f"- 有 **{counters['sensitive']} 条**消息含验证码/银行卡/尾号等凭据，已整条丢弃，未写入任何输出文件")
        lines.append("- 建议回头确认这些内容当时是否公开发出过；语料侧已清除，原始导出仍然保留")
    lines.append("")
    lines.append("## 去重")
    lines.append("")
    lines.append(f"- 与旧语料重叠（同时间+同文本，仍保留在 `corpus.jsonl`，仅标记）：{counters.get('重复_旧语料', 0)} 条")
    lines.append(f"- 本轮内部重复：{counters.get('重复_本轮', 0)} 条（已丢）")
    lines.append(f"- 注：口头禅式重复（如「对」「难绷」）按真实行为保留，不做文本级去重")
    lines.append("")
    lines.append("## 脱敏与清洗计数")
    lines.append("")
    for key, label in (("提及_已剥离", "@提及被剥离"), ("提及_带号码", "带 QQ 号的提及转成 @昵称"),
                       ("链接", "链接替换为 [链接]"), ("手机号", "手机号"),
                       ("身份证", "身份证"), ("邮箱", "邮箱"),
                       ("长数字", "9-11 位长数字（疑似 QQ 号）"),
                       ("人名脱敏", "指定词（真实姓名等）替换为 [人名]")):
        if counters.get(key):
            lines.append(f"- {label}：{counters[key]} 处")
    if redact_words:
        lines.append(f"- 本次屏蔽词：{'、'.join(redact_words)}")
    lines.append("")
    lines.append("## 长度分布（主语料）")
    lines.append("")
    lines.append(f"- 条数 {prof['count']}，均值 {prof.get('mean')} 字，中位数 {prof.get('p50')}，P90 {prof.get('p90')}，最长 {prof.get('max')}")
    lines.append("")
    lines.append("| 区间 | 条数 | 占比 |")
    lines.append("|---|---|---|")
    for name in BUCKET_NAMES:
        count = prof.get("buckets", {}).get(name, 0)
        share = count * 100 / max(1, prof["count"])
        lines.append(f"| {name} | {count} | {share:.0f}% |")
    lines.append("")
    endings = prof.get("endings", {})
    rendered = "、".join(f"{k} {v}（{v * 100 // max(1, prof['count'])}%）" for k, v in endings.items())
    lines.append(f"- 句末：{rendered}")
    lines.append(f"- 含半角/全角括号：{prof.get('paren', 0)} 条")
    lines.append(f"- 含换行：{prof.get('newline', 0)} 条")
    lines.append("")
    lines.append("## 高频整句（短消息原样，最能反映口头禅）")
    lines.append("")
    whole = Counter(t for t in texts if 1 <= len(t) <= 8)
    lines.append("、".join(f"「{t}」×{c}" for t, c in whole.most_common(30) if c >= 3) or "（无重复短句）")
    lines.append("")
    lines.append("## 高频片段（2 字 n-gram）")
    lines.append("")
    lines.append("、".join(f"{g}×{c}" for g, c in top_ngrams(texts, 40)))
    lines.append("")
    lines.append("## 表情使用")
    lines.append("")
    face_kinds = Counter(r["kind"] for r in faces)
    lines.append(f"- 表情包单发 {face_kinds.get('face', 0)} 次，emoji 单发 {face_kinds.get('emoji', 0)} 次")
    lines.append(f"- `[[名字]]` 共 {len(all_faces)} 种；出现在句中共 {sum(face_in_text.values())} 次")
    lines.append(f"- 已在 `personality/stickers.json` 配置：{len(configured)} 种（{('、'.join(configured) or '无')}）")
    lines.append("")
    lines.append("| 表情 | 次数 | 已配置 |")
    lines.append("|---|---|---|")
    for name, count in all_faces.most_common(25):
        lines.append(f"| `[[{name}]]` | {count} | {'是' if name in configured else '否'} |")
    unconfigured = [n for n, _ in all_faces.most_common() if n not in configured]
    lines.append("")
    lines.append(f"- 未配置的高频表情（如果要扩充 stickers.json 可优先考虑）：{('、'.join(f'[[{n}]]' for n in unconfigured[:12]) or '无')}")
    lines.append("")
    if long_rows:
        ai_like = sum(1 for r in long_rows if r["ai_like"])
        lines.append("## 长文本待复核")
        lines.append("")
        lines.append(f"- {n_long} 条超过 {args.max_chars} 字，其中 {ai_like} 条带 Markdown/结构化特征（高度疑似 AI 生成或从别处粘贴）")
        lines.append(f"- 这些既不是自然口语，也可能是别人/AI 写的东西，所以默认隔离，**不要直接并回主语料**")
        lines.append("")
        lines.append("| 日期 | 字数 | AI 味 | 开头 |")
        lines.append("|---|---|---|---|")
        for row in long_rows:
            head = row["text"][:36].replace("\n", " ").replace("|", "/")
            lines.append(f"| {row['time'][:10]} | {row['chars']} | {'是' if row['ai_like'] else '否'} | {head} |")
        lines.append("")
    lines.append("## 字段")
    lines.append("")
    lines.append("`id` / `time` / `qq_id` / `nickname` / `text` / `kind` / `scene` / `chat` / `has_media` / `in_baseline` / `source`")
    lines.append("")
    (out_dir / "CLEANING_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"源消息 {total_msgs} 条 -> 主语料 {n_corpus}（净新增 {n_new}）/ 表情 {n_faces} / 长文 {n_long} / 丢弃 {n_drop}")
    print(f"输出目录：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

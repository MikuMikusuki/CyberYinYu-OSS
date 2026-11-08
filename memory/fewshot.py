"""检索式 few-shot：从真实群聊配对里找出「类似情境下本人是怎么接的」。

用法：
    from memory.fewshot import fewshot_context
    block = fewshot_context("什么玩意", k=3)   # 返回可直接塞 tail 的字符串

设计取舍：
  * 不用 embedding —— 项目没有 embedding 端点，且 4929 条规模下字符 2-gram
    加 IDF 权重已经够用，启动加载 <1s，查询 <5ms。
  * 只做「风格示范」不做「答案回填」：渲染时会明确要求学语气、不要照抄内容，
    否则模型会把示例里的具体名词当成当前答案（「什么玩意」→ 直接抄 GTA）。
  * 索引常驻内存，文件改了要重启才生效 —— 与 otowa.env 同一条规矩，和 memes.json
    「每次重读」不同，这点在 docstring 里写死，避免以后有人误判。
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
# 配对文件由 dataset/build_ctx_corpus.py 产出；本仓库不附带任何真实聊天产物，
# 路径可覆盖。文件不存在时索引为空，功能静默关闭（不影响启动）。
DEFAULT_INDEX_PATH = Path(
    os.getenv("FEWSHOT_INDEX", str(PROJECT_DIR / "dataset" / "cleaned" / "ctx_pairs.jsonl"))
)

MAX_QUERY_CHARS = 60
NGRAM = 2
MIN_QUERY_GRAMS = 1


def _normalize(text: str) -> str:
    text = re.sub(r"\[[^\[\]]{1,8}\]", "", text or "")
    text = re.sub(r"@\S{1,16}", "", text)
    return re.sub(r"[\s\W_]+", "", text)


def _grams(text: str) -> list[str]:
    if len(text) < NGRAM:
        return [text] if text else []
    return [text[i : i + NGRAM] for i in range(len(text) - NGRAM + 1)]


class FewShotIndex:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_INDEX_PATH
        self.entries: list[dict] = []
        self._grams: list[Counter] = []
        self._idf: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw: list[dict] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ctx = row.get("ctx") or []
                reply = (row.get("reply") or "").strip()
                if not ctx or not reply:
                    continue
                trigger = ctx[-1]
                trig_text = _normalize(trigger.get("text", ""))
                if len(trig_text) < MIN_QUERY_GRAMS:
                    continue
                raw.append(
                    {
                        "trigger": trigger.get("text", "")[:60],
                        "trigger_norm": trig_text,
                        "speaker": trigger.get("name", ""),
                        "reply": reply[:60],
                        "ctx": [f"{c.get('name', '')}：{c.get('text', '')[:40]}" for c in ctx[:-1]],
                        "chat": row.get("chat", ""),
                    }
                )

        if not raw:
            return

        self.entries = raw
        self._grams = [Counter(_grams(e["trigger_norm"])) for e in raw]
        df: Counter = Counter()
        for grams in self._grams:
            df.update(set(grams))
        total = len(raw)
        self._idf = {g: (total / (1 + n)) ** 0.5 for g, n in df.items()}

    def __len__(self) -> int:
        return len(self.entries)

    def search(self, query: str, k: int = 3, exclude: str | None = None) -> list[dict]:
        norm = _normalize(query)[:MAX_QUERY_CHARS]
        qgrams = Counter(_grams(norm))
        if not qgrams or not self.entries:
            return []
        qset = set(qgrams)
        qweight = sum(self._idf.get(g, 1.0) for g in qset) or 1.0

        scored: list[tuple[float, int]] = []
        for idx, grams in enumerate(self._grams):
            if exclude and self.entries[idx]["trigger_norm"] == exclude:
                continue
            inter = qset & set(grams)
            if not inter:
                continue
            # 加权 Jaccard：既要覆盖 query，也不能靠一条超长条目蒙分
            iw = sum(self._idf.get(g, 1.0) for g in inter)
            uw = qweight + sum(self._idf.get(g, 1.0) for g in set(grams)) - iw
            score = iw / uw if uw else 0.0
            scored.append((score, idx))

        scored.sort(reverse=True)
        hits = []
        for score, idx in scored[:k]:
            item = dict(self.entries[idx])
            item["score"] = round(score, 3)
            hits.append(item)
        return hits


_INDEX: FewShotIndex | None = None


def get_index() -> FewShotIndex:
    global _INDEX
    if _INDEX is None:
        _INDEX = FewShotIndex()
    return _INDEX


def render(hits: list[dict], *, max_items: int = 3) -> str:
    """把命中条目渲染成 tail 块。措辞必须挡住「照抄具体内容」。"""
    hits = [h for h in hits if h.get("reply")][:max_items]
    if not hits:
        return ""
    lines = [f"「{h['trigger']}」→「{h['reply']}」" for h in hits]
    return (
        "群里遇到类似的话，平时是这么接的（学句式和语气，"
        "不要照搬里面的具体名词或答案）：\n" + "\n".join(lines)
    )


def fewshot_context(query: str, *, k: int = 3, min_score: float = 0.12) -> str:
    """查询入口：分数太低就不注入（宁可没有，也别给不相关的示范）。"""
    hits = [h for h in get_index().search(query, k=k) if h["score"] >= min_score]
    return render(hits)

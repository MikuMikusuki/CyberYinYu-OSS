"""DeepSeek 官方通道：自研搜索（Search）、网页抓取（Fetch）、直连新模型。

⚠️ 与百炼通道**完全隔离**，这是设计约束，不是巧合：

    维度      本模块                         百炼通道
    --------- ------------------------------ -----------------------------
    配置      DEEPSEEK_TOOLS_* / DEEPSEEK_API_KEY
                                               BAILIAN_WEB_SEARCH_ENABLED /
                                               IMAGE_SEARCH_*
    端点      api.deepseek.com（官方直连）     maas.qianwen… 网关
    鉴权      DEEPSEEK_API_KEY                 AI_API_KEY
    入口      `#ds搜` `#ds抓` `#ds问`          `#搜索`（图搜图）/ enable_search
    结果      本文件的 dataclass 与排版        ai/bailian_*.py 自己的解析器

**不要**从这里 import 百炼的任何东西，也**不要**把这里的东西接进 ai/bailian_*.py。
两边连 markdown 清理都不共用，就是为了改一边不会波及另一边。

接口依据官方文档（2026-10-17 上线）：
    POST /v1/tools/search   query(≤128) / count(1-50) / ori_question / localization / need_content
    POST /v1/tools/fetch    url(≤8192)
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from config import (
    DEEPSEEK_TOOLS_API_KEY,
    DEEPSEEK_TOOLS_BASE_URL,
    DEEPSEEK_TOOLS_ENABLED,
    DEEPSEEK_TOOLS_FETCH_ENABLED,
    DEEPSEEK_TOOLS_MODEL,
    DEEPSEEK_TOOLS_SEARCH_COUNT,
    DEEPSEEK_TOOLS_SEARCH_ENABLED,
    DEEPSEEK_TOOLS_TIMEOUT_SECONDS,
)

logger = logging.getLogger("cyberyinyu")

SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"
CHAT_PATH = "/chat/completions"

MAX_QUERY_CHARS = 128
MAX_URL_CHARS = 8192
# 单条消息的字数上限（和群记忆列表一致的做法：装不下就拆多条）
MESSAGE_CHARS = 600


class DeepSeekToolError(Exception):
    """Any non-2xx or malformed response from the DeepSeek tools API."""

    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def is_auth(self) -> bool:
        return self.status == 401 or self.code in {"authentication_error", "invalid_request_error"}

    def user_message(self) -> str:
        if self.status == 401:
            return "DeepSeek 官方 key 无效或已失效，需要换一个有效 key。"
        if self.status == 429:
            return "DeepSeek 搜索/抓取触发限流（每账号各 5 次/秒），过一会儿再试。"
        if self.status == 402:
            return "DeepSeek 账户余额不足，搜索/抓取是付费接口。"
        return f"DeepSeek 接口出错（HTTP {self.status or '—'}）：{self}"


# --- 配置与可用性 ---------------------------------------------------------

def availability() -> tuple[bool, str]:
    """Return (usable, reason_when_not)."""
    if not DEEPSEEK_TOOLS_ENABLED:
        return False, "DeepSeek 通道已关闭（DEEPSEEK_TOOLS_ENABLED=0）。"
    if not DEEPSEEK_TOOLS_API_KEY:
        return False, "没配 DEEPSEEK_API_KEY，DeepSeek 官方接口用不了。"
    if not DEEPSEEK_TOOLS_BASE_URL:
        return False, "没配 DEEPSEEK_TOOLS_BASE_URL。"
    return True, ""


def _redacted_key() -> str:
    key = DEEPSEEK_TOOLS_API_KEY
    return f"{key[:4]}****{key[-4:]}" if len(key) >= 8 else "（过短）"


def describe() -> str:
    usable, reason = availability()
    lines = [
        "DeepSeek 官方通道：",
        f"  启用: {'是' if DEEPSEEK_TOOLS_ENABLED else '否'}",
        f"  端点: {DEEPSEEK_TOOLS_BASE_URL}",
        f"  模型: {DEEPSEEK_TOOLS_MODEL}",
        f"  key : {_redacted_key() if DEEPSEEK_TOOLS_API_KEY else '（未设置）'}",
        f"  搜索: {'开' if DEEPSEEK_TOOLS_SEARCH_ENABLED else '关'}（默认 {DEEPSEEK_TOOLS_SEARCH_COUNT} 条）",
        f"  抓取: {'开' if DEEPSEEK_TOOLS_FETCH_ENABLED else '关'}",
        "  ——以下为百炼通道，与本通道无关——",
        f"  百炼联网搜索: {'开' if _bailian_flag() else '关'}",
    ]
    if not usable:
        lines.insert(1, f"  ⚠️ 不可用：{reason}")
    return "\n".join(lines)


def _bailian_flag() -> bool:
    """只读展示用；不参与本模块任何判断。"""
    from config import BAILIAN_WEB_SEARCH_ENABLED

    return bool(BAILIAN_WEB_SEARCH_ENABLED)


# --- HTTP 基础 ------------------------------------------------------------

def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {DEEPSEEK_TOOLS_API_KEY}",
        "Content-Type": "application/json",
    }


def _base() -> str:
    """主机部分。即使有人把 /v1 或 /chat/completions 抄进配置，也自动剥掉。"""
    base = DEEPSEEK_TOOLS_BASE_URL
    for suffix in ("/chat/completions", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base.rstrip("/")


async def _post(session: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    url = f"{_base()}{path}"
    async with session.post(
        url,
        json=body,
        headers=_headers(),
        timeout=DEEPSEEK_TOOLS_TIMEOUT_SECONDS,
    ) as response:
        raw = await response.text()
        status = response.status
    if status != 200:
        message, code = raw, ""
        try:
            error = json.loads(raw).get("error") or {}
            message = error.get("message") or raw
            code = str(error.get("type") or error.get("code") or "")
        except Exception:
            pass
        logger.warning("DeepSeek 工具调用失败: path=%s status=%s code=%s", path, status, code)
        raise DeepSeekToolError(message[:200], status=status, code=code)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeepSeekToolError(f"返回不是合法 JSON: {raw[:120]}") from exc


# --- Search --------------------------------------------------------------

@dataclass
class SearchHit:
    title: str = ""
    url: str = ""
    snippet: str = ""
    summary: str = ""
    content: str = ""
    site_name: str = ""
    date: str = ""
    rerank_score: float = 0.0
    authority: int = -1


@dataclass
class SearchResult:
    query: str = ""
    hits: list[SearchHit] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.hits


def _as_hit(item: dict[str, Any]) -> SearchHit:
    def text(key: str) -> str:
        value = item.get(key)
        return "" if value is None else str(value).strip()

    try:
        score = float(item.get("rerank_score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        authority = int(item.get("authority"))
    except (TypeError, ValueError):
        authority = -1
    return SearchHit(
        title=text("title"),
        url=text("url"),
        snippet=text("snippet"),
        summary=text("summary"),
        content=text("content"),
        site_name=text("site_name"),
        date=text("date"),
        rerank_score=score,
        authority=authority,
    )


async def search(
    session: Any,
    query: str,
    *,
    count: int | None = None,
    need_content: bool = False,
    ori_question: str = "",
) -> SearchResult:
    """POST /v1/tools/search —— DeepSeek 自研搜索引擎。"""
    usable, reason = availability()
    if not usable:
        raise DeepSeekToolError(reason)
    if not DEEPSEEK_TOOLS_SEARCH_ENABLED:
        raise DeepSeekToolError("DeepSeek 搜索已关闭（DEEPSEEK_TOOLS_SEARCH_ENABLED=0）。")
    query = " ".join(query.split())
    if not query:
        raise DeepSeekToolError("搜索词不能为空。")
    if len(query) > MAX_QUERY_CHARS:
        # 官方上限 128 字，超了直接截断比报错好用。
        query = query[:MAX_QUERY_CHARS]
    body: dict[str, Any] = {
        "query": query,
        "count": max(1, min(50, int(count or DEEPSEEK_TOOLS_SEARCH_COUNT))),
        "need_content": bool(need_content),
    }
    if ori_question:
        body["ori_question"] = ori_question[:MAX_QUERY_CHARS]
    payload = await _post(session, SEARCH_PATH, body)
    hits = [_as_hit(item) for item in payload.get("results") or [] if isinstance(item, dict)]
    logger.info("DeepSeek 搜索完成: query=%r 命中 %d 条", query, len(hits))
    return SearchResult(query=query, hits=hits)


# --- Fetch ---------------------------------------------------------------

@dataclass
class FetchedPage:
    url: str = ""
    title: str = ""
    content: str = ""


async def fetch(session: Any, url: str) -> FetchedPage:
    """POST /v1/tools/fetch —— 抓单个网页，返回 Markdown 正文。"""
    usable, reason = availability()
    if not usable:
        raise DeepSeekToolError(reason)
    if not DEEPSEEK_TOOLS_FETCH_ENABLED:
        raise DeepSeekToolError("DeepSeek 抓取已关闭（DEEPSEEK_TOOLS_FETCH_ENABLED=0）。")
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise DeepSeekToolError("要抓的地址必须以 http:// 或 https:// 开头。")
    if len(url) > MAX_URL_CHARS:
        raise DeepSeekToolError("地址过长（上限 8192 字符）。")
    payload = await _post(session, FETCH_PATH, {"url": url})
    page = FetchedPage(
        url=url,
        title=str(payload.get("title") or "").strip(),
        content=str(payload.get("content") or ""),
    )
    logger.info("DeepSeek 抓取完成: url=%s 正文 %d 字", url[:60], len(page.content))
    return page


# --- 直连模型（配合搜索结果答题）-----------------------------------------

async def probe(session: Any) -> str:
    """轻量连通性/鉴权检查：只打 /models，不产生 Search/Fetch 计费。"""
    usable, reason = availability()
    if not usable:
        return reason
    try:
        async with session.get(
            f"{_base()}/models",
            headers=_headers(),
            timeout=DEEPSEEK_TOOLS_TIMEOUT_SECONDS,
        ) as response:
            status = response.status
    except Exception as exc:  # noqa: BLE001 - 网络异常原因要原样带出来
        return f"连不上 {_base()}：{type(exc).__name__}: {exc}"[:160]
    if status == 200:
        return "鉴权正常，Search / Fetch 可用。"
    if status == 401:
        return "key 无效或已失效（401），需要换一个有效 key。"
    return f"接口返回 HTTP {status}。"


async def ask(session: Any, question: str, *, context: str = "", system: str = "") -> str:
    """用 DeepSeek 官方新模型（默认 deepseek-flash = V4.1-Flash-1017）作答。

    这是独立的模型通道：不经过网关，也不碰百炼。
    """
    usable, reason = availability()
    if not usable:
        raise DeepSeekToolError(reason)
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    content = f"{context}\n\n问题：{question}" if context else question
    messages.append({"role": "user", "content": content})
    body = {
        "model": DEEPSEEK_TOOLS_MODEL,
        "messages": messages,
        "temperature": 1.0,
        "max_tokens": 900,
        "stream": False,
    }
    payload = await _post(session, CHAT_PATH, body)
    choices = payload.get("choices") or []
    if not choices:
        raise DeepSeekToolError("模型没有返回任何 choice。")
    message = choices[0].get("message") or {}
    text = str(message.get("content") or "").strip()
    logger.info("DeepSeek 新模型作答完成: model=%s 输出 %d 字", DEEPSEEK_TOOLS_MODEL, len(text))
    return text


# --- 排版（本模块自用，不复用百炼的）-------------------------------------

def _chunk(lines: list[str], limit: int = MESSAGE_CHARS) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def format_search(result: SearchResult, *, max_items: int = 0) -> list[str]:
    """标题行 + 每条两行（标题/链接 + 摘要），超长自动分页。"""
    if result.empty:
        return [f"DeepSeek 没搜到「{result.query}」的结果。"]
    hits = result.hits[:max_items] if max_items else result.hits
    lines = [f"DeepSeek 搜索「{result.query}」：{len(hits)} 条"]
    for index, hit in enumerate(hits, 1):
        meta = []
        if hit.site_name:
            meta.append(hit.site_name)
        if hit.date:
            meta.append(hit.date[:10])
        meta.append(f"权威 {hit.authority}" if hit.authority >= 0 else "权威 未知")
        meta.append(f"相关度 {hit.rerank_score:g}")
        lines.append(f"{index}. {hit.title or '(无标题)'}")
        lines.append(f"   {hit.url}")
        lines.append(f"   {' · '.join(meta)}")
        body = hit.snippet or hit.summary
        if body:
            lines.append(f"   {_short(body, 160)}")
    return _chunk(lines)


def format_page(page: FetchedPage, *, limit_chars: int = 0) -> list[str]:
    """标题 + 正文（Markdown 原样，超长分页）。"""
    content = page.content.strip()
    if not content:
        return [f"抓到了「{page.title or page.url}」但正文是空的。"]
    if limit_chars and len(content) > limit_chars:
        content = content[:limit_chars] + "\n…（已截断）"
    header = f"DeepSeek 抓取：{page.title or '(无标题)'}\n{page.url}\n"
    return _chunk([header, *content.splitlines()])


def format_for_model(result: SearchResult, *, max_items: int = 5) -> str:
    """把搜索结果压成给模型读的资料块（用 summary，不是 snippet）。"""
    blocks = []
    for index, hit in enumerate(result.hits[:max_items], 1):
        body = hit.summary or hit.snippet or hit.content
        blocks.append(
            f"[{index}] {hit.title}\n{hit.url}\n{_short(body, 700)}"
        )
    return "\n\n".join(blocks)

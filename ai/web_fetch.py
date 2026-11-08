"""打开一个具体链接，把正文取回来给模型读（网页 / PDF 都行）。

两条通道，按内容类型分工，不重复造轮子：

    HTML → 先走 DeepSeek 官方 ``/v1/tools/fetch``（返回干净 Markdown，别人已经
           处理过 JS 渲染和正文提取）；官方抓不到再退回本地 aiohttp + 简易 HTML
           转文本，保证「能抓到」比「抓得漂亮」优先。
    PDF  → 本地下载 + pypdf 抽文本。官方 fetch 对 PDF 直接 404（2026-10-29 实测
           `failed to fetch URL`），所以 PDF 只能自己来。

安全边界（这是全项目唯一一个「让模型决定去访问哪个地址」的入口，必须挡住）：
``fetch_url`` 只允许 http/https，并且**解析后的每个 IP 都必须是公网地址**——
localhost / 127.0.0.0/8 / 10.x / 192.168.x / 169.254.x（云元数据）等一律拒绝，
否则一句「打开 http://127.0.0.1:8080/status」就能把内网读穿。
"""

import asyncio
import io
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlparse

from config import (
    WEB_FETCH_MAX_BYTES,
    WEB_FETCH_MAX_CHARS,
    WEB_FETCH_TIMEOUT_SECONDS,
)

logger = logging.getLogger("cyberyinyu")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

# 单次抓取最多读多少字节，防一个超大文件把内存吃满（12MB 默认，够一份论文）。
CHUNK = 64 * 1024

_BLOCKED_NETWORKS = [
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
]


class FetchError(Exception):
    """抓取失败。``user_message()`` 给模型看的解释，别把 traceback 丢给模型。"""

    def __init__(self, message: str, *, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind

    def user_message(self) -> str:
        return f"打开链接失败：{self}"


@dataclass
class FetchedDoc:
    url: str = ""
    title: str = ""
    text: str = ""
    kind: str = ""  # pdf / html / text
    truncated: bool = False
    total_chars: int = 0


# --- 地址安全检查 ---------------------------------------------------------

def _is_public_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
        return False
    if ip.is_reserved or ip.is_unspecified:
        return False
    return not any(ip in net for net in _BLOCKED_NETWORKS)


async def assert_public_url(url: str) -> str:
    """校验并规范化 URL；内网地址直接抛 FetchError。"""
    url = (url or "").strip().strip("<>").strip()
    if not url:
        raise FetchError("地址是空的。")
    if not url.lower().startswith(("http://", "https://")):
        raise FetchError("只支持 http:// 或 https:// 开头的地址。")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not host:
        raise FetchError("地址里没有主机名。")
    if len(url) > 8192:
        raise FetchError("地址过长（上限 8192 字符）。")

    def resolve() -> list[str]:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
        return [info[4][0] for info in infos]

    try:
        addresses = await asyncio.to_thread(resolve)
    except socket.gaierror as exc:
        raise FetchError(f"域名解析不了（{host}）。") from exc
    if not addresses:
        raise FetchError(f"域名解析不了（{host}）。")
    for address in addresses:
        if not _is_public_ip(address):
            # 内网地址不解释细节，避免被当成扫描器用。
            raise FetchError("这个地址指向内网，不能打开。", kind="blocked")
    return url


# --- HTML → 文本 ----------------------------------------------------------

class _TextExtractor(HTMLParser):
    """够用的正文提取：丢掉脚本/样式/导航，保留可读文本。"""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    BLOCK_TAGS = {
        "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "ul", "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\u00a0]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, text). 纯 stdlib，不引依赖。"""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - 脏 HTML 不该让整次抓取失败
        pass
    return " ".join(parser.title.split()), parser.text()


# --- 下载 ----------------------------------------------------------------

async def _download(session: Any, url: str) -> tuple[bytes, str]:
    """返回 (bytes, content_type)。超过上限直接截断，不报错。"""
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    async with session.get(url, headers=headers, timeout=WEB_FETCH_TIMEOUT_SECONDS,
                           allow_redirects=True, max_redirects=4) as response:
        if response.status != 200:
            raise FetchError(f"网站返回 HTTP {response.status}。")
        content_type = (response.headers.get("Content-Type") or "").lower()
        buffer = bytearray()
        async for chunk in response.content.iter_chunked(CHUNK):
            buffer.extend(chunk)
            if len(buffer) >= WEB_FETCH_MAX_BYTES:
                logger.info("抓取超过上限，已截断: url=%s", url[:80])
                break
    return bytes(buffer), content_type


def _pdf_text(data: bytes) -> tuple[str, str]:
    """(title, text)。pypdf 是纯 Python，放线程里跑，别堵事件循环。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 部署漏装才会走到
        raise FetchError("服务器没装 pypdf，读不了 PDF。") from exc

    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(pages)
    title = ""
    try:
        meta = reader.metadata or {}
        title = str(meta.get("/Title") or "").strip()
    except Exception:  # noqa: BLE001 - metadata 坏掉不影响正文
        title = ""
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


def _truncate(text: str) -> tuple[str, bool, int]:
    """超长时保留开头 + 结尾，砍中间。

    论文/公告的结构是「摘要→方法→结果→结论」，只留开头等于把结论扔掉，
    模型就会一直说「我只读到前一部分」。中间砍掉的部分留个标记，它知道有缺口。
    """
    total = len(text)
    if total <= WEB_FETCH_MAX_CHARS:
        return text, False, total
    head_chars = int(WEB_FETCH_MAX_CHARS * 0.7)
    tail_chars = WEB_FETCH_MAX_CHARS - head_chars
    omitted = total - head_chars - tail_chars
    merged = (
        f"{text[:head_chars]}\n\n……（中间省略 {omitted} 字）……\n\n{text[-tail_chars:]}"
    )
    return merged, True, total


def _looks_like_pdf(url: str, content_type: str) -> bool:
    if "pdf" in content_type:
        return True
    path = unquote(urlparse(url).path).lower()
    return path.endswith(".pdf")


async def fetch_url(session: Any, url: str) -> FetchedDoc:
    """抓一个地址，返回正文。PDF 本地解析，HTML 优先走官方 fetch。"""
    url = await assert_public_url(url)

    # 1) 明显是 PDF（.pdf 结尾）：直接本地下载 + pypdf，别浪费一次官方调用。
    if _looks_like_pdf(url, ""):
        data, content_type = await _download(session, url)
        if not data:
            raise FetchError("下载回来是空的。")
        if _looks_like_pdf(url, content_type) or data[:5] == b"%PDF-":
            title, text = await asyncio.to_thread(_pdf_text, data)
        else:
            title, text = html_to_text(data.decode("utf-8", "replace"))
    else:
        title, text = await _fetch_html(session, url)

    if not text.strip():
        raise FetchError("抓到了，但正文是空的（可能要登录或者是纯图）。")
    text, truncated, total = _truncate(text)
    doc = FetchedDoc(url=url, title=title.strip(), text=text,
                     kind="pdf" if _looks_like_pdf(url, "") else "html",
                     truncated=truncated, total_chars=total)
    logger.info(
        "抓取完成: kind=%s %d 字%s url=%s",
        doc.kind, total, "（已截断）" if truncated else "", url[:80],
    )
    return doc


async def _fetch_html(session: Any, url: str) -> tuple[str, str]:
    """先官方 fetch（干净 Markdown），失败再本地抓。返回 (title, text)。"""
    from ai import deepseek_tools

    if deepseek_tools.DEEPSEEK_TOOLS_FETCH_ENABLED and deepseek_tools.availability()[0]:
        try:
            page = await deepseek_tools.fetch(session, url)
            if page.content.strip():
                return page.title, page.content
            logger.info("官方 fetch 返回空正文，改本地抓: %s", url[:80])
        except deepseek_tools.DeepSeekToolError as exc:
            logger.info("官方 fetch 失败（%s），改本地抓: %s", str(exc)[:80], url[:80])

    data, content_type = await _download(session, url)
    if data[:5] == b"%PDF-":
        return await asyncio.to_thread(_pdf_text, data)
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type)
    if match:
        charset = match.group(1)
    try:
        html = data.decode(charset, "replace")
    except LookupError:
        html = data.decode("utf-8", "replace")
    return html_to_text(html)

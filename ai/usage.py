"""仅保存计量元数据；SQLite 跨进程持久化，不保存请求、响应正文或凭据。"""
from __future__ import annotations

import asyncio
import functools
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger("cyberyinyu.usage")
DB_PATH = Path(__file__).resolve().parents[1] / "data" / "usage.sqlite3"
_LOCK = threading.Lock()
_STORAGE_ERRORS = 0
_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")


def field(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
        return number if number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_usage(usage: Any, *, applicable: bool = True) -> dict[str, Any]:
    """兼容 Chat / Responses / Images；reasoning 是输出子集，不另加到总量。"""
    def first(*names):
        for name in names:
            value = _number(field(usage, name))
            if value is not None:
                return value
        return None

    prompt = first("prompt_tokens", "input_tokens")
    completion = first("completion_tokens", "output_tokens")
    total = first("total_tokens")
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    details = field(usage, "completion_tokens_details") or field(usage, "output_tokens_details")
    reasoning = _number(field(details, "reasoning_tokens"))
    if reasoning is None:
        reasoning = first("reasoning_tokens")
    values = dict(zip(_TOKEN_KEYS, (prompt, completion, total, reasoning)))
    known = sum(value is not None for value in (prompt, completion, total))
    state = "reported" if known == 3 else "partial" if known or reasoning is not None else "missing" if applicable else "not_applicable"
    return {**values, "usage_state": state}


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=2)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS usage_events (
            at REAL NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
            operation TEXT NOT NULL, status TEXT NOT NULL, usage_state TEXT NOT NULL,
            prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
            reasoning_tokens INTEGER, images INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("CREATE INDEX IF NOT EXISTS usage_events_at ON usage_events(at)")
        return conn
    except Exception:
        conn.close()
        raise


def _record(*, kind, name, operation, status, usage=None, applicable=True, images=0):
    global _STORAGE_ERRORS
    try:
        normalized = normalize_usage(usage, applicable=applicable)
        if status == "rejected":
            normalized = normalize_usage(None, applicable=False)
        # 名称和功能只能由代码常量或已配置模型标识传入，不接受用户文本。
        with _LOCK:
            conn = _connect()
            try:
                with conn:
                    conn.execute("INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                        time.time(), kind, str(name)[:160], str(operation)[:100], status,
                        normalized["usage_state"], *(normalized[key] for key in _TOKEN_KEYS),
                        _number(images) or 0,
                    ))
            finally:
                conn.close()
    except Exception:
        _STORAGE_ERRORS += 1
        # 不记录异常原文，数据库/供应商错误可能携带敏感内容。
        logger.warning("用量记录写入失败，本次业务调用不受影响")


def record_model(*, model: str, operation: str = "unclassified", success=True, usage=None,
                 status=None, images=0):
    _record(kind="model", name=model, operation=operation,
            status=status or ("success" if success else "failed"), usage=usage, images=images)


def record_tool(name: str, *, operation="tool", success=True, usage=None, status=None,
                applicable=True, images=0):
    _record(kind="tool", name=name, operation=operation,
            status=status or ("success" if success else "failed"), usage=usage,
            applicable=applicable, images=images)


async def track_model_call(call, *, model: str, operation: str, image_response=False):
    """每次显式 SDK/HTTP 调用计一次；SDK 内部重试不可见。计量错误不得改变业务结果。"""
    try:
        response = await call()
    except asyncio.CancelledError:
        record_model(model=model, operation=operation, status="cancelled")
        raise
    except Exception:
        record_model(model=model, operation=operation, success=False)
        raise
    try:
        status = field(response, "status")
        success = status not in {"failed", "cancelled", "incomplete"} and not field(response, "error")
        if field(response, "choices") == []:
            success = False
        items = (field(response, "data") or []) if image_response else []
        if image_response:
            success = success and bool(items)
        record_model(model=model, operation=operation, success=success,
                     usage=field(response, "usage"), images=len(items))
    except Exception:
        record_model(model=model, operation=operation, status="unknown")
        logger.warning("模型用量解析失败，业务结果保持原样")
    return response


async def track_tool_call(call, *, name: str, operation="tool", applicable=True):
    try:
        response = await call()
    except asyncio.CancelledError:
        record_tool(name, operation=operation, status="cancelled", applicable=applicable)
        raise
    except Exception:
        record_tool(name, operation=operation, success=False, applicable=applicable)
        raise
    try:
        success = not field(response, "error") and field(response, "status") not in {"failed", "cancelled", "incomplete"}
        record_tool(name, operation=operation, success=success, usage=field(response, "usage"), applicable=applicable)
    except Exception:
        record_tool(name, operation=operation, status="unknown", applicable=applicable)
        logger.warning("工具用量解析失败，业务结果保持原样")
    return response


def tracked_tool(name, *, applicable=True):
    """仅用于确实发起外部请求的底层函数，不装饰缓存/校验/限流分支。"""
    def decorate(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await track_tool_call(lambda: func(*args, **kwargs), name=name,
                                         applicable=applicable)
        return wrapper
    return decorate


@asynccontextmanager
async def track_http_request(request, *, name: str):
    """计量图片下载等无独立模型用量的 HTTP 请求，保留原有异常处理。"""
    outcome = "failed"
    try:
        async with request as response:
            outcome = "success" if 200 <= response.status < 400 else "failed"
            yield response
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:
        outcome = "failed"
        raise
    finally:
        record_tool(name, status=outcome, applicable=False)


def _bucket():
    return {"calls": 0, "success": 0, "failed": 0, "cancelled": 0, "unknown": 0,
            "rejected": 0, "unobserved": 0, "usage_reported": 0, "usage_partial": 0, "usage_missing": 0,
            "usage_not_applicable": 0, **{key: None for key in _TOKEN_KEYS},
            "images": 0, "last_at": None}


def _add(bucket, row):
    status = row["status"]
    executed = status not in {"rejected", "unobserved"}
    bucket["calls"] += row["n"] * int(executed)
    bucket[status] += row["n"]
    bucket["usage_" + row["usage_state"]] += row["n"] * int(executed)
    for key in _TOKEN_KEYS:
        if row[key] is not None:
            bucket[key] = (bucket[key] or 0) + row[key]
    bucket["images"] += row["images"]
    bucket["last_at"] = max(bucket["last_at"] or 0, row["at"])


def snapshot(period="all") -> dict[str, Any]:
    if period not in {"today", "7d", "all"}:
        raise ValueError("period 仅支持 today、7d、all")
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = 0 if period == "all" else (start - timedelta(days=6) if period == "7d" else start).timestamp()
    result = {"period": period, "timezone": str(now.astimezone().tzinfo), "since": since,
              "storage": "sqlite", "storage_errors": _STORAGE_ERRORS, "model": _bucket(),
              "models": {}, "operations": {}, "tools": {}, "images": {}, "first_at": None}
    with _LOCK:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            result["first_at"] = conn.execute("SELECT MIN(at) FROM usage_events").fetchone()[0]
            # 聚合在数据库内完成，避免 all 查询随历史记录增长占满内存。
            rows = conn.execute("""SELECT kind,name,operation,status,usage_state,
                MAX(at) AS at, COUNT(*) AS n, SUM(prompt_tokens) AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens, SUM(total_tokens) AS total_tokens,
                SUM(reasoning_tokens) AS reasoning_tokens, SUM(images) AS images
                FROM usage_events WHERE at>=? GROUP BY kind,name,operation,status,usage_state""", (since,)).fetchall()
        finally:
            conn.close()
    for raw in rows:
        row = dict(raw)
        stores = [result["tools"].setdefault(row["name"], _bucket())] if row["kind"] == "tool" else [
            result["model"], result["models"].setdefault(row["name"], _bucket()),
            result["operations"].setdefault(row["operation"], _bucket())]
        for bucket in stores:
            _add(bucket, row)
    return result

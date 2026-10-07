"""结构化日志 + 链路追踪。

## 为什么要结构化

`print("job#123 完成")` 在生产里没法用：不能按 job_id 过滤、不能算各阶段耗时、
不能被日志系统（Loki / ELK）索引。结构化日志一行一条记录、字段可查。

## 两档格式

    LOG_FORMAT=text   本地开发，人类可读（默认）
    LOG_FORMAT=json   生产 / 被采集，一行一个 JSON

## 链路（trace）

用 `contextvars` 存当前上下文（job_id / item_id / worker / request_id），
formatter 自动把它合进**每条**日志 —— 调用方不用每次手动带 job_id。

关键：`contextvars` 是**按线程**的，线程池里每个工作线程有自己的上下文，
所以 8 路并发审核之间不会串号。绑定之后这一条链路上所有日志都能被 grep 到一起。
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time

# 当前上下文（job_id / item_id / ...），formatter 会自动合进日志
_TRACE: contextvars.ContextVar[dict] = contextvars.ContextVar("trace", default={})

# 每条日志自己的字段放这里：log.info("x", extra={"fields": {...}})
FIELDS_ATTR = "fields"

_configured = False


# ============================================================
# 链路上下文
# ============================================================

def bind_trace(**fields) -> None:
    """把字段绑到当前线程的上下文。值为 None 的会被忽略。"""
    cur = dict(_TRACE.get())
    cur.update({k: v for k, v in fields.items() if v is not None})
    _TRACE.set(cur)


def clear_trace() -> None:
    _TRACE.set({})


def current_trace() -> dict:
    return dict(_TRACE.get())


def new_trace_id(prefix: str = "req") -> str:
    return f"{prefix}-{os.urandom(4).hex()}"


# ============================================================
# Formatter
# ============================================================

def _merged(record: logging.LogRecord) -> dict:
    """上下文 + 本条日志自带的字段，后者优先。"""
    data = dict(_TRACE.get())
    data.update(getattr(record, FIELDS_ATTR, None) or {})
    return data


class JsonFormatter(logging.Formatter):
    """一行一个 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        data.update(_merged(record))
        if record.exc_info:
            data["error"] = self.formatException(record.exc_info)[:2000]
        return json.dumps(data, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """人类可读：时间 级别 事件  字段=值 …"""

    def format(self, record: logging.LogRecord) -> str:
        ctx = _merged(record)
        suffix = "  " + " ".join(f"{k}={v}" for k, v in ctx.items()) if ctx else ""
        line = (f"{time.strftime('%H:%M:%S', time.localtime(record.created))} "
                f"{record.levelname:<5} {record.getMessage()}{suffix}")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# ============================================================
# 初始化
# ============================================================

def setup_logging(component: str = "app", level: str | None = None,
                  fmt: str | None = None) -> None:
    """初始化根 logger。幂等：重复调用不会重复挂 handler。"""
    global _configured
    if _configured:
        return
    _configured = True

    fmt = (fmt or os.getenv("LOG_FORMAT", "text")).lower()
    level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    handler = logging.StreamHandler(sys.stdout)      # 走 stdout，后台任务采集得到
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # 降噪：uvicorn / httpx 的访问日志默认太吵
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 组件名进上下文，这样两个进程的日志能区分开
    bind_trace(component=component)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

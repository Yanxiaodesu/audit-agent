"""连接管理。

## 为什么按线程缓存连接

每个审核任务会打开好几条连接：读商品、读卖家、每写一步 trace 各一次、
写回业务库……规则层单件 4-5 次，模型层更多。

实测（并发 8、40 件样本）：每步都新建连接的版本，P50 从 0.34s 涨到 **1.87s** ——
并发不但没提速多少，单件还慢了 5 倍。瓶颈不是模型调用，
而是 **Windows 上 MySQL 建连的开销**（每次 20-50ms），并发下互相叠加。

所以这里按线程缓存连接：

  - pymysql 连接**不是线程安全的** → 按线程隔离，天然安全
  - 取用时 `ping(reconnect=True)`，被服务端断开也能自愈
  - 每个 `with` 块结束一定 commit / rollback，不留悬挂事务和长快照

注意：这里**不要**把连接改成全局共享的。多线程共用一个 pymysql 连接
会串数据、报 "Packet sequence number wrong"，是并发改造里最容易踩的坑。

⚠ 另一个坑：**PyMySQL 的 `Connection.__exit__` 是 `self.close()`**，
不是 sqlite3 那种「只提交不关闭」。所以

    with meta_conn() as conn:      # ❌ 退出时会关掉缓存连接！
        ...

会静默毁掉线程缓存，之后任何复用都会报 `InterfaceError(0, '')`。
需要事务语义就用 `meta()` / `biz()` 上下文管理器；
需要自己操作连接就显式 `conn.commit()`，**不要对它用 `with`**。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from .config import BIZ_DB, DB_CONFIG, META_DB

_local = threading.local()


def connect(db: str | None = None, **overrides):
    """新建一条连接（不复用）。需要独立连接时用它。"""
    cfg = dict(DB_CONFIG)
    if db:
        cfg["database"] = db
    cfg.update(overrides)
    return pymysql.connect(cursorclass=DictCursor, **cfg)


def thread_conn(db: str):
    """取本线程缓存的连接；没有、或已断开就新建一条。"""
    slot = f"conn::{db}"
    conn = getattr(_local, slot, None)
    if conn is not None:
        # conn.open 为 False 说明它被外部 close 掉了。
        # 有调用方习惯自己 close，这里必须容忍并换一条，不能直接复用。
        if not getattr(conn, "open", False):
            setattr(_local, slot, None)
        else:
            try:
                # 用 reconnect=False：连接断了会抛异常，由下面重建。
                # （ping(reconnect=True) 在新版 PyMySQL 已废弃）
                conn.ping(reconnect=False)
                return conn
            except Exception:  # noqa: BLE001 —— 连接坏了就换一条，不上抛
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                setattr(_local, slot, None)
    conn = connect(db)
    setattr(_local, slot, conn)
    return conn


def close_thread_conns():
    """关闭本线程缓存的全部连接。进程退出前调用。"""
    for slot in list(vars(_local)):
        if not slot.startswith("conn::"):
            continue
        conn = getattr(_local, slot, None)
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            delattr(_local, slot)
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def _ctx(db: str, commit: bool = False):
    conn = thread_conn(db)
    try:
        with conn.cursor() as cur:
            yield cur
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 —— 回滚失败不应盖掉原始异常
            pass
        raise


def meta(commit: bool = False):
    """Agent 元数据库（audit_agent）。"""
    return _ctx(META_DB, commit)


def biz(commit: bool = False):
    """平台业务库（campus_market）。"""
    return _ctx(BIZ_DB, commit)


def meta_conn():
    """元数据库的本线程缓存连接（不要在调用方 close）。"""
    return thread_conn(META_DB)


def biz_conn():
    """业务库的本线程缓存连接（不要在调用方 close）。"""
    return thread_conn(BIZ_DB)

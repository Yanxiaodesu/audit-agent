"""审核任务消费者（worker）—— 并发版。

## 为什么用线程池而不是 asyncio

任务几乎全是 IO 等待：等模型 HTTP 响应（单件 5–10 秒）、等数据库。
CPU 占用可以忽略。而 pymysql 和 httpx 都只有同步接口，
硬上 asyncio 要把整条链路重写，收益不抵成本。线程池对 IO 密集任务完全够用。

实测：200 件（其中 37 件走模型）从**串行 280 秒**降到并发 8 路约 **40 秒**。

## 并发下必须守住的四条

1. **每个线程用自己的数据库连接** —— pymysql 连接不是线程安全的，
   多线程共用一个连接会串数据、报 "Packet sequence number wrong"。
2. **每个任务用自己的 Tracker** —— 否则步号互相覆盖（这个 bug 真实发生过）。
3. **claim 用独立连接**，和业务处理分开，避免长时间事务互相干扰。
4. **不无限预取** —— 在途任务数封顶，否则一个 worker 把队列抽干，
   别的 worker 没活干，也失去了水平扩展的意义。

## 三个老坑仍然处理着

- **MySQL ERROR 1093**：`UPDATE ... WHERE id = (SELECT ... FROM 同一张表)` 不允许，
  必须拆成「先 SELECT ... FOR UPDATE SKIP LOCKED 拿 id，再 UPDATE ... WHERE id = ?」。
- **僵尸任务**：worker 被 kill 后任务卡在 running，靠 `reclaim()` 回收。
- **失败重试**：未达上限退回 pending，超过上限标 failed。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from app.config import (JOB_LOCK_TIMEOUT_MIN, MAX_ATTEMPTS, RECLAIM_EVERY,
                        WORKER_CONCURRENCY)
from app.db import close_thread_conns, meta_conn
from app.logging_setup import bind_trace, clear_trace, get_logger, setup_logging
from agent import audit as audit_mod

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"
log = get_logger("worker")

CLAIM_SELECT = """
SELECT id, item_id, attempts
FROM audit_jobs
WHERE status = 'pending'
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED
"""

CLAIM_UPDATE = """
UPDATE audit_jobs
SET status = 'running', locked_by = %s, locked_at = NOW(), started_at = NOW(),
    attempts = attempts + 1
WHERE id = %s
"""

# 耗时直接用 SQL 算：started_at 在 claim 时就落好了。
# IFNULL 是兜底 —— 被 reclaim 回收过的任务 started_at 曾被清空过。
DURATION_SQL = "TIMESTAMPDIFF(MICROSECOND, IFNULL(started_at, NOW()), NOW()) DIV 1000"

RECLAIM_REQUEUE = """
UPDATE audit_jobs
SET status = 'pending', locked_by = NULL, locked_at = NULL,
    started_at = NULL,
    error = CONCAT('worker 超时未完成，已退回队列（第 ', attempts, ' 次尝试）')
WHERE status = 'running'
  AND locked_at IS NOT NULL
  AND locked_at < NOW() - INTERVAL %s MINUTE
  AND attempts < %s
"""

RECLAIM_FAIL = f"""
UPDATE audit_jobs
SET status = 'failed', duration_ms = {DURATION_SQL}, locked_by = NULL, locked_at = NULL,
    error = CONCAT('worker 超时且已达重试上限（attempts=', attempts, '）')
WHERE status = 'running'
  AND locked_at IS NOT NULL
  AND locked_at < NOW() - INTERVAL %s MINUTE
  AND attempts >= %s
"""

# 清理孤儿轨迹：job 已经不在、但步骤还在。
#
# 什么时候会产生：**在 worker 正在处理时把 job 删掉** ——
# 删完之后 worker 又写了几步，这些步骤就再也没有归属了。
# （真实发生过：测试清理跑在 worker 写轨迹之前，留下 4 条孤儿步。）
#
# 用多表 DELETE 而不是 `WHERE job_id NOT IN (SELECT ... FROM audit_jobs)` ——
# 后者会撞 MySQL ERROR 1093（不能在子查询里引用被删的表）。
# 接口层也已经加了「有任务在跑时拒绝物理删除」的前置校验，这里是兜底。
ORPHAN_STEPS = """
DELETE s FROM audit_steps s
LEFT JOIN audit_jobs j ON j.id = s.job_id
WHERE j.id IS NULL
"""

ORPHAN_CHECKPOINTS = """
DELETE c FROM checkpoints c
LEFT JOIN audit_jobs j ON j.id = c.job_id
WHERE j.id IS NULL
"""


# ============================================================
# 队列操作
# ============================================================

def claim(conn):
    """一个事务内完成「选中 + 占住」。队列空则返回 None。"""
    with conn.cursor() as cur:
        cur.execute(CLAIM_SELECT)
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None
        cur.execute(CLAIM_UPDATE, (WORKER_ID, row["id"]))
        conn.commit()
    return row


def reclaim(conn, minutes: int = JOB_LOCK_TIMEOUT_MIN, max_attempts: int = MAX_ATTEMPTS):
    """回收僵尸任务，并顺手清理孤儿轨迹。

    返回 `(退回队列数, 判失败数, 清理的孤儿步数)`。
    """
    with conn.cursor() as cur:
        cur.execute(RECLAIM_REQUEUE, (minutes, max_attempts))
        requeued = cur.rowcount
        cur.execute(RECLAIM_FAIL, (minutes, max_attempts))
        failed = cur.rowcount
        # 孤儿轨迹兜底清理（见 ORPHAN_STEPS 的注释）
        cur.execute(ORPHAN_STEPS)
        orphans = cur.rowcount
        cur.execute(ORPHAN_CHECKPOINTS)
        orphans += cur.rowcount
        conn.commit()
    return requeued, failed, orphans


# ============================================================
# 单任务处理
# ============================================================

def process(conn, job):
    """跑完一件商品的审核并写库。

    Tracker 在**这里创建一次**，run() 和 writeback() 共用同一个实例。
    """
    job_id, item_id = job["id"], job["item_id"]

    tracker = audit_mod.Tracker(job_id)
    result, tracker = audit_mod.run(item_id, job_id, tracker)
    written = audit_mod.writeback(item_id, job_id, result, tracker=tracker)

    with conn.cursor() as cur:
        # ⚠ `AND status = 'running'` 是并发守卫，不能省：
        # 如果本任务在我们处理期间被 reclaim 回收（锁超时）或被人为取消，
        # 现在这一行已经不是 running 了 —— 那我们就不该把状态盖成 succeeded，
        # 否则会覆盖另一个 worker 正在写的结果。
        cur.execute(
            f"""UPDATE audit_jobs
                SET status = 'succeeded', verdict = %s, confidence = %s, reason = %s,
                    rule_hits = %s, source = %s, fingerprint = %s,
                    duration_ms = {DURATION_SQL},
                    locked_by = NULL, locked_at = NULL, error = NULL
                WHERE id = %s AND status = 'running'""",
            (result["verdict"], result.get("confidence"), result.get("reason") or "",
             json.dumps(result.get("rule_hits") or [], ensure_ascii=False),
             result.get("source"), result.get("fingerprint"), job_id),
        )
        superseded = cur.rowcount == 0
        conn.commit()

    if superseded:
        # 结论已经写进业务库了，但任务状态不归我们管了（被别人回收/取消）
        log.warning("job.superseded", extra={"fields": {
            "job_id": job_id, "item_id": item_id,
            "note": "任务在处理期间被回收或取消，终态未被本次覆盖"}})
    return result, written


def fail(conn, job, exc: BaseException):
    """失败处理。

    返回 `(是否重试, 是否取消)`：
      - **商品已删除 → 取消**，不重试（确定性终态，重试没有意义）
      - 还有重试机会 → 退回队列
      - 超过重试上限 → 标记 failed
    """
    conn.rollback()
    attempts = int(job.get("attempts") or 1)
    msg = f"{type(exc).__name__}: {exc}"[:2000]

    # 商品被删/不可审核：直接取消，别浪费三次重试，也别污染 failed 指标
    if isinstance(exc, audit_mod.ItemNotAuditable):
        with conn.cursor() as cur:
            cur.execute(
                f"""UPDATE audit_jobs
                    SET status = 'cancelled', duration_ms = {DURATION_SQL},
                        error = %s, locked_by = NULL, locked_at = NULL
                    WHERE id = %s""",
                (f"{msg}（任务已取消，不重试）", job["id"]),
            )
        conn.commit()
        return False, True

    retry = attempts < MAX_ATTEMPTS
    with conn.cursor() as cur:
        if retry:
            cur.execute(
                """UPDATE audit_jobs
                   SET status = 'pending', error = %s, locked_by = NULL, locked_at = NULL
                   WHERE id = %s""",
                (f"{msg}（将重试，第 {attempts} 次失败）", job["id"]),
            )
        else:
            cur.execute(
                f"""UPDATE audit_jobs
                    SET status = 'failed', duration_ms = {DURATION_SQL},
                        error = %s, locked_by = NULL, locked_at = NULL
                    WHERE id = %s""",
                (f"{msg}（已达重试上限 {MAX_ATTEMPTS}）", job["id"]),
            )
        conn.commit()
    return retry, False


def process_one(job: dict) -> dict:
    """在**工作线程**里跑一个任务。

    连接来自 `thread_conn()` 的**本线程缓存**，所以这里不 close ——
    下一个任务直接复用同一条连接，省掉每次 20-50ms 的建连开销。
    线程之间不会共享连接（缓存是按线程的），pymysql 的线程安全问题不存在。
    """
    t0 = time.perf_counter()
    # 绑定链路：这个任务后续的所有日志（含各阶段、工具调用、护栏拦截）
    # 都会自动带上 job_id / item_id，能直接 grep 出一次审核的完整链路。
    # contextvars 是按线程的，8 路并发之间不会串号。
    bind_trace(job_id=job["id"], item_id=job["item_id"], worker=WORKER_ID)
    conn = meta_conn()
    try:
        result, written = process(conn, job)
        return {"ok": True, "job": job, "result": result, "written": written,
                "elapsed": time.perf_counter() - t0}
    except Exception as exc:  # noqa: BLE001 —— 单任务失败不能拖垮 worker
        try:
            retry, cancelled = fail(conn, job, exc)
        except Exception:  # noqa: BLE001 —— 连失败都写不进去时不阻塞线程退出
            retry, cancelled = False, False
        return {"ok": False, "job": job, "exc": exc, "retry": retry,
                "cancelled": cancelled, "elapsed": time.perf_counter() - t0}
    finally:
        clear_trace()


# ============================================================
# 主循环
# ============================================================

def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    idx = min(len(xs) - 1, max(0, int(round(p / 100 * len(xs) + 0.5)) - 1))
    return xs[idx]


def main():
    ap = argparse.ArgumentParser(description="audit-agent 审核消费者（并发）")
    ap.add_argument("--concurrency", type=int, default=WORKER_CONCURRENCY,
                    help=f"并发处理的任务数，默认 {WORKER_CONCURRENCY}")
    ap.add_argument("--once", action="store_true", help="只处理一个任务就退出")
    ap.add_argument("--drain", action="store_true", help="把队列跑空后退出（评测用）")
    ap.add_argument("--interval", type=float, default=0.3, help="队列空时的轮询间隔（秒）")
    ap.add_argument("--quiet", action="store_true", help="不打印每个任务")
    ap.add_argument("--reclaim-only", action="store_true", help="只回收僵尸任务然后退出")
    args = ap.parse_args()

    setup_logging("worker")

    concurrency = 1 if args.once else max(1, args.concurrency)
    # 在途上限：留一点缓冲让线程池不空转，但不要把队列抽干
    cap = 1 if args.once else concurrency * 2

    # ---- 启动时回收僵尸任务 ----
    conn = meta_conn()
    rq, fl, orphans = reclaim(conn)
    if rq or fl or orphans:
        log.warning("reclaim.startup", extra={"fields": {
            "requeued": rq, "failed": fl, "orphan_steps": orphans}})
    if args.reclaim_only:
        close_thread_conns()
        return

    log.info("worker.started", extra={"fields": {
        "worker": WORKER_ID, "concurrency": concurrency,
        "prefetch_cap": cap, "attempt_limit": MAX_ATTEMPTS,
    }})

    running = True

    def stop(*_):
        nonlocal running
        if running:
            running = False
            log.info("worker.stopping", extra={"fields": {"inflight": len(inflight)}})

    signal.signal(signal.SIGINT, stop)
    try:
        signal.signal(signal.SIGTERM, stop)
    except (ValueError, AttributeError):
        pass

    stats = {"ok": 0, "fail": 0, "llm": 0, "latencies": []}
    t_start = time.perf_counter()
    inflight: dict = {}

    def handle(r: dict):
        job = r["job"]
        stats["latencies"].append(r["elapsed"])
        if r["ok"]:
            stats["ok"] += 1
            if r["result"].get("source") == "llm":
                stats["llm"] += 1
            if not args.quiet:
                fields = {
                    "job_id": job["id"], "item_id": job["item_id"],
                    "verdict": r["result"]["verdict"],
                    "source": r["result"].get("source"),
                    "elapsed_ms": int(r["elapsed"] * 1000),
                }
                if r["result"].get("cost"):
                    fields["cost"] = r["result"]["cost"]
                if r["written"].get("skipped"):
                    fields["skipped"] = r["written"]["skipped"]
                log.info("job.done", extra={"fields": fields})
        else:
            stats["fail"] += 1
            log_fn = log.warning if r.get("cancelled") else log.error
            log_fn("job.cancelled" if r.get("cancelled") else "job.failed",
                   extra={"fields": {
                       "job_id": job["id"], "item_id": job["item_id"],
                       "retry": r["retry"],
                       "error": f"{type(r['exc']).__name__}: {r['exc']}",
                       "elapsed_ms": int(r["elapsed"] * 1000),
                   }})

        if RECLAIM_EVERY and stats["ok"] and stats["ok"] % RECLAIM_EVERY == 0:
            # ⚠ meta_conn() 返回的是**主线程的缓存连接**，这里绝不能 close ——
            # 关掉它下一次 claim() 就会 InterfaceError(0, '')。
            # 这个 bug 只有在跑满 RECLAIM_EVERY 个任务之后才会暴露。
            rq, fl, orphans = reclaim(meta_conn())
            if rq or fl or orphans:
                log.warning("reclaim.periodic", extra={"fields": {
                    "requeued": rq, "failed": fl, "orphan_steps": orphans}})

    claim_conn = meta_conn()          # 主线程的连接，专用于 claim，不与工作线程共享
    try:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="audit") as pool:
            while True:
                # 1) 补充在途任务（不超过 cap）
                while running and len(inflight) < cap:
                    job = claim(claim_conn)
                    if job is None:
                        break
                    inflight[pool.submit(process_one, job)] = job

                # 2) 无在途任务
                if not inflight:
                    if args.once or args.drain or not running:
                        break
                    time.sleep(args.interval)
                    continue

                # 3) 等任意一个完成，立刻处理结果并让出调度
                done, _ = wait(set(inflight), timeout=0.25, return_when=FIRST_COMPLETED)
                for fut in done:
                    inflight.pop(fut, None)
                    handle(fut.result())

                if args.once and (stats["ok"] + stats["fail"]) >= 1:
                    running = False

            # 收到停止信号后，等在途任务收尾
            if inflight:
                log.info("worker.draining", extra={"fields": {"inflight": len(inflight)}})
                for fut in list(inflight):
                    inflight.pop(fut, None)
                    handle(fut.result())
    finally:
        # 只关得掉主线程的缓存连接；线程池里各线程的连接随进程退出由 OS 回收。
        # worker 是长驻进程，池内线程会一直复用各自的连接，不会持续增长。
        close_thread_conns()

    # ---- 汇总 ----
    elapsed = time.perf_counter() - t_start
    total = stats["ok"] + stats["fail"]
    lat = stats["latencies"]
    if total:
        log.info("worker.summary", extra={"fields": {
            "worker": WORKER_ID, "total": total, "ok": stats["ok"],
            "failed": stats["fail"], "llm": stats["llm"],
            "elapsed_s": round(elapsed, 1),
            "throughput_per_min": round(total / elapsed * 60, 1) if elapsed else 0,
            "concurrency": concurrency,
            "p50_ms": int(_pct(lat, 50) * 1000),
            "p95_ms": int(_pct(lat, 95) * 1000),
            "max_ms": int(max(lat) * 1000) if lat else 0,
        }})
    else:
        log.info("worker.summary", extra={"fields": {
            "worker": WORKER_ID, "total": 0, "note": "没有处理任何任务"}})


if __name__ == "__main__":
    main()

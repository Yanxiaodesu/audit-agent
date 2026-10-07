"""并发性能对比：串行 vs 线程池。

用 **mock 模式 + 可配置的模拟延迟**，所以：
  - 结果可复现（不依赖网络和模型的实际响应速度）
  - **零 token 成本**，可以反复跑

跑法：
    python scripts/bench_concurrency.py --items 200 --delay 1.5
    python scripts/bench_concurrency.py --levels 1,4,8,16,32
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymysql  # noqa: E402
from pymysql.cursors import DictCursor  # noqa: E402

from app.config import BIZ_DB, DB_CONFIG, META_DB  # noqa: E402


def conn(db=None):
    cfg = {k: v for k, v in DB_CONFIG.items() if k != "database"}
    if db:
        cfg["database"] = db
    return pymysql.connect(cursorclass=DictCursor, **cfg)


def reset_and_enqueue(limit: int) -> int:
    """把商品恢复待审核、清空任务，然后全部投递。返回投递数量。"""
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"UPDATE `{BIZ_DB}`.item SET status = 0, version = 0 WHERE deleted = 0")
            cur.execute(f"DELETE FROM `{BIZ_DB}`.item_audit")
            for t in ("audit_steps", "checkpoints", "review_queue", "audit_jobs"):
                cur.execute(f"DELETE FROM `{META_DB}`.{t}")
            cur.execute(
                f"""INSERT INTO `{META_DB}`.audit_jobs (item_id, status)
                    SELECT id, 'pending' FROM `{BIZ_DB}`.item
                    WHERE status = 0 AND deleted = 0 ORDER BY id LIMIT %s""",
                (limit,),
            )
            n = cur.rowcount
        c.commit()
        return n
    finally:
        c.close()


def run_worker(concurrency: int, delay: float, mock: bool) -> tuple[float, dict]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if mock:
        env["LLM_MODE"] = "mock"
        env["MOCK_LLM_DELAY"] = str(delay)

    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-u", "worker.py", "--drain", "--quiet",
         "--concurrency", str(concurrency)],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    elapsed = time.perf_counter() - t0

    out = (proc.stdout or "") + (proc.stderr or "")
    info = {}
    m = re.search(r"吞吐\s+([\d.]+)\s+件/分钟", out)
    if m:
        info["rate"] = float(m.group(1))
    m = re.search(r"P50\s+([\d.]+)s\s+P95\s+([\d.]+)s\s+最大\s+([\d.]+)s", out)
    if m:
        info["p50"], info["p95"], info["max"] = (float(m.group(i)) for i in (1, 2, 3))
    m = re.search(r"处理 (\d+) 个任务（成功 (\d+)，失败 (\d+)", out)
    if m:
        info["total"], info["ok"], info["fail"] = (int(m.group(i)) for i in (1, 2, 3))
    if proc.returncode != 0:
        info["error"] = out.strip().splitlines()[-1] if out.strip() else f"exit {proc.returncode}"
    return elapsed, info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=200, help="投递多少件商品")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="mock 模式下每轮模型响应的模拟秒数（真实约 3-4 秒）")
    ap.add_argument("--levels", default="1,4,8,16", help="要测的并发档位，逗号分隔")
    ap.add_argument("--real", action="store_true",
                    help="用真实 LLM 而不是 mock（会花钱，且结果受网络影响）")
    args = ap.parse_args()

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    mode_desc = "真实 LLM" if args.real else f"mock（每轮延迟 {args.delay}s）"

    print("=" * 78)
    print("  并发性能对比")
    print("=" * 78)
    print(f"  样本 {args.items} 件   模式 {mode_desc}   档位 {levels}")
    print()

    results = []
    for c in levels:
        n = reset_and_enqueue(args.items)
        print(f"  并发 {c:>2}：投递 {n} 件，运行中 ...", end="", flush=True)
        elapsed, info = run_worker(c, args.delay, mock=not args.real)
        results.append((c, elapsed, info))
        done = info.get("total", 0)
        print(f"\r  并发 {c:>2}：处理 {done} 件，耗时 {elapsed:>6.1f}s，"
              f"吞吐 {info.get('rate', 0):>6.0f} 件/分钟"
              f"   P95 {info.get('p95', 0):>5.2f}s"
              f"{'   ⚠ ' + info['error'] if 'error' in info else ''}")

    base = next((e for c, e, _ in results if c == 1), None)
    print()
    print("  " + "-" * 74)
    print(f"  {'并发':>4}  {'耗时':>9}  {'吞吐(件/分)':>12}  {'P50':>8}  {'P95':>8}  {'加速比':>8}")
    print("  " + "-" * 74)
    for c, elapsed, info in results:
        speedup = f"{base / elapsed:.1f}x" if base and elapsed else "-"
        print(f"  {c:>4}  {elapsed:>8.1f}s  {info.get('rate', 0):>12.0f}  "
              f"{info.get('p50', 0):>7.2f}s  {info.get('p95', 0):>7.2f}s  {speedup:>8}")
    print("  " + "-" * 74)

    ok = next((i for c, _, i in results if c == 1), {})
    if ok.get("fail"):
        print(f"\n  ⚠ 有 {ok['fail']} 个任务失败，并发正确性需要检查")
    else:
        print("\n  所有档位均无任务失败 —— 说明 SKIP LOCKED 在高并发下没有重复消费")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""调查预算的效果对比：不限 vs 有预算。

回答的问题：**给模型一个工具调用预算，它会不会真的开始取舍？精度会不会掉？**

跑两轮全量（真实 LLM），对比：
  - 平均每任务工具调用次数（不限时是固定流程，全调一遍）
  - 一眼可判、一次工具都没调的任务数
  - token 与花费
  - 召回率 / 误杀率 / 复核率（**精度不能退**，退了就说明预算给少了）

用法：
    python scripts/bench_budget.py --budgets 0,3
    python scripts/bench_budget.py --budgets 0,1,2,3,5 --items 200
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import guard  # noqa: E402
from app import metrics as M  # noqa: E402
from app.config import BIZ_DB, META_DB  # noqa: E402
from app.db import meta_conn  # noqa: E402


def reset_and_enqueue(limit: int) -> int:
    conn = meta_conn()
    with conn.cursor() as cur:
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
    conn.commit()
    return n


def run_worker(budget: int, concurrency: int) -> tuple[float, str]:
    env = {
        **os.environ, "PYTHONIOENCODING": "utf-8", "LLM_TOOL_BUDGET": str(budget),
        # 压测期间关掉护栏：否则会被自己的限流器拖慢（默认 60 次/分钟，
        # 一轮 200 件要 ~110 次调用），耗时数据就没意义了。
        "LLM_RATE_PER_MIN": "0", "LLM_DAILY_CALL_LIMIT": "0", "LLM_DAILY_BUDGET": "0",
    }
    t0 = time.perf_counter()
    p = subprocess.run(
        [sys.executable, "-u", "worker.py", "--drain", "--quiet",
         "--concurrency", str(concurrency)],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = (p.stdout or "") + (p.stderr or "")
    return time.perf_counter() - t0, out


def token_stats() -> dict:
    """本轮跑完后的 token 消耗。这是调查预算真正省下来的东西。"""
    conn = meta_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS jobs, COALESCE(SUM(tokens), 0) AS tokens
               FROM audit_steps WHERE kind = 'llm_result'"""
        )
        r = cur.fetchone()
    jobs, tokens = int(r["jobs"] or 0), int(r["tokens"] or 0)
    return {
        "llm_jobs": jobs,
        "tokens": tokens,
        "tokens_per_job": round(tokens / jobs) if jobs else 0,
    }


def label(b: int) -> str:
    return "不限" if b == 0 else str(b)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budgets", default="0,3", help="要对比的预算档位，逗号分隔；0 = 不限")
    ap.add_argument("--items", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    budgets = [int(x) for x in args.budgets.split(",") if x.strip() != ""]

    print("=" * 84)
    print("  调查预算效果对比（真实 LLM）")
    print("=" * 84)

    rows = []
    for b in budgets:
        n = reset_and_enqueue(args.items)
        print(f"  预算 {label(b):>4}：投递 {n} 件，运行中 ...", end="", flush=True)
        elapsed, out = run_worker(b, args.concurrency)

        m = M.compute_metrics()
        M.save_run(f"budget-{label(b)}", m)
        ag = M.agent_stats(minutes=180)
        tk = token_stats()
        spend = guard.snapshot()
        rows.append({"budget": b, "elapsed": elapsed, "m": m, "ag": ag,
                     "tk": tk, "spend": spend})

        err = ""
        if "Traceback" in out:
            err = "  ⚠ worker 报错，见输出"
        print(f"\r  预算 {label(b):>4}：{elapsed:>5.0f}s   工具 "
              f"{ag['avg_tool_calls']:>4} 次/任务   token {tk['tokens_per_job']:>5}/任务   "
              f"召回 {m['recall_pct']}%  误杀 {m['false_reject_pct']}%   "
              f"复核 {m['review_rate_pct']}%{err}")

    base = rows[0]

    print()
    print("  " + "-" * 88)
    print(f"  {'预算':>5}{'调查/任务':>11}{'token/任务':>11}{'预算驳回':>10}"
          f"{'召回':>8}{'误杀':>8}{'复核率':>9}{'耗时':>9}")
    print("  " + "-" * 88)
    for r in rows:
        ag, m, tk = r["ag"], r["m"], r["tk"]
        print(f"  {label(r['budget']):>5}{ag['avg_query_calls']:>11}{tk['tokens_per_job']:>11}"
              f"{ag['budget_denied']:>10}{m['recall_pct']:>7}%{m['false_reject_pct']:>7}%"
              f"{m['review_rate_pct']:>8}%{r['elapsed']:>8.0f}s")
    print("  " + "-" * 88)
    print("  （调查/任务 = 真正的调查工具调用，不含终止调用 submit_verdict）")

    print("\n  工具调用次数分布（纪律性：分布越散说明越在做取舍）")
    for r in rows:
        dist = r["ag"]["distribution"]
        total = sum(dist.values()) or 1
        line = "  ".join(f"{k}次:{v}({v * 100 // total}%)" for k, v in dist.items())
        print(f"    预算 {label(r['budget']):>4}：{line}")

    print("\n  各工具被调用次数")
    for r in rows:
        tools = r["ag"]["tools"]
        line = "  ".join(f"{k}={v}" for k, v in tools.items())
        print(f"    预算 {label(r['budget']):>4}：{line}")

    if base["ag"]["avg_query_calls"] is not None and rows[-1]["ag"]["avg_query_calls"] is not None:
        drop = (1 - rows[-1]["ag"]["avg_query_calls"] / max(base["ag"]["avg_query_calls"], 0.01)) * 100
        print(f"\n  调查调用下降 {drop:.0f}%（{base['ag']['avg_query_calls']} → "
              f"{rows[-1]['ag']['avg_query_calls']} 次/任务）")
    if base["tk"]["tokens_per_job"] and rows[-1]["tk"]["tokens_per_job"]:
        drop = (1 - rows[-1]["tk"]["tokens_per_job"] / base["tk"]["tokens_per_job"]) * 100
        print(f"  单任务 token 下降 {drop:.0f}%（{base['tk']['tokens_per_job']} → "
              f"{rows[-1]['tk']['tokens_per_job']}）")

    ok = all(r["m"]["recall_pct"] >= base["m"]["recall_pct"] - 0.01 for r in rows)
    if ok:
        print("  ✅ 各档位召回率都没有下降 —— 预算没有牺牲检出能力")
    else:
        print("  ⚠ 有档位召回率下降，预算压得太紧了")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

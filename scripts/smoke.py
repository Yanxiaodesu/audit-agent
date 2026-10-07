"""端到端冒烟测试：投递全量 → 审核 → 校验指标。

CI 里跑它；本地改完代码也可以跑一遍确认没坏。

和 `pytest tests/` 的分工：
  - 单元/集成测试测**模块**（快，不起服务）
  - 冒烟测试测**整条链路真的能跑通**（起 API + worker，走 HTTP + 队列 + 数据库）

用法（需要 API 已经在跑）：
    python scripts/smoke.py
    python scripts/smoke.py --base-url http://127.0.0.1:8100 --mock
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAIL = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "✓" if ok else "✗"
    print(f"    {mark} {name}{('  ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def post_json(c: httpx.Client, url: str, name: str, expect=(200,), **kw):
    """POST 并**强制检查状态码**。

    不检查状态码的冒烟测试比没有还危险：422 校验失败的响应体里没有 `enqueued`，
    `r.get("enqueued")` 会静默变成 0，于是"投递 0 件"被当成正常结果报过去。
    （这个坑就是本脚本第一版踩的：传了超出上限的 limit，接口返回 422，脚本却报"通过"。）

    注意 `/audits/batch` 的语义是**异步投递**：有新任务入队返回 202 Accepted，
    全部命中幂等复用才返回 200 —— 两个都算成功。
    """
    resp = c.post(url, **kw)
    if resp.status_code not in expect:
        check(f"{name} 状态码属于 {expect}", False,
              f"HTTP {resp.status_code}: {resp.text[:180]}")
        return None
    try:
        return resp.json()
    except Exception as e:  # noqa: BLE001
        check(f"{name} 返回合法 JSON", False, f"{type(e).__name__}: {e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8100")
    ap.add_argument("--timeout", type=float, default=180.0, help="等队列跑空的秒数")
    args = ap.parse_args()

    c = httpx.Client(base_url=args.base_url, timeout=60)

    print("=" * 68)
    print("  audit-agent  端到端冒烟")
    print("=" * 68)

    # ---- 1. 服务可用 ----
    print("\n  [1/4] 服务可用性")
    try:
        for _ in range(30):
            try:
                c.get("/health").raise_for_status()
                break
            except Exception:
                time.sleep(1)
        h = c.get("/health").json()
        check("/health 返回 UP", h.get("data") == "UP", f"llm_enabled={h.get('llm_enabled')}")
    except Exception as e:  # noqa: BLE001
        print(f"    ✗ 服务不可用：{type(e).__name__}: {e}")
        return 1

    # ⚠ **每一个只读接口都要真的请求一遍**，不能挑几个查。
    # 这里踩过坑：把硬编码库名换成配置常量时漏了一个 import，
    # 结果 `/review-queue` 抛 NameError 变成 500 ——
    # 而当时的冒烟只查了 health / metrics / latency，完全没覆盖到，
    # 是用户打开页面才发现的。这类「改了一处、炸了另一处」的问题，
    # 只有把接口全扫一遍才拦得住。
    print("\n  [1b/4] 全部只读接口")
    read_endpoints = [
        ("/", "演示页面"), ("/metrics", "Prometheus 指标"), ("/progress", "进度"),
        ("/stats", "总览"), ("/budget", "成本护栏"), ("/latency", "延迟分位数"),
        ("/agent", "Agent 行为"), ("/categories", "分类列表"),
        ("/items?limit=5", "商品列表"), ("/review-queue?limit=5", "复核队列"),
        ("/eval/runs?limit=5", "历次评测"), ("/docs", "接口文档"),
        ("/openapi.json", "OpenAPI 描述"),
    ]
    for path, name in read_endpoints:
        try:
            r = c.get(path)
            check(f"GET {path.split('?')[0]}", r.status_code == 200,
                  f"{name} HTTP {r.status_code}"
                  + (f" —— {r.text[:110]}" if r.status_code != 200 else ""))
        except Exception as e:  # noqa: BLE001
            check(f"GET {path.split('?')[0]}", False, f"{type(e).__name__}: {e}")

    # ---- 2. 投递全量 ----
    print("\n  [2/4] 投递与消费")
    c.post("/admin/reset", json={"clear_evals": False})
    # 注意 limit 的上限是 2000（接口用 Query(le=2000) 校验），超了会 422
    r = post_json(c, "/audits/batch", "批量投递", expect=(200, 202),
                  json={"status": 0, "limit": 2000})
    if r is None:
        return 1
    enq = int(r.get("enqueued") or 0)
    check("批量投递有任务入队", enq > 0, f"enqueued={enq}")

    t0 = time.time()
    last = enq
    while time.time() - t0 < args.timeout:
        time.sleep(2)
        p = c.get("/progress").json()
        left = p["jobs"].get("pending", 0) + p["jobs"].get("running", 0)
        if left != last:
            print(f"      剩余 {left} ...", flush=True)
            last = left
        if left == 0:
            break

    p = c.get("/progress").json()
    jobs = p["jobs"]
    done = int(jobs.get("succeeded") or 0)
    failed = int(jobs.get("failed") or 0)
    check("队列已跑空", jobs.get("pending", 0) + jobs.get("running", 0) == 0)
    check("没有失败任务", failed == 0, f"failed={failed}")
    check("任务数等于投递数", done == enq, f"succeeded={done} / enqueued={enq}")

    # ---- 3. 数据完整性 ----
    print("\n  [3/4] 数据完整性")
    try:
        import pymysql
        from pymysql.cursors import DictCursor
        from app.config import DB_CONFIG, META_DB

        cfg = {k: v for k, v in DB_CONFIG.items() if k != "database"}
        cfg["database"] = META_DB
        conn = pymysql.connect(cursorclass=DictCursor, **cfg)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM audit_steps WHERE kind='load'")
            loads = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM audit_steps WHERE kind='writeback'")
            wbs = cur.fetchone()["n"]
            cur.execute("""SELECT COUNT(*) AS n FROM audit_steps s
                           LEFT JOIN audit_jobs j ON j.id = s.job_id
                           WHERE j.id IS NULL""")
            orphans = cur.fetchone()["n"]
        conn.close()
        check("每个任务都有 load 步", loads == done, f"{loads}/{done}")
        check("每个任务都有 writeback 步", wbs == done, f"{wbs}/{done}")
        check("没有孤儿轨迹步", orphans == 0, f"orphans={orphans}")
    except Exception as e:  # noqa: BLE001
        check("数据完整性检查可执行", False, f"{type(e).__name__}: {e}")

    # ---- 4. 判据不退化 ----
    print("\n  [4/4] 判据不退化（对照标注答案）")
    m = post_json(c, "/eval/run?tag=smoke", "评测")
    if m is None:
        pass
    elif "recall_pct" not in m:
        check("评测返回结构完整", False, f"缺少 recall_pct，实际字段：{list(m)[:8]}")
    else:
        check("违规召回率 100%", m["recall_pct"] >= 100.0, f"recall={m['recall_pct']}%")
        check("误杀率 0%", m["false_reject_pct"] <= 0.0,
              f"false_reject={m['false_reject_pct']}%")
        print(f"      复核率 {m['review_rate_pct']}%  自动处理 {m['auto_rate_pct']}%")

    print()
    if FAIL:
        print(f"  ✗ 冒烟失败：{len(FAIL)} 项不通过 -> {', '.join(FAIL)}")
        return 1
    print("  ✓ 冒烟全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""审核指标计算。

命令行（scripts/evaluate.py）和页面（app/main.py）共用这一套，
保证页面上看到的数字和命令行跑出来的一模一样，不会出现两个口径。
"""
from __future__ import annotations

import json

import pymysql
from pymysql.cursors import DictCursor

from app.config import BIZ_DB, DB_CONFIG, META_DB

# 每个商品只取最后一次成功的任务。
# ⚠ 库名必须走 BIZ_DB 而不是硬编码 —— 它是环境变量，改了就全 500。
LATEST_JOB_SQL = f"""
SELECT j.id AS job_id, j.item_id, j.verdict, j.source, j.confidence,
       g.expected, g.violation_type, i.title, i.price, c.name AS category_name
FROM audit_jobs j
JOIN audit_ground_truth g ON g.item_id = j.item_id
LEFT JOIN `{BIZ_DB}`.item i ON i.id = j.item_id
LEFT JOIN `{BIZ_DB}`.item_category c ON c.id = i.category_id
WHERE j.status = 'succeeded'
  AND j.verdict IS NOT NULL
  AND j.id = (SELECT MAX(j2.id) FROM audit_jobs j2
              WHERE j2.item_id = j.item_id AND j2.status = 'succeeded')
"""


def connect(db: str | None = META_DB):
    cfg = {k: v for k, v in DB_CONFIG.items() if k != "database"}
    if db:
        cfg["database"] = db
    return pymysql.connect(cursorclass=DictCursor, **cfg)


def _pct(a: int, b: int) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def compute_metrics() -> dict:
    """算出全部指标。没有已完成的任务时返回 {}。"""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(LATEST_JOB_SQL)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return {}

    for r in rows:
        if r.get("confidence") is not None:
            r["confidence"] = float(r["confidence"])
        if r.get("price") is not None:
            r["price"] = float(r["price"])

    total = len(rows)
    viol = [r for r in rows if r["expected"] == "REJECT"]
    norm = [r for r in rows if r["expected"] == "APPROVE"]
    gray = [r for r in rows if r["expected"] == "REVIEW"]

    caught = [r for r in viol if r["verdict"] == "REJECT"]
    missed = [r for r in viol if r["verdict"] != "REJECT"]
    killed = [r for r in norm if r["verdict"] == "REJECT"]
    auto = [r for r in rows if r["verdict"] in ("APPROVE", "REJECT")]
    reviewed = [r for r in rows if r["verdict"] == "REVIEW"]
    agree = [r for r in rows if r["verdict"] == r["expected"]]
    by_llm = [r for r in rows if r["source"] == "llm"]

    by_type: dict[str, dict] = {}
    for r in viol:
        t = r["violation_type"] or "UNKNOWN"
        d = by_type.setdefault(t, {"total": 0, "caught": 0})
        d["total"] += 1
        if r["verdict"] == "REJECT":
            d["caught"] += 1

    return {
        "total": total,
        "violation_total": len(viol),
        "normal_total": len(norm),
        "gray_total": len(gray),
        "recall_pct": _pct(len(caught), len(viol)),
        "false_reject_pct": _pct(len(killed), len(norm)),
        "auto_rate_pct": _pct(len(auto), total),
        "review_rate_pct": _pct(len(reviewed), total),
        "agreement_pct": _pct(len(agree), total),
        "missed_count": len(missed),
        "false_reject_count": len(killed),
        "llm_calls": len(by_llm),
        "recall_by_type": {
            t: {"total": d["total"], "caught": d["caught"], "recall_pct": _pct(d["caught"], d["total"])}
            for t, d in sorted(by_type.items())
        },
        "false_reject_items": [
            {"item_id": r["item_id"], "title": r["title"]} for r in killed[:10]
        ],
        "missed_items": [
            {"item_id": r["item_id"], "title": r["title"],
             "violation_type": r["violation_type"], "verdict": r["verdict"]}
            for r in missed[:10]
        ],
    }


def save_run(tag: str, metrics: dict) -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO eval_runs (tag, metrics) VALUES (%s, %s)",
                        (tag, json.dumps(metrics, ensure_ascii=False)))
            run_id = cur.lastrowid
        conn.commit()
        return run_id
    finally:
        conn.close()


def list_runs(limit: int = 20) -> list:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, tag, metrics, started_at FROM eval_runs ORDER BY id DESC LIMIT %s",
                        (limit,))
            rows = cur.fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        m = r["metrics"]
        if isinstance(m, str):
            m = json.loads(m)
        out.append({"id": r["id"], "tag": r["tag"], "started_at": str(r["started_at"]), **m})
    return out


def item_detail(item_id: int) -> dict:
    """一件商品的完整画像：业务信息 + 审核记录 + 最新任务 + 轨迹 + 复核状态 + 标注。"""
    conn = connect()
    result: dict = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT i.*, c.name AS category_name, u.nickname AS seller, u.credit_score
                    FROM `{BIZ_DB}`.item i
                    LEFT JOIN `{BIZ_DB}`.item_category c ON c.id = i.category_id
                    LEFT JOIN `{BIZ_DB}`.sys_user u ON u.id = i.seller_id
                    WHERE i.id = %s""",
                (item_id,),
            )
            item = cur.fetchone()
            if not item:
                return {}
            item["price"] = float(item["price"])
            item["original_price"] = float(item["original_price"]) if item["original_price"] is not None else None
            result["item"] = item

            cur.execute(
                f"""SELECT audit_source, audit_result, audit_remark, confidence, audit_time
                    FROM `{BIZ_DB}`.item_audit WHERE item_id = %s ORDER BY id DESC LIMIT 5""",
                (item_id,),
            )
            result["audits"] = [
                {**r, "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                 "audit_time": str(r["audit_time"])}
                for r in cur.fetchall()
            ]

            cur.execute(
                """SELECT id, status, verdict, confidence, reason, source, rule_hits, error, created_at
                   FROM audit_jobs WHERE item_id = %s ORDER BY id DESC LIMIT 1""",
                (item_id,),
            )
            job = cur.fetchone()
            if job:
                hits = job.get("rule_hits")
                if isinstance(hits, str):
                    hits = json.loads(hits or "[]")
                job["rule_hits"] = hits
                job["confidence"] = float(job["confidence"]) if job["confidence"] is not None else None
                job["created_at"] = str(job["created_at"])
                cur.execute(
                    """SELECT step_no, kind, payload, latency_ms FROM audit_steps
                       WHERE job_id = %s ORDER BY step_no""",
                    (job["id"],),
                )
                result["steps"] = [
                    {**s, "payload": json.loads(s["payload"]) if isinstance(s["payload"], str) else s["payload"]}
                    for s in cur.fetchall()
                ]
            result["job"] = job

            cur.execute(
                """SELECT reason, confidence, status, decision, remark, decided_at
                   FROM review_queue WHERE item_id = %s ORDER BY id DESC LIMIT 1""",
                (item_id,),
            )
            rq = cur.fetchone()
            result["review"] = (
                {**rq, "confidence": float(rq["confidence"]) if rq["confidence"] is not None else None,
                 "decided_at": str(rq["decided_at"]) if rq["decided_at"] else None}
                if rq else None
            )

            cur.execute("SELECT expected, violation_type FROM audit_ground_truth WHERE item_id = %s", (item_id,))
            result["ground_truth"] = cur.fetchone()
    finally:
        conn.close()
    return result


def progress() -> dict:
    """页面顶部的进度：还有多少没审、审核结果分布、人工复核结果。"""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT status, COUNT(*) AS n FROM `{BIZ_DB}`.item WHERE deleted = 0 GROUP BY status")
            items = {str(r["status"]): r["n"] for r in cur.fetchall()}
            cur.execute("SELECT status, COUNT(*) AS n FROM audit_jobs GROUP BY status")
            jobs = {r["status"]: r["n"] for r in cur.fetchall()}
            cur.execute("SELECT verdict, COUNT(*) AS n FROM audit_jobs WHERE verdict IS NOT NULL GROUP BY verdict")
            verdicts = {r["verdict"]: r["n"] for r in cur.fetchall()}
            cur.execute(
                """SELECT SUM(status = 0) AS pending,
                          SUM(status = 1 AND decision = 1) AS approved,
                          SUM(status = 1 AND decision = 2) AS rejected,
                          COUNT(*) AS total
                   FROM review_queue"""
            )
            rs = cur.fetchone() or {}
    finally:
        conn.close()
    return {
        "items": items,
        "jobs": jobs,
        "verdicts": verdicts,
        "pending_review": int(rs.get("pending") or 0),
        "review": {
            "total": int(rs.get("total") or 0),
            "pending": int(rs.get("pending") or 0),
            "approved": int(rs.get("approved") or 0),
            "rejected": int(rs.get("rejected") or 0),
        },
    }


# ============================================================
# 延迟分位数
# ============================================================

def _pctl(xs: list[int], p: float) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    idx = min(len(s) - 1, max(0, int(round(p / 100 * len(s) + 0.5)) - 1))
    return int(s[idx])


def latency_stats(sample: int = 3000, minutes: int = 60) -> dict:
    """从最近完成的任务算延迟分位数，并拆分到各阶段。

    MySQL 8 没有 `percentile_cont`，样本量也就几千行，
    直接取出来在 Python 里排个序更简单，也够快。
    """
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT duration_ms FROM audit_jobs
                   WHERE status = 'succeeded' AND duration_ms IS NOT NULL
                     AND updated_at > NOW() - INTERVAL %s MINUTE
                   ORDER BY id DESC LIMIT %s""",
                (minutes, sample),
            )
            job_ms = [int(r["duration_ms"]) for r in cur.fetchall()]

            cur.execute(
                """SELECT s.kind, s.latency_ms
                   FROM audit_steps s
                   JOIN audit_jobs j ON j.id = s.job_id
                   WHERE j.status = 'succeeded' AND s.latency_ms IS NOT NULL
                     AND j.updated_at > NOW() - INTERVAL %s MINUTE
                   ORDER BY s.id DESC LIMIT %s""",
                (minutes, sample * 5),
            )
            stages: dict[str, list[int]] = {}
            for r in cur.fetchall():
                stages.setdefault(r["kind"], []).append(int(r["latency_ms"]))
    finally:
        conn.close()

    def stat(xs: list[int]) -> dict:
        return {
            "count": len(xs),
            "avg_ms": int(sum(xs) / len(xs)) if xs else 0,
            "p50_ms": _pctl(xs, 50),
            "p95_ms": _pctl(xs, 95),
            "p99_ms": _pctl(xs, 99),
            "max_ms": max(xs) if xs else 0,
        }

    return {
        "window_minutes": minutes,
        "job": stat(job_ms),
        "stages": {k: stat(v) for k, v in sorted(stages.items())},
    }


# ============================================================
# Agent 行为统计
# ============================================================

def agent_stats(minutes: int = 60) -> dict:
    """模型到底有没有在做工具取舍 —— 这是量化证据。

    没有调查预算时，模型会对每个商品把工具全调一遍（实测 31/37 个任务的
    工具组合完全相同），那就不是决策而是固定流程。这几个指标能量出来差别：
      - `avg_tool_calls`    平均每任务调用几次工具
      - `zero_tool_jobs`    一眼可判、一次工具都没调的任务数
      - `distribution`      调用次数的直方图（离散度越大越像在做取舍）
      - `budget_denied`     预算用完后仍试图调工具的次数据
    """
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.job_id,
                          SUM(s.kind = 'tool_call')     AS calls,
                          SUM(s.kind = 'budget_denied') AS denied,
                          SUM(s.kind = 'llm_result')    AS llm_steps
                   FROM audit_steps s
                   JOIN audit_jobs j ON j.id = s.job_id
                   WHERE j.status = 'succeeded'
                     AND j.updated_at > NOW() - INTERVAL %s MINUTE
                   GROUP BY s.job_id
                   HAVING llm_steps > 0""",
                (minutes,),
            )
            rows = cur.fetchall()

            cur.execute(
                """SELECT JSON_UNQUOTE(JSON_EXTRACT(s.payload, '$.tool')) AS tool,
                          COUNT(*) AS n
                   FROM audit_steps s
                   JOIN audit_jobs j ON j.id = s.job_id
                   WHERE s.kind = 'tool_call' AND j.status = 'succeeded'
                     AND j.updated_at > NOW() - INTERVAL %s MINUTE
                   GROUP BY tool ORDER BY n DESC""",
                (minutes,),
            )
            tools = {r["tool"]: int(r["n"]) for r in cur.fetchall() if r["tool"]}
    finally:
        conn.close()

    calls = [int(r["calls"] or 0) for r in rows]
    denied = sum(int(r["denied"] or 0) for r in rows)
    dist: dict[str, int] = {}
    for c in calls:
        dist[str(c)] = dist.get(str(c), 0) + 1

    # ⚠ 区分两种口径：`submit_verdict` 是**终止调用**，不是调查行为。
    # 把它算进"工具调用次数"会稀释真正的节省幅度。
    submit = tools.get("submit_verdict", 0)
    query_calls = max(0, sum(tools.values()) - submit)
    jobs = len(calls)

    return {
        "window_minutes": minutes,
        "llm_jobs": jobs,
        "total_tool_calls": sum(calls),
        "avg_tool_calls": round(sum(calls) / jobs, 2) if jobs else 0.0,
        # 只算真正的调查工具调用 —— 这个才是"模型查了几次"
        "query_calls": query_calls,
        "avg_query_calls": round(query_calls / jobs, 2) if jobs else 0.0,
        "max_tool_calls": max(calls) if calls else 0,
        "zero_tool_jobs": sum(1 for c in calls if c == 0),
        "no_query_jobs": sum(1 for c in calls if c <= 1),   # 只调了 submit 就下结论
        "budget_denied": denied,
        "distribution": dict(sorted(dist.items(), key=lambda kv: int(kv[0]))),
        "tools": tools,
    }

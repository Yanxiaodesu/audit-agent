"""审核编排 —— 把「规则层 → 模型层 → 分流 → 写回」串起来。

worker 只是它的驱动器，这里才是核心逻辑。

决策表：
    规则命中 BLOCK          -> REJECT（source=rule）
    规则命中 REVIEW（含注入）-> 有 LLM 就交给 LLM，否则转人工
    规则全过
        信用分 >= 阈值 且
        无未处理举报 且
        账号正常            -> APPROVE（source=rule）
        否则                -> REVIEW（原因写清楚）
"""
from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal

from app.config import AUTO_APPROVE_MIN_CREDIT, LLM_ENABLED, REVIEW_THRESHOLD
from app.db import biz, meta
from app.rules import evaluate
from agent import loop

APPROVE, REJECT, REVIEW = "APPROVE", "REJECT", "REVIEW"


def _jsonable(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def fingerprint(item: dict) -> str:
    """内容指纹。

    判定依据只取决于这几项 —— 它们没变就不需要重新审核。
    用于幂等：同一份内容重复投递时直接复用已有任务，不重复烧钱。
    """
    raw = "|".join([
        str(item.get("title") or ""),
        str(item.get("description") or ""),
        str(item.get("price") or ""),
        str(item.get("original_price") or ""),
        str(item.get("category_id") or ""),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Tracker:
    """把每一步写进 audit_steps，并在每步之后落 checkpoint。

    ⚠ 一个任务**只能有一个** Tracker 实例。
    早期版本在 worker 里给 writeback 又新建了一个 Tracker，步号从 1 重来，
    结果 `writeback` 步从未入库，而第 1 步（load）的 payload 被静默覆盖。
    现在 Tracker 由调用方创建并一路传递，`record()` 也会同步更新 kind 做兜底。
    """

    def __init__(self, job_id: int, step: int = 0):
        self.job_id = job_id
        self.step = step
        self.state: dict = {}

    def record(self, kind: str, payload: dict, latency_ms: int = 0, tokens: int | None = None):
        self.step += 1
        payload = _jsonable(payload)
        self.state = {**self.state, "last_kind": kind, "step_no": self.step}
        with meta(commit=True) as cur:
            cur.execute(
                """INSERT INTO audit_steps (job_id, step_no, kind, payload, latency_ms, tokens)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE kind = VALUES(kind),
                                           payload = VALUES(payload),
                                           latency_ms = VALUES(latency_ms),
                                           tokens = VALUES(tokens)""",
                (self.job_id, self.step, kind, json.dumps(payload, ensure_ascii=False),
                 latency_ms, tokens),
            )
            cur.execute(
                """INSERT INTO checkpoints (job_id, step_no, state) VALUES (%s, %s, %s)
                   ON DUPLICATE KEY UPDATE state = VALUES(state)""",
                (self.job_id, self.step, json.dumps(_jsonable(self.state), ensure_ascii=False)),
            )
        return self.step


# ============================================================
# 数据装载
# ============================================================

class ItemNotAuditable(Exception):
    """商品不存在、已被删除，或处于不可审核状态。

    关键区别：这**不是瞬时故障**。
    任务失败机制是为"数据库抖了一下""模型超时了"这类可恢复问题设计的，
    而商品被删了是**确定性的终态** —— 重试 3 次只会白烧三次数据库往返，
    最后还会在指标里留下一条 failed，污染"系统是否健康"的判断。

    所以调用方应该把任务标成 `cancelled` 而不是 `failed`，并且不重试。
    """


def load_context(item_id: int) -> dict:
    with biz() as cur:
        cur.execute(
            """SELECT i.*, c.name AS category_name
               FROM item i LEFT JOIN item_category c ON c.id = i.category_id
               WHERE i.id = %s AND i.deleted = 0""",
            (item_id,),
        )
        item = cur.fetchone()
        if not item:
            raise ItemNotAuditable(f"商品 {item_id} 不存在或已被删除")
        cur.execute("SELECT * FROM sys_user WHERE id = %s", (item["seller_id"],))
        seller = cur.fetchone() or {}
        cur.execute(
            "SELECT COUNT(*) AS n FROM report WHERE item_id = %s AND status = 0 AND deleted = 0",
            (item_id,),
        )
        pending_reports = int(cur.fetchone()["n"])
    return {"item": item, "seller": seller, "pending_reports": pending_reports}


# ============================================================
# 主流程
# ============================================================

def run(item_id: int, job_id: int, tracker: Tracker | None = None) -> tuple[dict, Tracker]:
    """对一件商品做审核，返回 (结论, tracker)。

    tracker 必须由调用方传进来并复用给 writeback —— 否则步号会冲突。
    """
    tk = tracker if tracker is not None else Tracker(job_id)

    # ---- 第 1 步：装载上下文 ----
    t0 = time.perf_counter()
    ctx = load_context(item_id)
    item, seller = ctx["item"], ctx["seller"]
    tk.record("load", {
        "item_id": item_id,
        "title": item["title"],
        "description": (item.get("description") or "")[:200],
        "category": item.get("category_name"),
        "price": float(item["price"]),
        "original_price": float(item["original_price"]) if item.get("original_price") is not None else None,
        "seller_id": item["seller_id"],
        "seller_credit": seller.get("credit_score"),
        "pending_reports": ctx["pending_reports"],
        "fingerprint": fingerprint(item),
    }, int((time.perf_counter() - t0) * 1000))

    # ---- 第 2 步：规则引擎 ----
    t0 = time.perf_counter()
    rr = evaluate(item, item.get("category_name") or "", seller, ctx["pending_reports"])
    tk.record("rule", {
        "blocked": rr.blocked,
        "needs_second_layer": rr.needs_second_layer,
        "hits": [h.as_dict() for h in rr.hits],
        "signals": rr.signals,
    }, int((time.perf_counter() - t0) * 1000))

    base = {
        "item_id": item_id,
        "rule_hits": [h.as_dict() for h in rr.hits],
        "signals": rr.signals,
        "fingerprint": fingerprint(item),
    }

    # ---- 分支 A：规则直接驳回 ----
    if rr.blocked:
        detail = "；".join(h.detail for h in rr.hits if h.severity == "BLOCK")
        reason = f"规则引擎判定违规：{detail}"
        tk.record("verdict", {"verdict": REJECT, "source": "rule", "reason": reason})
        return {**base, "verdict": REJECT, "source": "rule", "confidence": 0.98,
                "reason": reason}, tk

    # ---- 分支 B：需要第二层 ----
    if rr.needs_second_layer:
        if LLM_ENABLED:
            t0 = time.perf_counter()
            llm_out = loop.run(item, seller, rr, ctx["pending_reports"], tracker=tk)
            tk.record("llm_result", {k: v for k, v in llm_out.items() if k != "messages"},
                      int((time.perf_counter() - t0) * 1000), tokens=llm_out.get("tokens"))

            # 护栏触发导致的降级：模型根本没参与判断，
            # 所以来源记 rule（而不是 llm），商品照常进人工复核队列。
            if llm_out.get("degraded"):
                reason = llm_out.get("reason") or "LLM 护栏触发，降级人工复核"
                tk.record("verdict", {"verdict": REVIEW, "source": "rule",
                                      "reason": reason, "degraded": True})
                return {**base, "verdict": REVIEW, "source": "rule", "confidence": 0.5,
                        "reason": reason, "degraded": True}, tk

            # loop.run() 已经收口过一次（归一化枚举 + 夹置信度），这里再兜一次：
            # 任何直接调用 run() 的脚本/测试都可能塞进越界值，而
            # confidence 列是 DECIMAL(4,3)，炸了会让整个任务失败并触发烧钱重试。
            verdict = loop.clean_verdict(llm_out.get("verdict"))
            conf = loop.clean_confidence(llm_out.get("confidence"))
            if verdict == REVIEW or conf < REVIEW_THRESHOLD:
                reason = llm_out.get("reason") or "模型置信度不足，转人工复核"
                tk.record("verdict", {"verdict": REVIEW, "source": "llm",
                                      "reason": reason, "confidence": conf})
                return {**base, "verdict": REVIEW, "source": "llm",
                        "confidence": conf, "reason": reason}, tk
            tk.record("verdict", {"verdict": verdict, "source": "llm",
                                  "reason": llm_out.get("reason"), "confidence": conf})
            return {**base, "verdict": verdict, "source": "llm", "confidence": conf,
                    "reason": llm_out.get("reason") or ""}, tk

        reason = "命中需人工判断的规则：" + "；".join(h.detail for h in rr.hits)
        reason += "（未配置 LLM，转人工）"
        tk.record("verdict", {"verdict": REVIEW, "source": "rule", "reason": reason})
        return {**base, "verdict": REVIEW, "source": "rule", "confidence": 0.5,
                "reason": reason}, tk

    # ---- 分支 C：规则全过，按信用与举报决定是否自动通过 ----
    credit = int(seller.get("credit_score") or 100)
    blocked_by_credit = credit < AUTO_APPROVE_MIN_CREDIT
    blocked_by_report = ctx["pending_reports"] > 0
    blocked_by_status = int(seller.get("status") or 1) != 1

    if not (blocked_by_credit or blocked_by_report or blocked_by_status):
        reason = f"规则无命中，卖家信用 {credit} ≥ {AUTO_APPROVE_MIN_CREDIT}，无未处理举报，自动通过"
        tk.record("verdict", {"verdict": APPROVE, "source": "rule",
                              "reason": reason, "confidence": 0.9})
        return {**base, "verdict": APPROVE, "source": "rule", "confidence": 0.9,
                "reason": reason}, tk

    why = []
    if blocked_by_credit:
        why.append(f"卖家信用 {credit} < {AUTO_APPROVE_MIN_CREDIT}")
    if blocked_by_report:
        why.append(f"存在 {ctx['pending_reports']} 条未处理举报")
    if blocked_by_status:
        why.append("卖家账号异常")
    reason = "规则无命中，但" + "、".join(why) + "，转人工复核"
    tk.record("verdict", {"verdict": REVIEW, "source": "rule",
                          "reason": reason, "confidence": 0.6})
    return {**base, "verdict": REVIEW, "source": "rule", "confidence": 0.6,
            "reason": reason}, tk


# ============================================================
# 写回
# ============================================================

def _safe_conf(result: dict) -> Decimal:
    """落库前最后一道置信度收口。

    上游 `loop.clean_confidence()` 已经夹过一次，这里再来一次是因为：
    `confidence` 列是 **DECIMAL(4,3)**（上限 9.999），
    而 `writeback` 除了 LLM，还可能被人工复核、测试、脚本直接调用 ——
    任何一条路径塞进 10 或 "high" 都会让整个事务抛 DataError/ValueError，
    而异常类型完全看不出「是模型输出格式的问题」。

    所以不信任调用方，在这里兜住。
    """
    try:
        v = float(result.get("confidence") or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v != v:                       # NaN
        v = 0.0
    if v > 1.0:
        v = v / 100.0 if v >= 2.0 else 1.0
    return Decimal(str(round(max(0.0, min(1.0, v)), 3)))


def writeback(item_id: int, job_id: int, result: dict, tracker: Tracker) -> dict:
    """把结论写回业务库。

    REJECT  -> item.status = 5（审核驳回） + item_audit(audit_result=2)
    APPROVE -> item.status = 1（在售）     + item_audit(audit_result=1)
    REVIEW  -> item.status 保持 0，进人工复核队列，不写 item_audit

    tracker 必须与 run() 用的是同一个，否则步号会撞车（见 Tracker 的注释）。
    """
    verdict = result["verdict"]
    t0 = time.perf_counter()
    written = {}

    if verdict in (APPROVE, REJECT):
        new_status = 1 if verdict == APPROVE else 5
        audit_result = 1 if verdict == APPROVE else 2
        with biz(commit=True) as cur:
            # 条件更新：只有还处于「待审核」才写，避免覆盖人工已裁决的结果
            cur.execute(
                "UPDATE item SET status = %s, version = version + 1 WHERE id = %s AND status = 0",
                (new_status, item_id),
            )
            affected = cur.rowcount
            if affected:
                cur.execute(
                    """INSERT INTO item_audit
                       (item_id, audit_user_id, audit_source, audit_result, audit_remark,
                        rule_hits, confidence, submit_time, audit_time)
                       VALUES (%s, NULL, 2, %s, %s, %s, %s, NOW(), NOW())""",
                    (item_id, audit_result, result.get("reason") or "",
                     json.dumps(result.get("rule_hits") or [], ensure_ascii=False),
                     _safe_conf(result)),
                )
            else:
                # 商品已被人工裁决或已售出，本次结论作废，不覆盖
                pass
        written = {"item_status": new_status, "audit_result": audit_result,
                   "rows_affected": affected,
                   "skipped": "商品状态已变更，未覆盖" if not affected else None}
    else:
        with meta(commit=True) as cur:
            # 单条语句完成「有则更新、无则插入」，把「一个商品最多一条待复核」
            # 这件事交给**数据库**保证（review_queue 上有生成列唯一键 uk_pending_item）。
            #
            # 原来的写法是「先 SELECT 再判断 INSERT/UPDATE」——
            # 两个 worker 线程同时审同一商品时，两个 SELECT 都会在对方 INSERT 之前
            # 返回空，于是插出两行待复核。串行测试永远抓不到这个竞态。
            cur.execute(
                """INSERT INTO review_queue (job_id, item_id, reason, confidence)
                   VALUES (%s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                       job_id = VALUES(job_id),
                       reason = VALUES(reason),
                       confidence = VALUES(confidence)""",
                (job_id, item_id, (result.get("reason") or "")[:500], _safe_conf(result)),
            )
            # ON DUPLICATE KEY UPDATE 命中时 lastrowid 不可靠，回查一次拿真 id
            cur.execute(
                "SELECT id FROM review_queue WHERE item_id = %s AND status = 0 LIMIT 1",
                (item_id,),
            )
            row = cur.fetchone()
            qid = int(row["id"]) if row else None
            written = {"review_queue_id": qid, "reused": bool(cur.rowcount == 2)}

    tracker.record("writeback", {"verdict": verdict, **written},
                   int((time.perf_counter() - t0) * 1000))
    return written


# 兼容旧调用名
audit = run

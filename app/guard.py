"""LLM 调用的限流与成本护栏。

## 为什么放在数据库里

限流必须是**全局**的：一个进程内 8 个线程、以及多个 worker 进程，
加起来都不能超过模型服务允许的速率 —— 放进程内存里做不到跨进程一致。
项目已经有 MySQL，用单行表 + `SELECT ... FOR UPDATE` 就够，不用再引入 Redis。

## 三道护栏

1. **速率限制（令牌桶）** —— 按分钟补充，突发受桶容量限制
2. **日预算** —— 当日累计花费超过上限就不再调模型
3. **日调用次数** —— 兜底，防止 token 单价估错导致账单失控

关于「硬」和「软」：调用次数在**取令牌时就预扣**，所以是**硬上限**；
而预算是**软约束** —— 花费要等调用返回才知道，并发 N 路时最多超出
`N × 单次最大成本`。要更严就把 `LLM_DAILY_BUDGET` 留出安全余量。

## 触发护栏时降级，而不是把任务判失败

这是最关键的设计决定。拿不到令牌时如果把任务标成 `failed`，
就等于把「暂时不能调模型」变成了「用户的商品审核失败了」—— 明显是错的。

正确做法是**降级成只走规则**，商品进人工复核队列，
并且把降级次数记下来（`day_degraded`），这样监控能发现「护栏在频繁触发」。
"""
from __future__ import annotations

import time

from app.config import (LLM_DAILY_BUDGET, LLM_DAILY_CALL_LIMIT,
                        LLM_GUARD_ENABLED, LLM_MODE, LLM_PRICE_CURRENCY,
                        LLM_PRICE_INPUT_PER_M, LLM_PRICE_OUTPUT_PER_M,
                        LLM_RATE_BURST, LLM_RATE_PER_MIN, LLM_RATE_WAIT_MAX)
from app.db import meta_conn

GUARD_ROW = 1


def guard_enabled() -> bool:
    """护栏是否生效。

    **mock 模式自动跳过**：它不做真实调用，没什么要保护的；
    而且如果照常限流，并发压测会被限速扭曲成 60 次/分钟，完全测不出真实并发能力。
    """
    return LLM_GUARD_ENABLED and LLM_MODE != "mock"


# ============================================================
# 内部工具
# ============================================================

def _ensure_row(cur) -> bool:
    """确保护栏那一行存在。返回 True 表示刚插入，调用方需要 commit。

    ⚠ 必须在取 `SELECT ... FOR UPDATE` **之前**、并且**单独提交**。

    原来的写法是把这个 `INSERT IGNORE` 塞进同一个事务，结果并发时死锁：
    `INSERT IGNORE` 遇到重复主键会对已存在的行加**共享锁**，
    紧接着的 `FOR UPDATE` 要把它升级成**排他锁** ——
    两个事务同时走这条路就是经典的 S→X 锁升级死锁。
    （这个 bug 是并发测试 `test_并发下调用的硬上限不会被突破` 抓出来的。）
    """
    cur.execute("SELECT id FROM llm_guard WHERE id = %s", (GUARD_ROW,))
    if cur.fetchone() is not None:
        return False
    cur.execute(
        """INSERT IGNORE INTO llm_guard (id, tokens, refilled_at_ms, day)
           VALUES (%s, %s, %s, CURDATE())""",
        (GUARD_ROW, LLM_RATE_BURST, int(time.time() * 1000)),
    )
    return True


def _decimal(v) -> float:
    return float(v) if v is not None else 0.0


# ============================================================
# 令牌桶 + 预算（一个事务内完成）
# ============================================================

def _try_acquire() -> tuple[bool, str, float]:
    """跨天归档 -> 补充令牌 -> 取令牌。

    返回 `(是否拿到, 说明, 建议等待秒数)`。
    建议等待为 0 表示「等下去也不会变好」（预算/次数类拒绝）。
    """
    conn = meta_conn()
    try:
        with conn.cursor() as cur:
            if _ensure_row(cur):
                conn.commit()          # 先提交，避免和下面的 FOR UPDATE 形成锁升级死锁
            cur.execute(
                """SELECT tokens, refilled_at_ms, day, day_calls, day_cost
                   FROM llm_guard WHERE id = %s FOR UPDATE""",
                (GUARD_ROW,),
            )
            row = cur.fetchone()
            now_ms = int(time.time() * 1000)

            # ---- 跨天：归档昨天用量并重置当日计数 ----
            cur.execute("SELECT CURDATE() AS today")
            today = cur.fetchone()["today"]
            if row["day"] != today:
                cur.execute(
                    """INSERT INTO llm_usage_daily
                           (day, calls, prompt_tokens, output_tokens, cost, degraded)
                       SELECT day, day_calls, day_prompt_tokens, day_output_tokens,
                              day_cost, day_degraded
                       FROM llm_guard WHERE id = %s
                       ON DUPLICATE KEY UPDATE
                           calls = VALUES(calls), prompt_tokens = VALUES(prompt_tokens),
                           output_tokens = VALUES(output_tokens), cost = VALUES(cost),
                           degraded = VALUES(degraded)""",
                    (GUARD_ROW,),
                )
                cur.execute(
                    """UPDATE llm_guard
                       SET day = %s, day_calls = 0, day_prompt_tokens = 0,
                           day_output_tokens = 0, day_cost = 0, day_degraded = 0
                       WHERE id = %s""",
                    (today, GUARD_ROW),
                )
                row["day"] = today
                row["day_calls"] = 0
                row["day_cost"] = 0

            # ---- 护栏 2/3：日预算与日调用次数 ----
            if LLM_DAILY_CALL_LIMIT > 0 and int(row["day_calls"]) >= LLM_DAILY_CALL_LIMIT:
                conn.commit()
                return False, f"已达日调用上限 {LLM_DAILY_CALL_LIMIT} 次", 0.0
            if LLM_DAILY_BUDGET > 0 and _decimal(row["day_cost"]) >= LLM_DAILY_BUDGET:
                conn.commit()
                return False, (f"已达日预算 {LLM_DAILY_BUDGET:g} {LLM_PRICE_CURRENCY}"
                               f"（已花 {_decimal(row['day_cost']):.4f}）"), 0.0

            # ---- 护栏 1：令牌桶 ----
            if LLM_RATE_PER_MIN <= 0:
                cur.execute("UPDATE llm_guard SET refilled_at_ms = %s WHERE id = %s",
                            (now_ms, GUARD_ROW))
                conn.commit()
                return True, "未启用限速", 0.0

            rate_per_ms = LLM_RATE_PER_MIN / 60000.0        # 每毫秒补充多少令牌
            elapsed = max(0, now_ms - int(row["refilled_at_ms"]))
            tokens = min(float(LLM_RATE_BURST), _decimal(row["tokens"]) + elapsed * rate_per_ms)

            if tokens >= 1.0:
                # 取到令牌的同时**预扣**一次调用额度。
                # 如果改成「调用完成后再 +1」，并发下 8 个线程会一起通过检查，
                # 上限就变成软约束（实测设 10 会跑到 17）。预扣之后是硬上限。
                cur.execute(
                    """UPDATE llm_guard
                       SET tokens = %s, refilled_at_ms = %s, day_calls = day_calls + 1
                       WHERE id = %s""",
                    (tokens - 1.0, now_ms, GUARD_ROW),
                )
                conn.commit()
                return True, "ok", 0.0

            # 令牌不够：把补到的部分存回去，算出还要等多久
            wait_s = (1.0 - tokens) / rate_per_ms / 1000.0
            cur.execute(
                "UPDATE llm_guard SET tokens = %s, refilled_at_ms = %s WHERE id = %s",
                (tokens, now_ms, GUARD_ROW),
            )
            conn.commit()
            return False, f"触发速率限制（{LLM_RATE_PER_MIN:g} 次/分钟）", max(0.02, wait_s)
    except Exception:
        conn.rollback()
        raise


def _bump_degraded():
    conn = meta_conn()
    try:
        with conn.cursor() as cur:
            if _ensure_row(cur):
                conn.commit()
            cur.execute("UPDATE llm_guard SET day_degraded = day_degraded + 1 WHERE id = %s",
                        (GUARD_ROW,))
        conn.commit()
    except Exception:  # noqa: BLE001 —— 计数失败不该影响主流程
        conn.rollback()


def acquire(wait_max_s: float | None = None) -> tuple[bool, str]:
    """取一个调用令牌。

    取不到会等到 `wait_max_s`（默认来自配置）；仍取不到就返回 False，
    调用方应当**降级**而不是报错。

    对「预算/次数」类拒绝不会等待 —— 等下去也不会变好。
    """
    if not guard_enabled():
        return True, "护栏未启用"

    if wait_max_s is None:
        wait_max_s = LLM_RATE_WAIT_MAX
    deadline = time.monotonic() + max(0.0, wait_max_s)
    last_why = "未知原因"

    while True:
        ok, why, wait_s = _try_acquire()
        if ok:
            return True, why
        last_why = why

        if wait_s <= 0:
            # 预算/次数类拒绝，等待无意义
            _bump_degraded()
            return False, why

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _bump_degraded()
            return False, f"{why}，等待 {wait_max_s:.0f}s 仍无令牌"
        time.sleep(min(wait_s, remaining, 0.5))


# ============================================================
# 记账
# ============================================================

def record_usage(usage: dict | None) -> dict:
    """记一次调用的 token 与花费。

    `usage` 是 OpenAI 兼容格式：{prompt_tokens, completion_tokens, total_tokens}
    """
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    cost = (prompt * LLM_PRICE_INPUT_PER_M + completion * LLM_PRICE_OUTPUT_PER_M) / 1_000_000

    if not guard_enabled():
        return {"prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": total, "cost": round(cost, 8), "recorded": False}

    conn = meta_conn()
    try:
        with conn.cursor() as cur:
            if _ensure_row(cur):
                conn.commit()
            # 注意：**不在这里加 day_calls** —— 调用次数已经在 acquire() 取令牌时预扣了。
            # 这里只累加真实的 token 与花费。
            cur.execute(
                """UPDATE llm_guard
                   SET day_prompt_tokens = day_prompt_tokens + %s,
                       day_output_tokens = day_output_tokens + %s,
                       day_cost = day_cost + %s
                   WHERE id = %s""",
                (prompt, completion, cost, GUARD_ROW),
            )
        conn.commit()
    except Exception:  # noqa: BLE001 —— 记账失败不该让审核失败
        conn.rollback()

    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total, "cost": round(cost, 8)}


# ============================================================
# 状态快照
# ============================================================

def snapshot() -> dict:
    """当前护栏状态。给 /metrics 和页面用。"""
    conn = meta_conn()
    try:
        with conn.cursor() as cur:
            if _ensure_row(cur):
                conn.commit()
            cur.execute(
                """SELECT tokens, refilled_at_ms, day, day_calls, day_prompt_tokens,
                          day_output_tokens, day_cost, day_degraded
                   FROM llm_guard WHERE id = %s""",
                (GUARD_ROW,),
            )
            row = cur.fetchone() or {}
    finally:
        pass

    now_ms = int(time.time() * 1000)
    tokens = _decimal(row.get("tokens"))
    if LLM_RATE_PER_MIN > 0 and row.get("refilled_at_ms"):
        elapsed = max(0, now_ms - int(row["refilled_at_ms"]))
        tokens = min(float(LLM_RATE_BURST), tokens + elapsed * (LLM_RATE_PER_MIN / 60000.0))

    spent = _decimal(row.get("day_cost"))
    calls = int(row.get("day_calls") or 0)
    return {
        "day": str(row.get("day") or ""),
        "rate_per_min": LLM_RATE_PER_MIN,
        "tokens_available": round(tokens, 2),
        "burst": LLM_RATE_BURST,
        "calls_today": calls,
        "call_limit": LLM_DAILY_CALL_LIMIT,
        "prompt_tokens_today": int(row.get("day_prompt_tokens") or 0),
        "output_tokens_today": int(row.get("day_output_tokens") or 0),
        "cost_today": round(spent, 6),
        "budget": LLM_DAILY_BUDGET,
        "budget_used_pct": round(100.0 * spent / LLM_DAILY_BUDGET, 1) if LLM_DAILY_BUDGET > 0 else 0.0,
        "degraded_today": int(row.get("day_degraded") or 0),
        "currency": LLM_PRICE_CURRENCY,
        "avg_cost_per_call": round(spent / calls, 6) if calls else 0.0,
        "price_input_per_m": LLM_PRICE_INPUT_PER_M,
        "price_output_per_m": LLM_PRICE_OUTPUT_PER_M,
    }


def daily_history(limit: int = 14) -> list:
    """每日用量历史，用来看成本趋势。"""
    conn = meta_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT day, calls, prompt_tokens, output_tokens, cost, degraded
                   FROM llm_usage_daily ORDER BY day DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
    finally:
        pass
    for r in rows:
        r["day"] = str(r["day"])
        r["cost"] = round(float(r["cost"] or 0), 6)
    return rows

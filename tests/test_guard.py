"""LLM 限流与成本护栏的测试。

护栏的核心承诺有两条，测试就围绕它们：
  1. 触发护栏时返回「拒绝」，让调用方**降级**，而不是抛异常
  2. 记账数字正确（token 与花费能对得上）

注意：这些用例直接操作 `llm_guard` 那一行，所以用 `_reset()` 把状态摆成确定值，
避免和正在运行的 worker 抢令牌导致 flaky。
"""
from __future__ import annotations

import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.guard as guard  # noqa: E402
from app.db import meta_conn  # noqa: E402


def _reset(tokens: float = 100.0, calls: int = 0, cost: float = 0.0,
           degraded: int = 0, day: str | None = None):
    conn = meta_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM llm_guard")
        if day:
            cur.execute(
                """INSERT INTO llm_guard (id, tokens, refilled_at_ms, day, day_calls, day_cost, day_degraded)
                   VALUES (1, %s, %s, %s, %s, %s, %s)""",
                (tokens, int(time.time() * 1000), day, calls, cost, degraded),
            )
        else:
            cur.execute(
                """INSERT INTO llm_guard (id, tokens, refilled_at_ms, day, day_calls, day_cost, day_degraded)
                   VALUES (1, %s, %s, CURDATE(), %s, %s, %s)""",
                (tokens, int(time.time() * 1000), calls, cost, degraded),
            )
    conn.commit()


def _snapshot_row():
    """把护栏那一行整个备份下来，测试结束后原样恢复。

    测试需要把护栏摆成确定状态，但如果直接删掉重建，
    **跑一遍测试就会把线上的当日用量清空** —— 这属于测试有了破坏性副作用。
    """
    conn = meta_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM llm_guard WHERE id = 1")
        return cur.fetchone()


def _restore_row(row):
    conn = meta_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM llm_guard")
        if row:
            cur.execute(
                """INSERT INTO llm_guard
                       (id, tokens, refilled_at_ms, day, day_calls,
                        day_prompt_tokens, day_output_tokens, day_cost, day_degraded)
                   VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (row["tokens"], row["refilled_at_ms"], row["day"], row["day_calls"],
                 row["day_prompt_tokens"], row["day_output_tokens"],
                 row["day_cost"], row["day_degraded"]),
            )
    conn.commit()


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    """让护栏在测试里真的生效（mock 模式会跳过它），并在结束后恢复现场。"""
    saved = _snapshot_row()
    monkeypatch.setattr(guard, "LLM_GUARD_ENABLED", True)
    monkeypatch.setattr(guard, "LLM_MODE", "live")
    monkeypatch.setattr(guard, "LLM_DAILY_BUDGET", 10.0)
    monkeypatch.setattr(guard, "LLM_DAILY_CALL_LIMIT", 1000)
    monkeypatch.setattr(guard, "LLM_RATE_BURST", 100.0)
    monkeypatch.setattr(guard, "LLM_RATE_PER_MIN", 6000.0)   # 100/秒，够快
    yield
    _restore_row(saved)


# ============================================================
# 令牌桶
# ============================================================

def test_有令牌时放行():
    _reset(tokens=5.0)
    ok, why = guard.acquire(wait_max_s=0)
    assert ok, f"有令牌却拒绝了：{why}"


def test_令牌耗尽时拒绝而不是抛异常():
    """这是护栏最核心的承诺：拒绝要能优雅降级，不能炸。"""
    _reset(tokens=0.0)
    guard.LLM_RATE_PER_MIN = 0.0001          # 几乎不补充
    ok, why = guard.acquire(wait_max_s=0)
    assert not ok
    assert "速率" in why, f"拒绝原因应说明是限流：{why}"


def test_拒绝会累计降级计数():
    _reset(tokens=0.0, degraded=0)
    guard.LLM_RATE_PER_MIN = 0.0001
    guard.acquire(wait_max_s=0)
    snap = guard.snapshot()
    assert snap["degraded_today"] >= 1, "降级次数没有被记下来"


# ============================================================
# 日预算与调用次数
# ============================================================

def test_超出日预算时拒绝():
    _reset(tokens=100.0, cost=10.5)            # 预算 10.0
    ok, why = guard.acquire(wait_max_s=0)
    assert not ok
    assert "预算" in why, f"拒绝原因应说明是预算：{why}"


def test_预算类的拒绝不会傻等():
    """预算耗尽了，等下去也不会变好 —— 必须立刻返回而不是等到超时。"""
    _reset(tokens=100.0, cost=99.0)
    t0 = time.perf_counter()
    ok, why = guard.acquire(wait_max_s=30)
    elapsed = time.perf_counter() - t0
    assert not ok
    assert elapsed < 2.0, f"预算类拒绝等了 {elapsed:.1f}s，应该立刻返回"


def test_达到日调用上限时拒绝():
    _reset(tokens=100.0, calls=1000)           # 上限 1000
    ok, why = guard.acquire(wait_max_s=0)
    assert not ok
    assert "调用上限" in why, f"拒绝原因应说明是次数上限：{why}"


def test_预算内正常放行():
    _reset(tokens=100.0, cost=1.0, calls=10)
    ok, why = guard.acquire(wait_max_s=0)
    assert ok, f"预算充足却拒绝了：{why}"


# ============================================================
# 成本记账
# ============================================================

def test_成本按_prompt_和_completion_分别计价(monkeypatch):
    monkeypatch.setattr(guard, "LLM_PRICE_INPUT_PER_M", 2.0)
    monkeypatch.setattr(guard, "LLM_PRICE_OUTPUT_PER_M", 8.0)
    monkeypatch.setattr(guard, "LLM_GUARD_ENABLED", True)
    monkeypatch.setattr(guard, "LLM_MODE", "live")
    _reset()

    info = guard.record_usage({"prompt_tokens": 1000, "completion_tokens": 500,
                               "total_tokens": 1500})
    # 1000/1e6*2.0 + 500/1e6*8.0 = 0.002 + 0.004 = 0.006
    assert abs(info["cost"] - 0.006) < 1e-9, f"成本算错了：{info['cost']}"
    assert info["total_tokens"] == 1500


def test_取令牌时预扣调用次数():
    """调用次数在取令牌时就 +1 —— 因为要的是**硬上限**。"""
    _reset(tokens=100.0, calls=0)
    before = guard.snapshot()["calls_today"]
    ok, _ = guard.acquire(wait_max_s=0)
    assert ok
    assert guard.snapshot()["calls_today"] == before + 1


def test_记账只累加_token_与花费不改调用次数():
    _reset(tokens=100.0)
    guard.acquire(wait_max_s=0)
    mid = guard.snapshot()

    guard.record_usage({"prompt_tokens": 2000, "completion_tokens": 1000,
                        "total_tokens": 3000})
    after = guard.snapshot()

    assert after["calls_today"] == mid["calls_today"], "记账不该再改调用次数"
    assert after["prompt_tokens_today"] == mid["prompt_tokens_today"] + 2000
    assert after["output_tokens_today"] == mid["output_tokens_today"] + 1000
    assert after["cost_today"] > mid["cost_today"]


def test_并发下调用的硬上限不会被突破():
    """回归测试：早期版本是「调用完成后才 +1」，8 路并发下上限会变成软约束
    （实测设 10 结果跑了 17）。改成取令牌时预扣之后，并发也不能突破。"""
    from concurrent.futures import ThreadPoolExecutor

    _reset(tokens=1000.0, calls=0)
    guard.LLM_DAILY_CALL_LIMIT = 20
    guard.LLM_RATE_PER_MIN = 0.0001          # 不补充令牌，让次数上限成为唯一约束

    def take(_):
        return guard.acquire(wait_max_s=0)[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        granted = sum(1 for r in pool.map(take, range(60)) if r)

    assert granted == 20, f"上限 20，并发下实际放行了 {granted}"
    assert guard.snapshot()["calls_today"] == 20


def test_usage_缺失字段也不崩():
    _reset()
    info = guard.record_usage({})              # 接口没返回 usage 的情况
    assert info["total_tokens"] == 0
    assert info["cost"] == 0
    info2 = guard.record_usage(None)
    assert info2["total_tokens"] == 0


# ============================================================
# 跨天归档
# ============================================================

def test_跨天会归档并重置当日计数():
    yesterday = "2020-01-01"
    _reset(tokens=100.0, calls=42, cost=3.5, degraded=7, day=yesterday)

    ok, _ = guard.acquire(wait_max_s=0)
    assert ok

    with meta_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM llm_usage_daily WHERE day = %s", (yesterday,))
            archived = cur.fetchone()

    assert archived is not None, "昨天的用量没有被归档"
    assert archived["calls"] == 42
    assert float(archived["cost"]) == 3.5
    assert archived["degraded"] == 7

    snap = guard.snapshot()
    # 跨天会重置计数；剩下的 1 次正是触发归档的那次 acquire（它自己也预扣了 1）
    assert snap["calls_today"] == 1, f"跨天后当日调用数应重置，实际 {snap['calls_today']}"
    assert snap["cost_today"] == 0
    assert snap["degraded_today"] == 0


# ============================================================
# 快照
# ============================================================

def test_快照字段完整且可用于监控():
    _reset(tokens=7.0, calls=3, cost=1.25)
    snap = guard.snapshot()
    for key in ("tokens_available", "calls_today", "cost_today", "budget",
                "budget_used_pct", "degraded_today", "avg_cost_per_call"):
        assert key in snap, f"快照缺少 {key}"
    assert abs(snap["budget_used_pct"] - 12.5) < 0.1
    assert abs(snap["cost_today"] - 1.25) < 1e-6
    assert abs(snap["avg_cost_per_call"] - 1.25 / 3) < 1e-6


def test_mock_模式下护栏自动跳过(monkeypatch):
    """mock 不做真实调用，护栏必须跳过 —— 否则并发压测会被限速扭曲。"""
    monkeypatch.setattr(guard, "LLM_MODE", "mock")
    _reset(tokens=0.0, cost=999.0, calls=99999)
    ok, why = guard.acquire(wait_max_s=0)
    assert ok, f"mock 模式不该被护栏拦截：{why}"

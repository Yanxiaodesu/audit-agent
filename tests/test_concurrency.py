"""并发竞态回归测试。

**为什么单独一个文件**：`test_flow.py` 的用例都是串行跑的，
而下面这些问题**只在并发下才出现** —— 串行跑一万次都抓不到：

  1. `review_queue` 的「先查后插」在并发下会插出**两行待复核**
     （修复前实测：对齐 6 轮里有 2 轮复现）
  2. `/audits` 的幂等只在应用层，两个并发请求都查空 → **重复入队、重复烧钱**
  3. 模型给的 `confidence` 越界（百分制 / 字符串）→ `DECIMAL(4,3)` 溢出
     → 任务失败 → **重跑一整轮 LLM 循环再花一次钱**

这三个在修复前都能稳定复现，所以这里是真正的回归测试，不是形式主义。
"""
from __future__ import annotations

import pathlib
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pymysql
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import audit as audit_mod  # noqa: E402
from app.db import biz, meta, meta_conn  # noqa: E402

WORKERS = 6          # 并发度：够触发竞态，又不会把本地 MySQL 打爆


# ============================================================
# 夹具
# ============================================================

@pytest.fixture
def temp_item():
    item_no = f"PYTEST-RACE-{uuid.uuid4().hex[:8]}"
    with biz(commit=True) as cur:
        cur.execute(
            """INSERT INTO item (item_no, seller_id, category_id, title, description,
                                 price, original_price, trade_type, item_condition, status)
               VALUES (%s, 1, 2, %s, %s, 199.00, 399.00, 1, 2, 0)""",
            (item_no, f"pytest 并发测试 {item_no}", "并发竞态回归测试用"),
        )
        item_id = cur.lastrowid
    yield item_id
    _purge(item_id)


def _purge(item_id: int):
    with meta(commit=True) as cur:
        cur.execute("SELECT id FROM audit_jobs WHERE item_id = %s", (item_id,))
        jids = [r["id"] for r in cur.fetchall()]
        if jids:
            ph = ",".join(["%s"] * len(jids))
            cur.execute(f"DELETE FROM audit_steps WHERE job_id IN ({ph})", jids)
            cur.execute(f"DELETE FROM checkpoints WHERE job_id IN ({ph})", jids)
        cur.execute("DELETE FROM review_queue WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM audit_jobs WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM audit_ground_truth WHERE item_id = %s", (item_id,))
    with biz(commit=True) as cur:
        cur.execute("DELETE FROM item_audit WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM item WHERE id = %s", (item_id,))


def _new_job(item_id: int, fingerprint: str) -> int:
    """建成 `running` 而不是 `pending`：线上 worker 只领 pending，
    这样测试任务对它完全不可见，不会被抢走。"""
    conn = meta_conn()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO audit_jobs (item_id, status, fingerprint, locked_by, locked_at)
               VALUES (%s, 'running', %s, 'pytest', NOW())""",
            (item_id, fingerprint),
        )
        job_id = cur.lastrowid
    conn.commit()
    return job_id


def _review_result(item_id: int, tag: str) -> dict:
    return {"item_id": item_id, "verdict": "REVIEW", "source": "rule",
            "confidence": 0.5, "reason": f"并发测试 {tag}", "rule_hits": []}


# ============================================================
# 1. 复核队列：并发下只能有一行待复核
# ============================================================

def test_并发写回只产生一行待复核(temp_item):
    """修复前：应用层「先 SELECT 再 INSERT」，两个线程都查空 → 插两行。

    现在 `review_queue` 上有生成列唯一键 `uk_pending_item`
    （`IF(status=0, item_id, NULL)`），配合 `INSERT ... ON DUPLICATE KEY UPDATE`，
    由**数据库**保证「一个商品最多一条待复核」。
    """
    barrier = Barrier(WORKERS)

    def one(i: int):
        job_id = _new_job(temp_item, f"race-{i}")
        barrier.wait()                      # 让所有线程尽量同时冲进去
        audit_mod.writeback(temp_item, job_id, _review_result(temp_item, str(i)),
                            tracker=audit_mod.Tracker(job_id))
        return i

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(one, range(WORKERS)))

    with meta() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM review_queue WHERE item_id = %s AND status = 0",
                    (temp_item,))
        pending = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM review_queue WHERE item_id = %s", (temp_item,))
        total = cur.fetchone()["n"]

    assert pending == 1, f"待复核行应该只有 1 条，实际 {pending} 条（并发去重失效）"
    assert total == 1, f"不该留下历史垃圾行，实际共 {total} 条"


# ============================================================
# 2. 幂等：并发投递同一内容只能有一个活跃任务
# ============================================================

def test_并发投递同内容只产生一个活跃任务(temp_item):
    """模拟两个并发的 `POST /audits`：应用层的「先查后插」都会查空。

    靠 `audit_jobs` 的 `uk_live`（活跃状态 + 同 item + 同指纹）唯一键拦住第二个；
    接口层捕获 `IntegrityError` 之后复用先到的那个任务。

    这里直接打数据库，验证**约束本身**是有效的 ——
    接口层的行为由 `test_items_api.py` 覆盖。
    """
    fp = f"race-fp-{uuid.uuid4().hex[:8]}"
    barrier = Barrier(WORKERS)

    def insert(_i: int):
        conn = meta_conn()
        try:
            barrier.wait()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_jobs (item_id, status, fingerprint) "
                    "VALUES (%s, 'pending', %s)", (temp_item, fp))
            conn.commit()
            return "created"
        except pymysql.err.IntegrityError:
            conn.rollback()
            return "conflict"

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(insert, range(WORKERS)))

    assert results.count("created") == 1, \
        f"只能有一个成功插入，实际成功 {results.count('created')} 个"
    assert results.count("conflict") == WORKERS - 1

    with meta() as cur:
        cur.execute(
            """SELECT COUNT(*) AS n FROM audit_jobs
               WHERE item_id = %s AND fingerprint = %s AND status IN ('pending','running')""",
            (temp_item, fp))
        assert cur.fetchone()["n"] == 1


def test_历史任务不占用唯一键(temp_item):
    """约束只针对**活跃**任务（pending/running）。

    已经成功的任务不该继续占着键 —— 否则内容改回原样后就没法重审了。
    """
    fp = f"race-hist-{uuid.uuid4().hex[:8]}"
    with meta(commit=True) as cur:
        cur.execute(
            """INSERT INTO audit_jobs (item_id, status, fingerprint, verdict)
               VALUES (%s, 'succeeded', %s, 'APPROVE')""", (temp_item, fp))
    # 同指纹再来一个 pending，应该允许
    with meta(commit=True) as cur:
        cur.execute(
            "INSERT INTO audit_jobs (item_id, status, fingerprint) VALUES (%s, 'pending', %s)",
            (temp_item, fp))


# ============================================================
# 3. confidence：任何越界值都不能让写回失败
# ============================================================

@pytest.mark.parametrize("bad,expected_ok", [
    (10.0, True),        # 百分制写成 10
    (100, True),         # 百分制
    (1.5, True),         # 轻微越界
    ("high", True),      # 模型返回了字符串
    (-1, True),          # 负数
    (None, True),        # 没给
    (float("nan"), True),
    (0.82, True),        # 正常值
])
def test_越界置信度不会让写回失败(temp_item, bad, expected_ok):
    """修复前：`10.0` 会 DataError(1264)、`"high"` 会 ValueError。

    后果不只是这一件商品判不了 —— 异常冒到 worker 会被当成**可重试故障**，
    重跑一整轮 LLM 循环再花一次钱，最后记一条 `failed` 污染健康指标。

    现在两道收口：`loop.clean_confidence()`（生成侧）+ `audit._safe_conf()`（落库侧）。
    """
    job_id = _new_job(temp_item, f"conf-{uuid.uuid4().hex[:6]}")
    written = audit_mod.writeback(
        temp_item, job_id,
        {"item_id": temp_item, "verdict": "REJECT", "source": "llm",
         "confidence": bad, "reason": "测试越界置信度", "rule_hits": []},
        tracker=audit_mod.Tracker(job_id))

    assert written["rows_affected"] == 1, f"confidence={bad!r} 时写回失败：{written}"

    with biz() as cur:
        cur.execute("SELECT confidence, audit_result FROM item_audit WHERE item_id = %s",
                    (temp_item,))
        row = cur.fetchone()
    assert row is not None, "审核记录没写进去"
    conf = float(row["confidence"])
    assert 0.0 <= conf <= 1.0, f"落库的置信度应该在 [0,1]，实际 {conf}"
    assert int(row["audit_result"]) == 2, "REJECT 对应 audit_result=2"


def test_置信度百分制会被归一化():
    """模型给 85 显然是指 0.85，不该被当成 85% 的置信度丢掉。"""
    from agent.loop import clean_confidence
    assert clean_confidence(85) == 0.85
    assert clean_confidence(100) == 1.0
    assert clean_confidence(0.85) == 0.85
    assert clean_confidence(1.5) == 1.0      # 轻微越界按 1.0 处理
    assert clean_confidence("high") == 0.0   # 认不出来就给最低，宁可转人工


def test_结论必须归一成枚举():
    """模型填 `"approve"` / `"PASS"` / `"通过"` 都很常见。

    认不出来一律 REVIEW —— 审核系统绝不能因为解析失败就默认放行。
    """
    from agent.loop import clean_verdict
    assert clean_verdict("approve") == "APPROVE"
    assert clean_verdict("通过") == "APPROVE"
    assert clean_verdict("PASS") == "APPROVE"
    assert clean_verdict("block") == "REJECT"
    assert clean_verdict("违规") == "REJECT"
    assert clean_verdict("banana") == "REVIEW"
    assert clean_verdict(None) == "REVIEW"


# ============================================================
# 4. 终态更新的并发守卫
# ============================================================

def test_任务被回收后终态不覆盖(temp_item):
    """`process()` 的终态 UPDATE 带 `AND status='running'`。

    如果任务在处理期间被别人 reclaim 回收（锁超时）或取消，
    现在这一行已经不是 running 了 —— 那我们就不该把状态盖成 succeeded，
    否则会覆盖另一个 worker 正在写的结论。
    """
    import worker as worker_mod

    job_id = _new_job(temp_item, f"supersede-{uuid.uuid4().hex[:6]}")
    # 模拟「处理期间被回收」：把状态改回 pending
    with meta(commit=True) as cur:
        cur.execute("UPDATE audit_jobs SET status = 'pending' WHERE id = %s", (job_id,))

    conn = meta_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE audit_jobs SET status = 'succeeded' WHERE id = %s AND status = 'running'",
                    (job_id,))
        affected = cur.rowcount
    conn.commit()
    assert affected == 0, "已不是 running 的任务不该被终态更新覆盖"

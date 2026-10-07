"""集成测试：需要本地 MySQL 在跑。

主要覆盖三个真实修过的 bug 的回归测试：
  1. Tracker 步号冲突 —— writeback 步必须真的入库，且不能覆盖 load 步的 payload
  2. 复核队列重复行   —— 同一商品重复审核不应堆积多条待处理记录
  3. 幂等             —— 内容指纹相同应能识别为同一份内容

跑法：
    pytest tests/ -v
    pytest tests/test_rules.py -v     # 只跑快的单元测试
"""
from __future__ import annotations

import json
import pathlib
import sys
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import audit as audit_mod  # noqa: E402
from app.db import biz, meta, meta_conn  # noqa: E402
import worker as worker_mod  # noqa: E402


# ============================================================
# 夹具：临时商品 + 任务，测试完清理干净
# ============================================================

@pytest.fixture
def temp_item():
    """建一件临时商品，测试结束连它的审核记录一起删掉。"""
    item_no = f"PYTEST-{uuid.uuid4().hex[:10]}"
    with biz(commit=True) as cur:
        cur.execute(
            """INSERT INTO item (item_no, seller_id, category_id, title, description,
                                 price, original_price, trade_type, item_condition, status)
               VALUES (%s, 1, 2, %s, %s, 199.00, 399.00, 1, 2, 0)""",
            (item_no, f"pytest 临时商品 {item_no}", "自动化测试用，测试结束即删除"),
        )
        item_id = cur.lastrowid
    yield item_id
    with biz(commit=True) as cur:
        cur.execute("DELETE FROM item_audit WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM item WHERE id = %s", (item_id,))
    _cleanup_jobs(item_id)


def _cleanup_jobs(item_id: int):
    """清掉这件商品产生的所有任务与轨迹。

    连接来自 `meta_conn()` 的**线程缓存**，所以这里不 close（见 app/db.py）。
    必须显式 commit：pymysql 连接的 `with` 在块内抛异常时会回滚，
    那会让「测试失败时反而留下垃圾数据」。
    """
    conn = meta_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM audit_jobs WHERE item_id = %s", (item_id,))
        for r in cur.fetchall():
            cur.execute("DELETE FROM audit_steps WHERE job_id = %s", (r["id"],))
            cur.execute("DELETE FROM checkpoints WHERE job_id = %s", (r["id"],))
        cur.execute("DELETE FROM review_queue WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM audit_jobs WHERE item_id = %s", (item_id,))
    conn.commit()


def _new_job(item_id: int, fingerprint: str = "pytest") -> int:
    """建一个测试用任务。

    ⚠ 状态必须是 running 而不是 pending。
    如果建成 pending，**正在运行的线上 worker 会把它抢走**，
    而测试的清理跑在 worker 写轨迹之前 —— 结果就是留下一堆孤儿轨迹步
    （job 被删了，step 还在）。

    这里直接建成 running（并带上 locked_by），worker 的 claim 只取 pending，
    于是测试任务对线上 worker 完全不可见。
    """
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


# ============================================================
# 回归 1：Tracker 步号冲突
# ============================================================

def test_writeback_步必须真的入库且不覆盖_load(temp_item):
    """这是真实发生过的 bug。

    worker 曾经给 writeback 又新建了一个 Tracker，步号从 1 重来，
    ON DUPLICATE KEY UPDATE 把第 1 步（load）的 payload 覆盖成了 writeback 的数据，
    而 kind 字段不在更新列表里，所以标签还留着 load。
    结果：writeback 步从未入库，load 步的内容被静默破坏。
    """
    job_id = _new_job(temp_item)
    worker_mod.process(meta_conn(), {"id": job_id, "item_id": temp_item, "attempts": 1})
    with meta() as cur:
        cur.execute(
            "SELECT step_no, kind, payload FROM audit_steps WHERE job_id = %s ORDER BY step_no",
            (job_id,),
        )
        steps = cur.fetchall()

    kinds = [s["kind"] for s in steps]

    assert "load" in kinds, "load 步不见了"
    assert "writeback" in kinds, "writeback 步没有入库 —— Tracker 步号冲突回归了"
    assert kinds.count("load") == 1, "load 步出现了多次"

    # 步号必须严格连续
    assert [s["step_no"] for s in steps] == list(range(1, len(steps) + 1)), \
        f"步号不连续：{[s['step_no'] for s in steps]}"

    # load 步的 payload 必须还是 load 的数据
    load_payload = next(s["payload"] for s in steps if s["kind"] == "load")
    if isinstance(load_payload, str):
        load_payload = json.loads(load_payload)
    assert "title" in load_payload, f"load 步的 payload 被覆盖了：{list(load_payload)}"
    assert "fingerprint" in load_payload

    # writeback 步的 payload 应该是写回结果
    wb_payload = next(s["payload"] for s in steps if s["kind"] == "writeback")
    if isinstance(wb_payload, str):
        wb_payload = json.loads(wb_payload)
    assert "verdict" in wb_payload


def test_同一个_job_用两个_Tracker_会撞车(temp_item):
    """把危险行为本身固化成测试：两个 Tracker 的步号会互相覆盖。

    现在 record() 会同步更新 kind（兜底），所以至少不会再出现
    「标签写着 load、内容却是 writeback」这种静默错位 ——
    但数据仍然会丢，所以「一个任务只能有一个 Tracker」这条约束必须守住。
    """
    job_id = _new_job(temp_item)
    tk1 = audit_mod.Tracker(job_id)
    tk1.record("load", {"title": "原始标题"})
    tk2 = audit_mod.Tracker(job_id)          # 第二个 Tracker，步号从 1 重来
    tk2.record("writeback", {"verdict": "APPROVE"})

    conn = meta_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, payload FROM audit_steps WHERE job_id = %s ORDER BY step_no",
            (job_id,),
        )
        rows = cur.fetchall()

    # 只剩一步 —— load 步的数据被吃掉了
    assert len(rows) == 1, "两个 Tracker 产生了不同的步号，说明它们没撞车"
    payload = rows[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert "verdict" in payload, "payload 已被 writeback 覆盖"
    # 兜底：kind 也一起更新了，不会出现标签与内容不符
    assert rows[0]["kind"] == "writeback"


# ============================================================
# 回归 2：复核队列不堆积重复行
# ============================================================

def test_重复审核不堆积复核行(temp_item):
    """同一件商品被审两次（都判 REVIEW），队列里只应有一条待处理。"""
    conn = meta_conn()
    for i in range(2):
        # 两个不同指纹的任务（相当于内容改过之后又审了一次）。
        # 不能再用同一个指纹 —— audit_jobs 上有 uk_live 唯一键，
        # 同一商品的同指纹活跃任务只允许存在一个（那是防止重复烧钱的约束）。
        job_id = _new_job(temp_item, fingerprint=f"pytest-{i}")
        result = {"item_id": temp_item, "verdict": "REVIEW", "source": "rule",
                  "confidence": 0.5, "reason": "测试：转人工", "rule_hits": []}
        tracker = audit_mod.Tracker(job_id)
        audit_mod.writeback(temp_item, job_id, result, tracker=tracker)

    with meta() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM review_queue WHERE item_id = %s AND status = 0",
            (temp_item,),
        )
        n = cur.fetchone()["n"]

    assert n == 1, f"复核队列里堆积了 {n} 条待处理记录，应该只有 1 条"


# ============================================================
# 回归 3：内容指纹
# ============================================================

class TestFingerprint:

    def test_相同内容指纹一致(self):
        a = {"title": "iPad", "description": "九成新", "price": 199, "original_price": 399, "category_id": 2}
        b = dict(a)
        assert audit_mod.fingerprint(a) == audit_mod.fingerprint(b)

    @pytest.mark.parametrize("field,value", [
        ("title", "iPad Pro"),
        ("description", "全新未拆"),
        ("price", 299),
        ("original_price", 499),
        ("category_id", 3),
    ])
    def test_内容变了指纹就变(self, field, value):
        base = {"title": "iPad", "description": "九成新", "price": 199, "original_price": 399, "category_id": 2}
        changed = {**base, field: value}
        assert audit_mod.fingerprint(base) != audit_mod.fingerprint(changed), \
            f"{field} 变了但指纹没变，幂等会误判"


# ============================================================
# 回归 4：条件更新不覆盖人工裁决
# ============================================================

def test_已裁决的商品不被AI覆盖(temp_item):
    """商品被人工作出结论后（status 变 1 或 5），后续 AI 写回不应覆盖它。"""
    # 先把它设成人工已通过
    with biz(commit=True) as cur:
        cur.execute("UPDATE item SET status = 1 WHERE id = %s", (temp_item,))

    job_id = _new_job(temp_item)
    result = {"item_id": temp_item, "verdict": "REJECT", "source": "rule",
              "confidence": 0.98, "reason": "测试：AI 想驳回", "rule_hits": []}
    tracker = audit_mod.Tracker(job_id)
    written = audit_mod.writeback(temp_item, job_id, result, tracker=tracker)

    with biz() as cur:
        cur.execute("SELECT status FROM item WHERE id = %s", (temp_item,))
        status = cur.fetchone()["status"]

    assert status == 1, "人工已裁决为通过，AI 的驳回不应覆盖它"
    assert written.get("rows_affected") == 0
    assert written.get("skipped")

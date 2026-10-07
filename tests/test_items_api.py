"""商品增删接口的测试。

虽然是「审核服务」，但审核的输入商品总得有人能加进来 ——
原来只能手写 SQL，这组测试把这条链路固化住。

覆盖了三类容易被忽略的点：
  1. **卖家必须是普通人** —— sys_user 里还有 role=9 的 AI 审核员和 role=2 的管理员，
     商品挂到系统账号名下会让「该卖家历史被驳回数」之类的统计全乱
  2. **删商品要顺手取消它排队中的任务** —— 否则 worker 会白跑一趟
  3. **商品被删后任务应当是 cancelled 而不是 failed** ——
     「商品没了」是确定性终态，重试 3 次只是浪费，还会污染失败指标
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.audit import ItemNotAuditable, load_context  # noqa: E402
from app.db import biz, meta  # noqa: E402

API = "http://127.0.0.1:8100"


@pytest.fixture(scope="module")
def client():
    httpx = pytest.importorskip("httpx")
    c = httpx.Client(base_url=API, timeout=30)
    try:
        c.get("/health").raise_for_status()
    except Exception:
        pytest.skip("API 未启动，跳过接口测试（先 python -m uvicorn app.main:app）")
    return c


@pytest.fixture
def cat_id(client):
    cats = client.get("/categories").json()["categories"]
    assert cats, "没有任何分类，先跑 scripts/bootstrap.py"
    return cats[0]["id"]


def _mk(client, cat_id, **over):
    body = {"title": "测试商品 临时", "description": "pytest", "price": 10.0,
            "category_id": cat_id}
    body.update(over)
    r = client.post("/items", json=body)
    assert r.status_code == 201, f"创建失败 HTTP {r.status_code}: {r.text}"
    return r.json()


def _cleanup(item_id: int):
    """物理删除，别给演示数据留垃圾。

    先取消排队中的任务：否则 worker 可能正在我们删除的同时开始处理，
    它写下的轨迹会变成孤儿步。worker 的周期回收会兜底清掉，
    但测试不该故意制造这种竞态。
    """
    with meta(commit=True) as cur:
        cur.execute(
            """UPDATE audit_jobs SET status = 'cancelled', locked_by = NULL, locked_at = NULL
               WHERE item_id = %s AND status = 'pending'""", (item_id,))
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
        cur.execute("DELETE FROM item_image WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM item_audit WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM item WHERE id = %s", (item_id,))


# ============================================================
# 新增
# ============================================================

def test_新增商品并自动送审(client, cat_id):
    res = _mk(client, cat_id, title="pytest 新增商品")
    try:
        assert res["item_id"] > 0
        assert res["item_no"].startswith("IT")
        assert res["status"] == 0, "新商品应该是待审核"
        assert res["job_id"], "auto_audit=true 时应该已经投递了任务"

        # 任务确实进了队列
        job = client.get(f"/audits/{res['job_id']}").json()
        assert job["item_id"] == res["item_id"]
        assert job["status"] in ("pending", "running", "succeeded")
    finally:
        _cleanup(res["item_id"])


def test_新增商品必须落在普通卖家名下(client, cat_id):
    """回归：随机挑卖家时曾经挑到 role=9 的 AI 审核员。

    sys_user 里有两种系统账号（999 AI 审核员、998 管理员），
    商品挂到它们名下会让卖家维度的统计失真。
    """
    res = _mk(client, cat_id)
    try:
        with biz() as cur:
            cur.execute("SELECT role, username FROM sys_user WHERE id = %s", (res["seller_id"],))
            u = cur.fetchone()
        assert u is not None, "卖家不存在"
        assert u["role"] == 1, \
            f"卖家应当是普通用户(role=1)，实际是 {u['username']}(role={u['role']})"
    finally:
        _cleanup(res["item_id"])


def test_可以指定卖家(client, cat_id):
    with biz() as cur:
        cur.execute("SELECT id FROM sys_user WHERE role = 1 AND deleted = 0 LIMIT 1")
        sid = cur.fetchone()["id"]
    res = _mk(client, cat_id, seller_id=sid)
    try:
        assert res["seller_id"] == sid
    finally:
        _cleanup(res["item_id"])


def test_auto_audit_false_时不投递(client, cat_id):
    res = _mk(client, cat_id, auto_audit=False)
    try:
        assert res["job_id"] is None
        with meta() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM audit_jobs WHERE item_id = %s", (res["item_id"],))
            assert cur.fetchone()["n"] == 0
    finally:
        _cleanup(res["item_id"])


def test_分类不存在时返回400(client):
    r = client.post("/items", json={"title": "x", "price": 1, "category_id": 99999999})
    assert r.status_code == 400
    assert "分类" in r.text


def test_标题为空时被拒绝(client, cat_id):
    r = client.post("/items", json={"title": "", "price": 1, "category_id": cat_id})
    assert r.status_code == 422, "空标题应该被 pydantic 挡下"


# ============================================================
# 删除
# ============================================================

def test_软删除后默认列表查不到(client, cat_id):
    # ⚠ 用关键词搜索而不是翻列表：库里已经有 200+ 件商品，
    # 新建的商品 id 最大，`ORDER BY id LIMIT 200` 会把它截掉 ——
    # 断言就会因为「分页没翻到」而失败，而不是因为功能有问题。
    title = "pytest软删除专用标题ZZQ"
    res = _mk(client, cat_id, title=title, auto_audit=False)
    iid = res["item_id"]
    try:
        assert client.delete(f"/items/{iid}").status_code == 200

        lst = client.get("/items", params={"status": -1, "q": title, "limit": 50}).json()
        assert lst["total"] == 0, "软删除后默认不该再搜到"

        lst2 = client.get("/items", params={"status": -1, "q": title,
                                            "include_deleted": True, "limit": 50}).json()
        assert lst2["total"] == 1, "include_deleted=true 应该能看到它"
        assert lst2["items"][0]["deleted"] is True, "返回里应该标明它已被删除"
    finally:
        _cleanup(iid)


def test_软删除会取消排队中的任务(client, cat_id):
    res = _mk(client, cat_id)          # auto_audit=True，队列里有一个 pending 任务
    iid, jid = res["item_id"], res["job_id"]
    try:
        r = client.delete(f"/items/{iid}").json()
        assert r["cancelled_jobs"] >= 1, "删除时应当取消排队中的任务"
        job = client.get(f"/audits/{jid}").json()
        assert job["status"] == "cancelled", f"任务应被取消，实际 {job['status']}"
    finally:
        _cleanup(iid)


def test_重复删除返回404(client, cat_id):
    res = _mk(client, cat_id, auto_audit=False)
    iid = res["item_id"]
    try:
        assert client.delete(f"/items/{iid}").status_code == 200
        assert client.delete(f"/items/{iid}").status_code == 404, "已删除的再删应该 404"
    finally:
        _cleanup(iid)


def test_物理删除会清干净所有关联数据(client, cat_id):
    res = _mk(client, cat_id)
    iid = res["item_id"]
    r = client.delete(f"/items/{iid}", params={"hard": True}).json()
    assert r["mode"] == "hard"

    with biz() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM item WHERE id = %s", (iid,))
        assert cur.fetchone()["n"] == 0, "商品没删掉"
        cur.execute("SELECT COUNT(*) AS n FROM item_image WHERE item_id = %s", (iid,))
        assert cur.fetchone()["n"] == 0
    with meta() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM audit_jobs WHERE item_id = %s", (iid,))
        assert cur.fetchone()["n"] == 0


# ============================================================
# 恢复
# ============================================================

def test_有任务在跑时拒绝物理删除(client, cat_id):
    """回归：在 worker 正在给 job 写轨迹时把 job 删掉，会留下**孤儿轨迹步**。

    真实发生过 —— 测试清理跑在 worker 写轨迹之前，留下 4 条孤儿步。
    所以物理删除遇到 running 任务必须拒绝（409），而不是默默制造垃圾。
    """
    res = _mk(client, cat_id, auto_audit=False)
    iid = res["item_id"]
    try:
        with meta(commit=True) as cur:
            cur.execute(
                """INSERT INTO audit_jobs (item_id, status, fingerprint, locked_by, locked_at)
                   VALUES (%s, 'running', 'pytest-race', 'pytest', NOW())""", (iid,))
        r = client.delete(f"/items/{iid}", params={"hard": True})
        assert r.status_code == 409, \
            f"有任务在跑时物理删除应被拒绝，实际 HTTP {r.status_code}: {r.text[:150]}"
        assert "正在处理" in r.text or "孤儿" in r.text

        # 软删除不受影响 —— 它只取消 pending，running 的会让它跑完
        assert client.delete(f"/items/{iid}").status_code == 200
    finally:
        _cleanup(iid)


def test_恢复后回到待审核(client, cat_id):
    res = _mk(client, cat_id, auto_audit=False)
    iid = res["item_id"]
    try:
        client.delete(f"/items/{iid}")
        assert client.post(f"/items/{iid}/restore").json()["status"] == 0
        detail = client.get(f"/items/{iid}/detail").json()
        item = detail.get("item", detail)
        assert not item.get("deleted"), "恢复后 deleted 应该回到 0"
    finally:
        _cleanup(iid)


def test_没被删除的商品不能恢复(client, cat_id):
    res = _mk(client, cat_id, auto_audit=False)
    try:
        assert client.post(f"/items/{res['item_id']}/restore").status_code == 404
    finally:
        _cleanup(res["item_id"])


# ============================================================
# 审核链路对「商品已删除」的处理
# ============================================================

def test_商品被删后审核上下文抛可识别异常(client, cat_id):
    """`ItemNotAuditable` 是专门的异常类型，worker 靠它区分
    「可重试的瞬时故障」和「确定性的终态」。"""
    res = _mk(client, cat_id, auto_audit=False)
    iid = res["item_id"]
    try:
        client.delete(f"/items/{iid}")
        with pytest.raises(ItemNotAuditable):
            load_context(iid)
    finally:
        _cleanup(iid)


def test_不存在的商品也是可识别异常():
    with pytest.raises(ItemNotAuditable):
        load_context(99999999)

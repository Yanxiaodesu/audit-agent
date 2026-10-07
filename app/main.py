"""FastAPI 应用。

对外提供：投递审核任务、查任务与轨迹、人工复核队列、指标评测、演示页面。
真正的审核在 worker 里跑，提交即返回，不阻塞平台发布流程。
"""
import pathlib
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime

import pymysql
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from agent import audit as audit_mod
from app import guard
from app import metrics as M
from app.config import (ADMIN_TOKEN, BIZ_DB, LLM_ENABLED, LLM_MODE, LLM_MODEL,
                        LLM_TOOL_BUDGET, REVIEW_THRESHOLD)
from app.db import biz, meta
from app.logging_setup import (bind_trace, clear_trace, get_logger, new_trace_id,
                               setup_logging)

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"

setup_logging("api")
log = get_logger("api")

setup_logging("api")
log = get_logger("api")


# ============================================================
# 鉴权
# ============================================================

def require_admin(x_admin_token: str | None = Header(None, alias="X-Admin-Token")):
    """管理类接口的鉴权依赖。

    `ADMIN_TOKEN` 没配置时**不启用**（本地演示方便）—— 但启动日志里会警告，
    免得有人在公网上也这么跑。配置了就强制校验，用常数时间比较避免时序侧信道。
    """
    if not ADMIN_TOKEN:
        return                      # 未配置 = 不鉴权（启动时已警告）
    if not secrets.compare_digest(x_admin_token or "", ADMIN_TOKEN):
        raise HTTPException(401, "缺少或错误的 X-Admin-Token 请求头")


def require_admin_for_hard_delete(
    request: Request,
    x_admin_token: str | None = Header(None, alias="X-Admin-Token"),
):
    """软删除不鉴权（可恢复、影响小），**物理删除要鉴权**。

    物理删除会连 `audit_ground_truth`（评测基线）一起抹掉，误用一次基线就没了。
    """
    if str(request.query_params.get("hard", "")).lower() in ("1", "true", "yes", "on"):
        require_admin(x_admin_token)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动钩子：把鉴权状态明确打出来。

    用 lifespan 而不是已经弃用的 `@app.on_event("startup")`。
    """
    if not ADMIN_TOKEN:
        log.warning("auth.disabled", extra={"fields": {
            "detail": "ADMIN_TOKEN 未配置：/admin/reset、human-decide、"
                      "物理删除等管理接口当前**无鉴权**，仅适合本地演示",
        }})
    else:
        log.info("auth.enabled", extra={"fields": {
            "protected": ["POST /admin/reset", "DELETE /items/{id}?hard=true",
                          "POST /items/{id}/human-decide"]}})
    yield


app = FastAPI(title="audit-agent", version="0.4.0",
              description="校园二手交易平台 商品合规审核 Agent 服务",
              lifespan=lifespan)


@app.middleware("http")
async def access_log(request: Request, call_next):
    """给每个请求分配 trace id，并记一条结构化访问日志。

    trace id 会回写到响应头 `X-Trace-Id`，出错时能顺着它去日志里捞整条链路。
    客户端也可以自带 `X-Trace-Id`，把上游的链路串起来。
    """
    trace_id = request.headers.get("X-Trace-Id") or new_trace_id("req")
    path = request.url.path
    bind_trace(request_id=trace_id, method=request.method, path=path)
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("http.error")
        clear_trace()
        raise
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    response.headers["X-Trace-Id"] = trace_id
    if path not in ("/metrics",) and not path.startswith("/static"):
        log.info("http.request", extra={"fields": {
            "status": response.status_code, "elapsed_ms": elapsed_ms}})
    clear_trace()
    return response

ITEM_STATUS = {"0": "待审核", "1": "在售", "2": "已锁定", "3": "已售出", "4": "已下架", "5": "审核驳回"}


# ============================================================
# 基础
# ============================================================

class AuditIn(BaseModel):
    item_id: int = Field(..., gt=0, description="要审核的商品 ID")
    force: bool = Field(False, description="true 表示忽略幂等，强制重新审核")


class BatchIn(BaseModel):
    status: int = Field(0, description="按商品状态批量投递，默认 0=待审核")
    limit: int = Field(500, ge=1, le=2000)


class ResetIn(BaseModel):
    clear_evals: bool = Field(False, description="是否同时清空历次评测记录")


class DecideIn(BaseModel):
    decision: str = Field(..., description="APPROVE 或 REJECT")
    remark: str = Field("", max_length=500, description="人工意见")
    reviewer_id: int = Field(998, description="裁决人 sys_user.id，默认管理员")


class ItemIn(BaseModel):
    """新增商品。只暴露卖家真正会填的字段，其余用默认值。"""

    title: str = Field(..., min_length=1, max_length=100, description="商品标题")
    description: str = Field("", max_length=2000, description="商品描述")
    price: float = Field(..., ge=0, le=9999999, description="售价")
    original_price: float | None = Field(None, ge=0, le=9999999, description="原价（可选）")
    category_id: int = Field(..., gt=0, description="分类 ID")
    seller_id: int | None = Field(None, gt=0, description="卖家 ID；不传则随机挑一个正常卖家")
    trade_type: int = Field(1, ge=1, le=3, description="1面交/2邮寄/3均可")
    item_condition: int = Field(2, ge=1, le=4, description="1全新/2九成新/3七成新/4五成新及以下")
    auto_audit: bool = Field(True, description="建完是否立刻投递审核任务")


@app.get("/health")
def health():
    with meta() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
    with biz() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
    return {
        "code": 0, "message": "success", "data": "UP",
        "llm_enabled": LLM_ENABLED,
        "llm_mode": LLM_MODE,
        "llm_model": LLM_MODEL if LLM_ENABLED else None,
        "decide_note": ("未配置 LLM，审核走「规则引擎 + 人工复核」" if not LLM_ENABLED
                        else ("LLM 走 mock 模式（脚本化假响应，用于验证循环）"
                              if LLM_MODE == "mock" else "规则 + LLM")),
    }


# ============================================================
# 审核任务
# ============================================================

LIVE_STATUSES = ("pending", "running", "succeeded")


@app.post("/audits")
def create_audit(body: AuditIn, response: Response):
    """提交单件商品审核。

    幂等：按「商品 ID + 内容指纹」去重。同一份内容重复投递时**直接复用已有任务**
    （返回 200），不重复烧模型调用。内容改了（标题/描述/价格变了）指纹就变，
    会正常重新审。确实要强制重审可以传 `force=true`。

    首轮创建返回 202。
    """
    with biz() as cur:
        cur.execute(
            """SELECT id, title, description, price, original_price, category_id, status
               FROM item WHERE id = %s AND deleted = 0""",
            (body.item_id,),
        )
        item = cur.fetchone()
    if not item:
        raise HTTPException(404, f"商品 {body.item_id} 不存在")

    fp = audit_mod.fingerprint(item)

    with meta(commit=True) as cur:
        if not body.force:
            cur.execute(
                """SELECT id, status, verdict FROM audit_jobs
                   WHERE item_id = %s AND fingerprint = %s AND status IN %s
                   ORDER BY id DESC LIMIT 1""",
                (body.item_id, fp, LIVE_STATUSES),
            )
            dup = cur.fetchone()
            if dup:
                response.status_code = 200
                return {
                    "job_id": dup["id"], "item_id": body.item_id, "status": dup["status"],
                    "verdict": dup["verdict"], "idempotent": True,
                    "note": "内容未变化，复用已有任务；要强制重审请传 force=true",
                }
        try:
            cur.execute(
                "INSERT INTO audit_jobs (item_id, status, fingerprint) VALUES (%s, 'pending', %s)",
                (body.item_id, fp),
            )
            job_id, created = cur.lastrowid, True
        except pymysql.err.IntegrityError:
            # 并发投递：两个请求的「先查后插」都查空了，
            # 数据库的唯一键 uk_live 拦下了第二个 —— 这时应当**复用**先到的那个，
            # 而不是把 500 抛给调用方。
            cur.execute(
                """SELECT id, status, verdict FROM audit_jobs
                   WHERE item_id = %s AND fingerprint = %s AND status IN %s
                   ORDER BY id DESC LIMIT 1""",
                (body.item_id, fp, LIVE_STATUSES),
            )
            row = cur.fetchone()
            if not row:      # 极小概率：冲突的行刚好被改掉了，让调用方重试
                raise HTTPException(409, "并发投递冲突，请重试")
            job_id, created = row["id"], False

    if not created:
        response.status_code = 200
        return {"job_id": job_id, "item_id": body.item_id, "status": "pending",
                "idempotent": True,
                "note": "并发投递冲突，复用了先到的任务"}

    response.status_code = 202
    return {"job_id": job_id, "item_id": body.item_id, "status": "pending",
            "idempotent": False, "fingerprint": fp}


@app.post("/audits/batch", status_code=202)
def create_batch(body: BatchIn):
    """按状态批量投递。

    同样幂等：已经存在活跃任务的商品会被跳过，不会重复入队。
    """
    with biz() as cur:
        cur.execute(
            """SELECT id, title, description, price, original_price, category_id
               FROM item WHERE status = %s AND deleted = 0 ORDER BY id LIMIT %s""",
            (body.status, body.limit),
        )
        items = cur.fetchall()
    if not items:
        return {"enqueued": 0, "skipped": 0, "item_status": body.status}

    rows = [(it["id"], audit_mod.fingerprint(it)) for it in items]
    ids = [r[0] for r in rows]

    with meta(commit=True) as cur:
        ph = ",".join(["%s"] * len(ids))
        cur.execute(
            f"""SELECT item_id, fingerprint FROM audit_jobs
                WHERE item_id IN ({ph}) AND status IN ('pending','running','succeeded')""",
            ids,
        )
        live = {(r["item_id"], r["fingerprint"]) for r in cur.fetchall()}
        todo = [r for r in rows if r not in live]
        if todo:
            cur.executemany(
                "INSERT INTO audit_jobs (item_id, status, fingerprint) VALUES (%s, 'pending', %s)",
                todo,
            )
    return {"enqueued": len(todo), "skipped": len(rows) - len(todo), "item_status": body.status}


@app.get("/audits/{job_id}")
def get_audit(job_id: int):
    with meta() as cur:
        cur.execute("SELECT * FROM audit_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, f"任务 {job_id} 不存在")
    return row


@app.get("/audits/{job_id}/steps")
def get_steps(job_id: int):
    """任务的完整执行轨迹。

    任务不存在时返回 **404**（而不是空数组）—— 和 `/audits/{job_id}` 保持一致。
    返回 `[]` 会让调用方分不清「任务不存在」和「任务存在但还没写轨迹」。
    """
    with meta() as cur:
        cur.execute("SELECT id FROM audit_jobs WHERE id = %s", (job_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"任务 {job_id} 不存在")
        cur.execute(
            """SELECT step_no, kind, payload, latency_ms, tokens, created_at
               FROM audit_steps WHERE job_id = %s ORDER BY step_no""",
            (job_id,),
        )
        return cur.fetchall()


# ============================================================
# 商品与复核队列
# ============================================================

@app.get("/items")
def list_items(status: int = Query(0, description="商品状态；传 -1 表示不限"),
               q: str = Query(None, description="关键词，搜标题与描述"),
               include_deleted: bool = Query(False, description="是否包含已下架（软删除）的商品"),
               limit: int = Query(30, ge=1, le=200),
               offset: int = Query(0, ge=0)):
    where = [] if include_deleted else ["i.deleted = 0"]
    args: list = []
    if status >= 0:
        where.append("i.status = %s")
        args.append(status)
    if q:
        where.append("(i.title LIKE %s OR IFNULL(i.description,'') LIKE %s)")
        args += [f"%{q}%", f"%{q}%"]
    clause = " AND ".join(where) if where else "1=1"

    with biz() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM item i WHERE {clause}", args)
        total = cur.fetchone()["n"]
        cur.execute(
            f"""SELECT i.id, i.title, i.price, i.status, i.deleted, c.name AS category_name,
                       u.credit_score, u.nickname AS seller
                FROM item i
                LEFT JOIN item_category c ON c.id = i.category_id
                LEFT JOIN sys_user u ON u.id = i.seller_id
                WHERE {clause}
                ORDER BY i.id LIMIT %s OFFSET %s""",
            args + [limit, offset],
        )
        rows = cur.fetchall()
    for r in rows:
        r["price"] = float(r["price"])
        r["deleted"] = bool(r["deleted"])
    return {"total": total, "count": len(rows), "offset": offset, "items": rows}


# ============================================================
# 商品增删（审核服务的入参来源）
# ============================================================

def _gen_item_no() -> str:
    """生成商品编号：IT + 年月日 + 6 位随机。

    极小概率碰撞由 `uk_item_no` 唯一索引兜住，调用方重试即可。
    """
    return f"IT{datetime.now():%Y%m%d}{secrets.randbelow(1000000):06d}"


def _cancel_pending_jobs(item_id: int, reason: str) -> int:
    """取消该商品**排队中**的任务。

    为什么必须做：商品删掉后，worker 拿到任务会在 `load_context` 抛
    `ItemNotAuditable` —— 虽然现在会被正确标成 `cancelled`（不是 failed），
    但让任务在队列里白跑一趟、走一遍 claim → 加载 → 失败的流程本身就是浪费，
    能提前摘掉就提前摘掉。

    `running` 的不动：worker 正在处理，它会自己收尾。
    """
    with meta(commit=True) as cur:
        cur.execute(
            """UPDATE audit_jobs SET status = 'cancelled', error = %s
               WHERE item_id = %s AND status = 'pending'""",
            (reason, item_id),
        )
        return cur.rowcount


@app.get("/categories")
def list_categories():
    """分类列表（页面新增商品时的下拉框数据）。"""
    with biz() as cur:
        cur.execute(
            """SELECT id, name, parent_id FROM item_category
               WHERE deleted = 0 AND status = 1 ORDER BY sort, id""")
        rows = cur.fetchall()
    return {"count": len(rows), "categories": rows}


@app.post("/items", status_code=201)
def create_item(body: ItemIn):
    """新增一件商品。

    默认 `auto_audit=true` —— 建完立刻投递审核任务，返回的 `job_id` 可以直接拿去
    `GET /audits/{job_id}` 轮询结果。传 `false` 则只建商品，之后再手动投递。

    不传 `seller_id` 时，会随机挑一个**状态正常且信用分达标**的卖家，
    避免新品落到被封号或低信用的账号上、一出生就进复核队列。
    """
    with biz() as cur:
        cur.execute("SELECT id, name FROM item_category WHERE id = %s AND deleted = 0",
                    (body.category_id,))
        if not cur.fetchone():
            raise HTTPException(400, f"分类 {body.category_id} 不存在")

    seller_id = body.seller_id
    if seller_id:
        with biz() as cur:
            cur.execute("SELECT id FROM sys_user WHERE id = %s AND deleted = 0", (seller_id,))
            if not cur.fetchone():
                raise HTTPException(400, f"卖家 {seller_id} 不存在")
    else:
        # ⚠ 必须限定 role = 1（普通用户）。
        # sys_user 里还有 role=9 的 AI 审核员和 role=2 的管理员 —— 它们是系统账号，
        # 让商品挂到它们名下，后面「卖家历史被驳回数」之类的统计就全乱了。
        with biz() as cur:
            cur.execute(
                """SELECT id FROM sys_user
                   WHERE role = 1 AND status = 1 AND deleted = 0 AND credit_score >= 60
                   ORDER BY RAND() LIMIT 1""")
            row = cur.fetchone()
        if not row:
            raise HTTPException(500, "没有可用的卖家账号，请先跑 python scripts/bootstrap.py")
        seller_id = int(row["id"])

    # 编号靠唯一索引兜碰撞，撞了就换一个重来
    item_id, item_no, last_err = None, None, None
    for _ in range(3):
        candidate = _gen_item_no()
        try:
            with biz(commit=True) as cur:
                cur.execute(
                    """INSERT INTO item
                           (item_no, seller_id, category_id, title, description, price,
                            original_price, cover_img, trade_type, item_condition, status)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)""",
                    (candidate, seller_id, body.category_id, body.title.strip(),
                     body.description, body.price, body.original_price,
                     f"/img/{candidate}.jpg", body.trade_type, body.item_condition),
                )
                item_id, item_no = cur.lastrowid, candidate
            break
        except pymysql.err.IntegrityError as e:      # 编号撞车，换一个
            last_err = e
    if item_id is None:
        raise HTTPException(500, f"生成商品编号失败（连续 3 次碰撞）：{last_err}")

    job_id = None
    if body.auto_audit:
        with biz() as cur:
            cur.execute(
                """SELECT id, title, description, price, original_price, category_id, status
                   FROM item WHERE id = %s""", (item_id,))
            row = cur.fetchone()
        with meta(commit=True) as cur:
            cur.execute(
                """INSERT INTO audit_jobs (item_id, status, fingerprint)
                   VALUES (%s, 'pending', %s)""",
                (item_id, audit_mod.fingerprint(row)),
            )
            job_id = cur.lastrowid

    return {
        "ok": True, "item_id": int(item_id), "item_no": item_no,
        "seller_id": seller_id, "status": 0, "job_id": job_id,
        "note": ("已投递审核，可轮询 GET /audits/{job_id}" if job_id
                 else "未投递审核；之后可 POST /audits 或 /audits/batch"),
    }


@app.delete("/items/{item_id}")
def delete_item(item_id: int,
                hard: bool = Query(False, description="true = 物理删除，不可恢复"),
                _: None = Depends(require_admin_for_hard_delete)):
    """下架 / 删除商品。

    **默认软删除**：`deleted=1` + 状态改为「已下架」，并**取消该商品排队中的任务**。
    软删除可以恢复（`POST /items/{item_id}/restore`），审核记录也保留着。

    `hard=true` 才是物理删除：连同图片、审核记录、标注答案、举报、任务与轨迹一起抹掉，
    **不可恢复** —— 一般只在清理测试数据时用。
    """
    with biz() as cur:
        cur.execute("SELECT id FROM item WHERE id = %s AND deleted = 0", (item_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"商品 {item_id} 不存在或已下架")

    # ⚠ 有任务正在处理时不允许物理删除：
    # worker 可能正在给这个 job 写轨迹，删掉 job 会让那些轨迹变成**孤儿步**
    # （真实发生过 —— 清理跑在 worker 写轨迹之前，留下 4 条孤儿步）。
    # 软删除没有这个问题：只取消 pending，running 的会让它正常跑完。
    if hard:
        with meta() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM audit_jobs WHERE item_id = %s AND status = 'running'",
                (item_id,))
            running = int(cur.fetchone()["n"])
        if running:
            raise HTTPException(
                409, f"该商品有 {running} 个任务正在处理中，物理删除会留下孤儿轨迹。"
                     f"请稍后重试，或改用软删除（不加 hard=true）")

    if not hard:
        with biz(commit=True) as cur:
            cur.execute("UPDATE item SET deleted = 1, status = 4 WHERE id = %s", (item_id,))
        cancelled = _cancel_pending_jobs(item_id, "商品已被下架，任务取消")
        return {"ok": True, "item_id": item_id, "mode": "soft",
                "cancelled_jobs": cancelled,
                "note": "软删除，可用 POST /items/{id}/restore 恢复"}

    with meta(commit=True) as cur:
        cur.execute("SELECT id FROM audit_jobs WHERE item_id = %s", (item_id,))
        job_ids = [int(r["id"]) for r in cur.fetchall()]
        if job_ids:
            ph = ",".join(["%s"] * len(job_ids))
            cur.execute(f"DELETE FROM audit_steps WHERE job_id IN ({ph})", job_ids)
            cur.execute(f"DELETE FROM checkpoints WHERE job_id IN ({ph})", job_ids)
        cur.execute("DELETE FROM review_queue WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM audit_jobs WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM audit_ground_truth WHERE item_id = %s", (item_id,))

    with biz(commit=True) as cur:
        cur.execute("DELETE FROM item_image WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM item_audit WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM report WHERE item_id = %s", (item_id,))
        cur.execute("DELETE FROM item WHERE id = %s", (item_id,))

    return {"ok": True, "item_id": item_id, "mode": "hard",
            "deleted_jobs": len(job_ids), "note": "物理删除，不可恢复"}


@app.post("/items/{item_id}/restore")
def restore_item(item_id: int):
    """恢复被软删除的商品：`deleted` 置回 0，状态回到「待审核」。

    不会自动重新投递审核 —— 想立刻重审就再调一次
    `POST /audits {"item_id": ..., "force": true}`。
    """
    with biz() as cur:
        cur.execute("SELECT id FROM item WHERE id = %s AND deleted = 1", (item_id,))
        if not cur.fetchone():
            raise HTTPException(404, f"商品 {item_id} 不存在，或它没有被软删除")
    with biz(commit=True) as cur:
        cur.execute("UPDATE item SET deleted = 0, status = 0 WHERE id = %s", (item_id,))
    return {"ok": True, "item_id": item_id, "status": 0}


@app.get("/items/{item_id}/detail")
def item_detail(item_id: int):
    """商品完整画像：业务信息 + 审核记录 + 最新任务 + 轨迹 + 复核状态 + 标注。"""
    d = M.item_detail(item_id)
    if not d:
        raise HTTPException(404, f"商品 {item_id} 不存在")
    return d


@app.post("/items/{item_id}/human-decide")
def human_decide(item_id: int, body: DecideIn, _: None = Depends(require_admin)):
    """人工裁决 —— 复核队列的出口，也是管理员改判的入口。

    两种场景：
      * 商品在复核队列里（AI 判了 REVIEW）→ 复核裁决，`item_audit.audit_source = 3`（AI+人工复核）
      * 商品已被 AI 审过（申诉 / 管理员介入）→ 人工改判，`audit_source = 1`（纯人工）

    关键：这里**不回写 `audit_jobs.verdict`**。那个字段保留 AI 的原始判断，
    这样评测指标衡量的始终是「AI 判得准不准」，人工介入不会污染它。
    """
    decision = (body.decision or "").upper()
    if decision not in ("APPROVE", "REJECT"):
        raise HTTPException(400, "decision 必须是 APPROVE 或 REJECT")

    with biz() as cur:
        cur.execute("SELECT id, title, status FROM item WHERE id = %s AND deleted = 0", (item_id,))
        item = cur.fetchone()
    if not item:
        raise HTTPException(404, f"商品 {item_id} 不存在")

    new_status = 1 if decision == "APPROVE" else 5
    audit_result = 1 if decision == "APPROVE" else 2
    remark = (body.remark or "").strip() or ("人工复核通过" if decision == "APPROVE" else "人工复核驳回")

    # 看这件商品是否来自复核队列。
    # 一次把该商品**所有**待处理行都结掉 —— 早期版本只结最新一条，
    # 重复审核堆积出来的旧记录会永远留在队列里。
    with meta(commit=True) as cur:
        cur.execute(
            """UPDATE review_queue
               SET status = 1, decision = %s, remark = %s, decided_by = %s, decided_at = NOW()
               WHERE item_id = %s AND status = 0""",
            (audit_result, remark[:500], body.reviewer_id, item_id),
        )
        resolved = cur.rowcount
        source = 3 if resolved else 1        # 3 = AI+人工复核, 1 = 纯人工

    with biz(commit=True) as cur:
        cur.execute("UPDATE item SET status = %s, version = version + 1 WHERE id = %s",
                    (new_status, item_id))
        cur.execute(
            """INSERT INTO item_audit
               (item_id, audit_user_id, audit_source, audit_result, audit_remark,
                rule_hits, confidence, submit_time, audit_time)
               VALUES (%s, %s, %s, %s, %s, NULL, NULL, NOW(), NOW())""",
            (item_id, body.reviewer_id, source, audit_result, remark),
        )

    return {
        "ok": True,
        "item_id": item_id,
        "title": item["title"],
        "decision": decision,
        "item_status": new_status,
        "audit_source": source,
        "audit_source_note": "AI + 人工复核" if source == 3 else "纯人工裁决",
        "resolved_review_rows": resolved,
        "remark": remark,
    }


@app.get("/review-queue")
def review_queue(status: int = Query(0, description="0 待处理 / 1 已处理 / -1 全部"),
                 limit: int = Query(30, ge=1, le=200)):
    """人工复核队列 —— 「宁可转人工也不误杀」的落地出口。"""
    clause = "" if status < 0 else "WHERE q.status = %s"
    args: list = [] if status < 0 else [status]
    with meta() as cur:
        cur.execute(
            f"""SELECT q.id, q.job_id, q.item_id, q.reason, q.confidence, q.status,
                       q.decision, q.remark, q.decided_at, q.created_at,
                       i.title, i.price, c.name AS category_name
                FROM review_queue q
                LEFT JOIN `{BIZ_DB}`.item i ON i.id = q.item_id
                LEFT JOIN `{BIZ_DB}`.item_category c ON c.id = i.category_id
                {clause}
                ORDER BY q.id DESC LIMIT %s""",
            args + [limit],
        )
        rows = cur.fetchall()
        cur.execute(
            """SELECT SUM(status = 0) AS pending,
                      SUM(status = 1 AND decision = 1) AS approved,
                      SUM(status = 1 AND decision = 2) AS rejected,
                      COUNT(*) AS total
               FROM review_queue"""
        )
        s = cur.fetchone() or {}
    for r in rows:
        if r.get("price") is not None:
            r["price"] = float(r["price"])
        if r.get("confidence") is not None:
            r["confidence"] = float(r["confidence"])
        if r.get("decided_at") is not None:
            r["decided_at"] = str(r["decided_at"])
        if r.get("created_at") is not None:
            r["created_at"] = str(r["created_at"])
    return {
        "summary": {k: int(s.get(k) or 0) for k in ("pending", "approved", "rejected", "total")},
        "threshold": REVIEW_THRESHOLD,
        "count": len(rows),
        "items": rows,
    }


@app.get("/progress")
def progress():
    """页面顶部进度：还有多少待审核、结果分布。"""
    p = M.progress()
    p["item_status_legend"] = ITEM_STATUS
    p["llm_enabled"] = LLM_ENABLED
    return p


# ============================================================
# 成本与可观测性
# ============================================================

@app.get("/budget")
def budget(days: int = Query(14, ge=1, le=90)):
    """LLM 护栏状态 + 每日用量历史。"""
    snap = guard.snapshot()
    snap["history"] = guard.daily_history(days)
    snap["guard_enabled"] = guard.guard_enabled()
    return snap


@app.get("/latency")
def latency(minutes: int = Query(60, ge=1, le=1440), sample: int = Query(3000, ge=100, le=20000)):
    """延迟分位数：整体，以及按阶段（load / rule / llm / writeback）拆分。"""
    return M.latency_stats(sample=sample, minutes=minutes)


@app.get("/agent")
def agent(minutes: int = Query(60, ge=1, le=1440)):
    """Agent 行为统计：工具调用次数分布、预算驳回次数、各工具使用频次。

    这是「模型是否真的在做取舍」的量化证据，也是调查预算的效果度量。
    """
    st = M.agent_stats(minutes=minutes)
    st["tool_budget"] = LLM_TOOL_BUDGET
    return st


@app.get("/metrics", include_in_schema=False)
def metrics():
    """Prometheus 文本格式指标。

    给人看的概览在 `/stats` 和 `/progress`；这里是给监控系统抓的。
    """
    p = M.progress()
    g = guard.snapshot()
    lines: list[str] = []

    def emit(name: str, help_: str, kind: str, samples: list[str]):
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(samples)

    emit("audit_jobs", "审核任务数（按状态）", "gauge",
         [f'audit_jobs{{status="{k}"}} {v}' for k, v in sorted(p["jobs"].items())])
    emit("audit_verdicts", "审核结论数（按结论）", "gauge",
         [f'audit_verdicts{{verdict="{k}"}} {v}' for k, v in sorted(p["verdicts"].items())])
    emit("audit_items", "商品数（按商品状态）", "gauge",
         [f'audit_items{{status="{k}"}} {v}' for k, v in sorted(p["items"].items())])
    emit("audit_review_queue_pending", "待人工复核数", "gauge",
         [f"audit_review_queue_pending {p['pending_review']}"])

    emit("llm_calls_today", "当日 LLM 调用次数", "gauge", [f"llm_calls_today {g['calls_today']}"])
    emit("llm_tokens_today", "当日 token 用量", "gauge", [
        f'llm_tokens_today{{direction="prompt"}} {g["prompt_tokens_today"]}',
        f'llm_tokens_today{{direction="completion"}} {g["output_tokens_today"]}',
    ])
    emit("llm_cost_today", "当日 LLM 花费", "gauge",
         [f'llm_cost_today{{currency="{g["currency"]}"}} {g["cost_today"]}'])
    emit("llm_budget_used_ratio", "日预算使用比例 0-1", "gauge",
         [f"llm_budget_used_ratio {round(g['budget_used_pct'] / 100.0, 6)}"])
    emit("llm_rate_limit_tokens", "令牌桶当前可用令牌数", "gauge",
         [f"llm_rate_limit_tokens {g['tokens_available']}"])
    emit("llm_degraded_today", "当日因护栏降级的次数", "gauge",
         [f"llm_degraded_today {g['degraded_today']}"])

    # ---- 延迟分位数（按 Prometheus summary 约定用 quantile 标签）----
    lat = M.latency_stats()
    j = lat["job"]
    emit("audit_job_duration_ms", "审核任务处理耗时（不含排队等待）", "summary", [
        f'audit_job_duration_ms{{quantile="0.5"}} {j["p50_ms"]}',
        f'audit_job_duration_ms{{quantile="0.95"}} {j["p95_ms"]}',
        f'audit_job_duration_ms{{quantile="0.99"}} {j["p99_ms"]}',
        f"audit_job_duration_ms_sum {j['avg_ms'] * j['count']}",
        f"audit_job_duration_ms_count {j['count']}",
    ])

    stage_samples = []
    for kind, st in lat["stages"].items():
        stage_samples.append(
            f'audit_stage_duration_ms{{stage="{kind}",quantile="0.5"}} {st["p50_ms"]}')
        stage_samples.append(
            f'audit_stage_duration_ms{{stage="{kind}",quantile="0.95"}} {st["p95_ms"]}')
    emit("audit_stage_duration_ms", "各阶段耗时（最近窗口，stage=load/rule/llm/writeback）",
         "summary", stage_samples or ["audit_stage_duration_ms_count 0"])

    # ---- Agent 行为：模型有没有在做工具取舍 ----
    ag = M.agent_stats()
    emit("agent_tool_calls", "工具调用次数（含终止调用 submit_verdict）", "summary", [
        f"agent_tool_calls_avg {ag['avg_tool_calls']}",
        f"agent_tool_calls_sum {ag['total_tool_calls']}",
        f"agent_tool_calls_count {ag['llm_jobs']}",
    ])
    emit("agent_query_calls", "真正的调查工具调用次数（不含 submit_verdict）", "summary", [
        f"agent_query_calls_avg {ag['avg_query_calls']}",
        f"agent_query_calls_sum {ag['query_calls']}",
    ])
    emit("agent_jobs_without_query", "只看基本信息、一次调查都没做的任务数", "gauge",
         [f"agent_jobs_without_query {ag['no_query_jobs']}"])
    emit("agent_budget_denied", "调查预算用完后仍试图调工具的累计次数", "gauge",
         [f"agent_budget_denied {ag['budget_denied']}"])
    emit("agent_tool_usage", "各工具被调用的次数", "gauge",
         [f'agent_tool_usage{{tool="{k}"}} {v}' for k, v in ag["tools"].items()]
         or ["agent_tool_usage_count 0"])

    return PlainTextResponse("\n".join(lines) + "\n",
                             media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/stats")
def stats():
    p = M.progress()
    return {
        "jobs": p["jobs"],
        "verdicts": p["verdicts"],
        "pending_review": p["pending_review"],
        "item_status": p["items"],
        "item_status_legend": ITEM_STATUS,
    }


# ============================================================
# 评测
# ============================================================

@app.post("/eval/run")
def run_eval(tag: str = Query(None, description="轮次标签，留空自动生成")):
    """跑一轮评测并入库，返回指标。"""
    m = M.compute_metrics()
    if not m:
        raise HTTPException(400, "还没有已完成的审核任务，先提交审核")
    tag = tag or f"run-{datetime.now():%m%d-%H%M%S}"
    M.save_run(tag, m)
    return {"tag": tag, **m}


@app.get("/eval/runs")
def eval_runs(limit: int = Query(20, ge=1, le=100)):
    """历次评测，用于轮次对比。"""
    return M.list_runs(limit)


# ============================================================
# 演示辅助
# ============================================================

@app.post("/admin/reset")
def admin_reset(body: ResetIn, _: None = Depends(require_admin)):
    """重置演示数据：商品回到待审核、清空任务与轨迹、保留标注答案。"""
    with biz(commit=True) as cur:
        cur.execute("UPDATE item SET status = 0, version = 0 WHERE deleted = 0")
        cur.execute("DELETE FROM item_audit")
    with meta(commit=True) as cur:
        for t in ("audit_steps", "checkpoints", "review_queue", "audit_jobs"):
            cur.execute(f"DELETE FROM {t}")
        if body.clear_evals:
            cur.execute("DELETE FROM eval_runs")
    return {"ok": True, "cleared_evals": body.clear_evals}


@app.get("/", include_in_schema=False)
def index():
    """演示页面。本项目定位是后端服务，页面只是为了让效果可视化。"""
    return FileResponse(STATIC_DIR / "index.html")

"""Agent 的工具集。

设计原则（每个工具都必须满足）：
    1. 有超时 / 有界返回，绝不让一次调用拖垮整个任务
    2. 出错返回错误字符串而不是抛异常 —— 错误要回灌给模型，让它自己纠正
    3. 结果截断，避免把大段文本塞进上下文烧 token

这些工具是给 LLM 用的（tool calling），但规则层与人工复核也能直接复用，
所以它们不依赖 LLM 存在。
"""
from __future__ import annotations

import json

from app.db import biz
from app.rules import evaluate

MAX_ROWS = 10          # 相似商品最多返回几条
MAX_TEXT = 300         # 单个文本字段截断长度


def _cut(text, n: int = MAX_TEXT) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[:n] + "…"


# ============================================================
# 工具实现
# ============================================================

def get_item_detail(item_id: int) -> dict:
    """商品完整信息 + 分类名 + 图片数。"""
    with biz() as cur:
        cur.execute(
            """SELECT i.*, c.name AS category_name
               FROM item i LEFT JOIN item_category c ON c.id = i.category_id
               WHERE i.id = %s AND i.deleted = 0""",
            (item_id,),
        )
        row = cur.fetchone()
        if not row:
            return {"error": f"商品 {item_id} 不存在"}
        cur.execute("SELECT COUNT(*) AS n FROM item_image WHERE item_id = %s AND deleted = 0", (item_id,))
        row["image_count"] = cur.fetchone()["n"]
    row["description"] = _cut(row.get("description"), 600)
    row["price"] = float(row["price"])
    row["original_price"] = float(row["original_price"]) if row.get("original_price") is not None else None
    return row


def get_seller_credit(seller_id: int) -> dict:
    """卖家信用画像：当前分、近期变动、历史被驳回商品数。"""
    with biz() as cur:
        cur.execute(
            "SELECT id, nickname, credit_score, status FROM sys_user WHERE id = %s AND deleted = 0",
            (seller_id,),
        )
        user = cur.fetchone()
        if not user:
            return {"error": f"用户 {seller_id} 不存在"}
        cur.execute(
            """SELECT change_score, before_score, after_score, reason, create_time
               FROM credit_record WHERE user_id = %s ORDER BY id DESC LIMIT 5""",
            (seller_id,),
        )
        user["recent_credit_changes"] = cur.fetchall()
        cur.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status = 5 THEN 1 ELSE 0 END) AS rejected
               FROM item WHERE seller_id = %s AND deleted = 0""",
            (seller_id,),
        )
        stat = cur.fetchone()
    user["published_total"] = int(stat["total"] or 0)
    user["rejected_total"] = int(stat["rejected"] or 0)
    return user


def get_report_history(item_id: int | None = None, seller_id: int | None = None) -> dict:
    """举报记录。item_id 与 seller_id 至少给一个。"""
    if item_id is None and seller_id is None:
        return {"error": "item_id 与 seller_id 至少要提供一个"}
    with biz() as cur:
        if item_id is not None:
            cur.execute(
                """SELECT r.id, r.reason, r.status, r.create_time
                   FROM report r WHERE r.item_id = %s AND r.deleted = 0
                   ORDER BY r.id DESC LIMIT %s""",
                (item_id, MAX_ROWS),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) AS n FROM report WHERE item_id = %s AND status = 0 AND deleted = 0",
                (item_id,),
            )
            return {"item_id": item_id, "pending_count": cur.fetchone()["n"], "reports": rows}
        cur.execute(
            """SELECT r.id, r.item_id, r.reason, r.status, r.create_time
               FROM report r JOIN item i ON i.id = r.item_id
               WHERE i.seller_id = %s AND r.deleted = 0
               ORDER BY r.id DESC LIMIT %s""",
            (seller_id, MAX_ROWS),
        )
        rows = cur.fetchall()
        return {"seller_id": seller_id, "report_count": len(rows), "reports": rows}


HYBRID_W_TEXT = 0.7       # 词法相关度权重
HYBRID_W_CATEGORY = 0.2   # 同分类权重
HYBRID_W_PRICE = 0.1      # 价格接近度权重
MIN_RELEVANCE = 1.0       # 全文相关度下限，低于它算噪声（ngram 只匹配到单个字的那些）
REL_MIN_RATIO = 0.25      # 相对阈值：低于最高分这个比例的算"只共享个别字"的长尾噪声


def _price_similarity(a: float, b: float) -> float:
    if not a or not b:
        return 0.5
    return max(0.0, 1.0 - abs(a - b) / max(a, b))


def _audit_rules(item_ids: list[int]) -> tuple[dict, dict]:
    """查出这些商品被驳回时命中的规则。

    返回 `(规则命中总次数, {item_id: [rule_id, ...]})`。

    为什么连**每一条**的规则都要返回：**文本相似 ≠ 违规情形相同**。
    「学生证卡套」和「学生证 可代买学生票」共享"学生证"三个字，
    但一个是正常配件、一个是违规服务。只有把各自的驳回规则摆出来，
    模型才能自己判断能不能类比 —— 只给个总数会把它推向误杀。
    """
    if not item_ids:
        return {}, {}
    ph = ",".join(["%s"] * len(item_ids))
    counts: dict[str, int] = {}
    per_item: dict[int, list[str]] = {}
    with biz() as cur:
        cur.execute(
            f"""SELECT item_id, rule_hits FROM item_audit
                WHERE item_id IN ({ph}) AND rule_hits IS NOT NULL""",
            item_ids,
        )
        for row in cur.fetchall():
            hits = row["rule_hits"]
            if isinstance(hits, str):
                try:
                    hits = json.loads(hits)
                except (json.JSONDecodeError, TypeError):
                    hits = []
            ids = []
            for h in hits or []:
                if isinstance(h, dict):
                    rid = h.get("rule_id") or h.get("rule")
                    if rid:
                        ids.append(str(rid))
                        counts[str(rid)] = counts.get(str(rid), 0) + 1
            if ids:
                per_item[int(row["item_id"])] = sorted(set(ids))
    return (dict(sorted(counts.items(), key=lambda kv: -kv[1])), per_item)


def search_similar_items(item_id: int | None = None, query: str | None = None,
                         category_id: int | None = None, limit: int = 8) -> dict:
    """混合检索：ngram 全文相关度 + 分类/价格结构化重排。

    这是审核场景里"这个表述在平台上常见吗？类似商品怎么判的？"的证据来源。

    为什么用 MySQL 的 ngram 全文索引而不是向量检索：
      - 向量检索要额外的 embedding 服务（多一个部署依赖、多一份调用成本），
        而这个项目刻意做到「除 LLM 外零外部依赖」
      - MySQL 8 内置 ngram 解析器，专为中日韩设计（按 2-gram 切分），
        相关度评分对本场景完全够用 —— 实测「学生证卡套」得 27.5 分，
        只共享一个字的噪声项只有 2.3 分，区分度是够的
      - 真要上向量，接口形状不用变，换掉召回层即可

    召回（词法）→ 重排（结构化）→ 聚合（结论）三步：
      1. 召回：CONTINUE MATCH...AGAINST 拿文本相关度
      2. 重排：0.7×相关度 + 0.2×同分类 + 0.1×价格接近度
      3. 聚合：相似商品里几件通过/几件驳回、驳回命中了哪些规则、以及一句解读提示
    """
    limit = max(1, min(int(limit or 8), MAX_ROWS))

    base = {}
    if item_id:
        with biz() as cur:
            cur.execute(
                """SELECT id, title, description, category_id, price
                   FROM item WHERE id = %s AND deleted = 0""",
                (int(item_id),),
            )
            base = cur.fetchone() or {}
        if not base:
            return {"error": f"商品 {item_id} 不存在"}

    qtext = _cut(query, 200) if query else ""
    if not qtext and base:
        qtext = f"{base.get('title') or ''} {base.get('description') or ''}"[:300]
    if not qtext.strip():
        return {"error": "需要 item_id 或 query 之一，且检索文本不能为空"}

    cid = category_id or base.get("category_id")
    base_price = float(base["price"]) if base.get("price") is not None else 0.0
    exclude = int(item_id or 0)

    # ---- 1. 词法召回（ngram 全文）----
    with biz() as cur:
        cur.execute(
            """SELECT i.id, i.title, i.status, i.price, i.category_id,
                      c.name AS category_name,
                      MATCH(i.title, i.description)
                          AGAINST (%s IN NATURAL LANGUAGE MODE) AS text_score
               FROM item i
               LEFT JOIN item_category c ON c.id = i.category_id
               WHERE i.deleted = 0 AND i.status IN (1, 5) AND i.id <> %s
                 AND MATCH(i.title, i.description)
                     AGAINST (%s IN NATURAL LANGUAGE MODE) >= %s
               ORDER BY text_score DESC
               LIMIT %s""",
            (qtext, exclude, qtext, MIN_RELEVANCE, limit * 4),
        )
        rows = cur.fetchall()

    # ---- 兜底：全文没命中就退化为同分类最近审核过的商品，并如实标注 ----
    fallback = False
    if not rows and cid:
        fallback = True
        with biz() as cur:
            cur.execute(
                """SELECT i.id, i.title, i.status, i.price, i.category_id,
                          c.name AS category_name, 0 AS text_score
                   FROM item i
                   LEFT JOIN item_category c ON c.id = i.category_id
                   WHERE i.deleted = 0 AND i.status IN (1, 5)
                     AND i.id <> %s AND i.category_id = %s
                   ORDER BY i.update_time DESC LIMIT %s""",
                (exclude, cid, limit),
            )
            rows = cur.fetchall()

    # ---- 相对阈值：低于最高分 25% 的属于"只共享个别字"的长尾噪声，丢掉 ----
    if rows and not fallback:
        top = max(float(r["text_score"]) for r in rows)
        floor = max(top * REL_MIN_RATIO, MIN_RELEVANCE)
        rows = [r for r in rows if float(r["text_score"]) >= floor]

    # ---- 2. 结构化重排 ----
    max_text = max((float(r["text_score"]) for r in rows), default=1.0) or 1.0
    for r in rows:
        text_n = float(r["text_score"]) / max_text
        same_cat = 1.0 if (cid and r["category_id"] == cid) else 0.0
        price_sim = _price_similarity(base_price, float(r["price"])) if base_price else 0.5
        r["similarity"] = round(HYBRID_W_TEXT * text_n + HYBRID_W_CATEGORY * same_cat
                                + HYBRID_W_PRICE * price_sim, 3)
        r["text_score"] = round(float(r["text_score"]), 2)
        r["verdict"] = "通过" if r["status"] == 1 else "驳回"
        r["title"] = _cut(r["title"], 60)
        r["price"] = float(r["price"])
        r["id"] = int(r["id"])
        r["category_id"] = int(r["category_id"]) if r["category_id"] else None
        r.pop("status", None)

    # ★ 必须先转成 list 再 sort。
    #
    # PyMySQL 的 fetchall() 返回的是 **tuple**，tuple 没有 .sort()。
    # 上面那条「相对阈值过滤」只在**查到了结果**时执行，才会把 rows 变成 list；
    # 所以「一条都没查到」时 rows 还是个空 tuple，这里直接
    #   AttributeError: 'tuple' object has no attribute 'sort'
    #
    # 这个 bug 本地一直没暴露：本地库数据多，全文检索从没返回过空。
    # CI 用全新的种子库，一查就空，立刻炸出「检索不到」这条路径 ——
    # 而那恰恰是必须正常工作的一条（要如实告诉模型「孤立表述」，不能报错）。
    rows = list(rows)
    if not fallback:
        rows.sort(key=lambda r: -r["similarity"])
    rows = rows[:limit]

    # ---- 3. 聚合出结论 ----
    reasons, per_item = _audit_rules([r["id"] for r in rows])
    for r in rows:
        r["rejected_rules"] = per_item.get(r["id"], [])
    rejected = sum(1 for r in rows if r["verdict"] == "驳回")

    # 关键一步：把「参照物被驳回的规则」和「本商品自己命中的规则」比一比。
    # 工具多花一次本地查库（不花 token），就能替模型完成相关性判断 ——
    # 否则模型看到"7 件被驳回"很容易直接往违规上靠，而那是误杀。
    # 例：「学生证卡套」和「学生证代买学生票」共享关键词，但前者是配件，
    # 参照物的 R-BANNED_GOODS 与它无关，必须明确告诉模型"别据此驳回"。
    own_rules: set[str] = set()
    if item_id:
        try:
            cr = check_rule(int(item_id))
            own_rules = {h.get("rule_id") for h in (cr.get("hits") or []) if h.get("rule_id")}
        except Exception:  # noqa: BLE001 —— 比对失败不影响检索本身
            own_rules = set()
    overlap = sorted(set(reasons) & own_rules)

    if fallback:
        note = ("全文检索没有找到文本相似的商品，下面退化为**同分类最近审核过**的商品，"
                "只能当弱参考 —— **不能**据此认为该表述常见或罕见。")
    elif not rows:
        note = ("平台上检索不到相似商品。这属于**孤立表述**：既不能因为『别人都这么写』"
                "就放行，也不能仅仅因为陌生就判违规。")
    elif rejected and overlap:
        top = "、".join(f"{k}({v}件)" for k, v in list(reasons.items())[:3])
        note = (f"检索到 {len(rows)} 件相似商品，其中 {rejected} 件被驳回，驳回规则：{top}。"
                f"其中 **{'、'.join(overlap)} 与本商品命中的规则一致** —— "
                f"这是判定违规的**有力佐证**。")
    elif rejected and own_rules:
        note = (f"检索到 {len(rows)} 件相似商品，其中 {rejected} 件被驳回，驳回规则："
                f"{'、'.join(reasons)}。但本商品命中的是 {'、'.join(sorted(own_rules))}，"
                f"**两者没有重叠** —— 这些参照物属于「共享关键词但违规情形不同」，"
                f"**不构成**本商品的违规依据，不要据此驳回。")
    elif rejected:
        note = (f"检索到 {len(rows)} 件相似商品，其中 {rejected} 件被驳回，驳回规则："
                f"{'、'.join(reasons) or '无明细'}。本商品规则层**没有命中**，"
                f"这些参照物的违规情形与本次疑点是否相同，需要你自己判断；"
                f"若只是共享关键词而用途不同（配件、耗材之类），不构成违规依据。")
    else:
        note = (f"检索到 {len(rows)} 件相似商品，**全部判为通过** —— "
                f"说明该表述在平台上是常见的正常写法，倾向于不构成违规。")

    return {
        "query": _cut(qtext, 80),
        "matched": len(rows),
        "approved": len(rows) - rejected,
        "rejected": rejected,
        "rejected_rules": reasons,
        "own_rules": sorted(own_rules),
        "rule_overlap": overlap,
        "fallback": fallback,
        "samples": rows,
        "note": note,
    }


def check_rule(item_id: int) -> dict:
    """对指定商品重跑规则引擎，返回命中明细。"""
    item = get_item_detail(item_id)
    if "error" in item:
        return item
    with biz() as cur:
        cur.execute("SELECT * FROM sys_user WHERE id = %s", (item["seller_id"],))
        seller = cur.fetchone() or {}
        cur.execute(
            "SELECT COUNT(*) AS n FROM report WHERE item_id = %s AND status = 0 AND deleted = 0",
            (item_id,),
        )
        pending = cur.fetchone()["n"]
    rr = evaluate(item, item.get("category_name") or "", seller, pending)
    return {
        "blocked": rr.blocked,
        "needs_second_layer": rr.needs_second_layer,
        "hits": [h.as_dict() for h in rr.hits],
        "signals": rr.signals,
    }


# ============================================================
# 注册表：给 LLM 用的 schema + 分发表
# ============================================================

DISPATCH = {
    "get_item_detail": get_item_detail,
    "get_seller_credit": get_seller_credit,
    "get_report_history": get_report_history,
    "search_similar_items": search_similar_items,
    "check_rule": check_rule,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_item_detail",
            "description": "获取商品的完整信息，包括标题、描述、价格、分类、图片数量。",
            "parameters": {
                "type": "object",
                "properties": {"item_id": {"type": "integer", "description": "商品 ID"}},
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_seller_credit",
            "description": "获取卖家信用画像：信用分、近期变动记录、历史发布与被驳回数量。注意：信用分低不等于商品违规，只用于调整审核严格度。",
            "parameters": {
                "type": "object",
                "properties": {"seller_id": {"type": "integer", "description": "卖家用户 ID"}},
                "required": ["seller_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_report_history",
            "description": "查询举报记录。给出 item_id 查该商品被举报情况，给出 seller_id 查该卖家名下商品的举报情况。",
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer", "description": "商品 ID"},
                    "seller_id": {"type": "integer", "description": "卖家用户 ID"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_similar_items",
            "description": (
                "检索平台上**文本相似**的已审核商品，用来判断某个表述是否常见、"
                "类似商品当时是怎么判的。"
                "传 item_id 会用该商品的标题+描述做检索；也可以直接传 query 检索某个可疑词。"
                "返回里含「相似商品被驳回时命中的规则」和一句解读提示，是判定的重要佐证。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer",
                                "description": "要检索相似商品的商品 ID（推荐，会自动取它的标题+描述）"},
                    "query": {"type": "string",
                              "description": "直接用一段文本检索，例如某个可疑词"},
                    "category_id": {"type": "integer",
                                    "description": "限定分类（可选，默认与 item_id 同分类）"},
                    "limit": {"type": "integer", "description": "返回条数，上限 10"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_rule",
            "description": "对商品重跑规则引擎，返回命中的规则明细。",
            "parameters": {
                "type": "object",
                "properties": {"item_id": {"type": "integer", "description": "商品 ID"}},
                "required": ["item_id"],
            },
        },
    },
]


def call_tool(name: str, args: dict) -> str:
    """执行工具并把结果序列化成字符串（错误也序列化，交给模型自己纠正）。"""
    fn = DISPATCH.get(name)
    if fn is None:
        return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)
    try:
        result = fn(**(args or {}))
    except TypeError as e:
        return json.dumps({"error": f"参数错误: {e}"}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 —— 工具异常必须回灌，不能中断循环
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    out = json.dumps(result, ensure_ascii=False, default=str)
    return out if len(out) <= 4000 else out[:4000] + "…(已截断)"

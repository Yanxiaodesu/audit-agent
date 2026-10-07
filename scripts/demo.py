"""同步演示：逐件展示审核决策链，不需要起 API 和 worker。

挑若干有代表性的商品，直接调用审核逻辑，打印：
    规则命中了什么 -> 信号 -> 最终结论 -> 依据
一眼看清「规则层 + 分流」是怎么工作的。

用法：
    python scripts/demo.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymysql  # noqa: E402
from pymysql.cursors import DictCursor  # noqa: E402

from app.config import BIZ_DB, DB_CONFIG, LLM_ENABLED, META_DB  # noqa: E402
from agent import audit as audit_mod  # noqa: E402

ICON = {"APPROVE": "[通过]", "REJECT": "[驳回]", "REVIEW": "[转人工]"}


def connect(db=None):
    cfg = {k: v for k, v in DB_CONFIG.items() if k != "database"}
    if db:
        cfg["database"] = db
    return pymysql.connect(cursorclass=DictCursor, **cfg)


def find_items():
    """挑有代表性的商品：先按标题关键词，找不到再按标注类型兜底。"""
    conn = connect(META_DB)
    picked, seen = [], set()
    try:
        with conn.cursor() as cur:
            def add(item_id, title, label):
                if item_id in seen:
                    return
                seen.add(item_id)
                picked.append((item_id, title, label))

            def by_title(kw, label):
                # 标题或描述里出现即算命中（很多违规特征写在描述里）
                cur.execute(
                    f"""SELECT id, title FROM `{BIZ_DB}`.item
                        WHERE title LIKE %s OR IFNULL(description,'') LIKE %s LIMIT 1""",
                    (f"%{kw}%", f"%{kw}%"))
                r = cur.fetchone()
                if r:
                    add(r["id"], r["title"], label)

            # 挑一件高信用卖家的正常商品，用来展示「自动通过」这条路径
            cur.execute(
                f"""SELECT i.id, i.title FROM `{BIZ_DB}`.item i
                    JOIN `{BIZ_DB}`.sys_user u ON u.id = i.seller_id
                    JOIN `{META_DB}`.audit_ground_truth g ON g.item_id = i.id
                    WHERE g.expected='APPROVE' AND g.violation_type IS NULL
                      AND u.credit_score >= 80 AND i.category_id = 2
                    LIMIT 1""")
            r = cur.fetchone()
            if r:
                add(r["id"], r["title"], "正常商品（卖家信用高，应自动通过）")

            by_title("配套习题答案", "教辅含「答案」——教材类目下属正常，应放行")
            by_title("仓鼠笼子", "⚠ 误伤样本：正常配件，但含违禁词子串")
            by_title("白酒杯", "⚠ 误伤样本：正常杯子，但含违禁词子串")
            by_title("电子烟", "违禁品")
            by_title("论文代写", "学术作弊")
            by_title("刷单兼职", "垃圾广告")
            by_title("加微信", "联系方式外露（灰区）")

            cur.execute(
                f"""SELECT g.item_id, i.title FROM `{META_DB}`.audit_ground_truth g
                    JOIN `{BIZ_DB}`.item i ON i.id = g.item_id
                    WHERE g.expected='REVIEW' AND g.violation_type='FAKE_INFO'
                      AND i.price > i.original_price LIMIT 1""")
            r = cur.fetchone()
            if r:
                add(r["item_id"], r["title"], "价格异常：售价比原价还高（灰区）")

            cur.execute(
                f"""SELECT g.item_id, i.title FROM `{META_DB}`.audit_ground_truth g
                    JOIN `{BIZ_DB}`.item i ON i.id = g.item_id
                    WHERE g.expected='REVIEW' AND g.violation_type='FAKE_INFO'
                      AND (i.description IS NULL OR CHAR_LENGTH(i.description) < 10) LIMIT 1""")
            r = cur.fetchone()
            if r:
                add(r["item_id"], r["title"], "描述缺失（灰区）")
    finally:
        conn.close()
    return picked


def main():
    items = find_items()
    if not items:
        print("[!] 没有数据，请先运行：python scripts/init_db.py && python scripts/seed_data.py")
        return 1

    print("=" * 80)
    print("  商品合规审核 Agent —— 决策链演示")
    print(f"  LLM 层：{'已启用' if LLM_ENABLED else '未启用（审核走「规则引擎 + 人工复核」）'}")
    print("=" * 80)

    counts = {"APPROVE": 0, "REJECT": 0, "REVIEW": 0}
    for item_id, title, label in items:
        conn = connect(META_DB)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO audit_jobs (item_id, status) VALUES (%s, 'running')", (item_id,))
                job_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()

        try:
            result = audit_mod.audit(item_id, job_id)
        except Exception as e:  # noqa: BLE001
            print(f"\nitem#{item_id} 审核失败: {type(e).__name__}: {e}")
            continue

        counts[result["verdict"]] += 1
        print(f"\nitem#{item_id:<4} {title[:52]}")
        print(f"        场景    ：{label}")

        hits = result.get("rule_hits") or []
        if hits:
            for h in hits:
                print(f"        规则命中：[{h['severity']:<6}] {h['rule_id']:<20} {h['detail']}")
        else:
            print("        规则命中：无")

        sig = result.get("signals") or {}
        print(f"        信号    ：卖家信用 {sig.get('seller_credit')}，未处理举报 {sig.get('report_count')}")
        print(f"        {ICON[result['verdict']]} 结论 {result['verdict']}"
              f"（置信度 {result['confidence']}，来源 {result['source']}）")
        print(f"        依据    ：{result['reason']}")

    print()
    print("=" * 80)
    print(f"  本轮统计：通过 {counts['APPROVE']}   驳回 {counts['REJECT']}   转人工 {counts['REVIEW']}")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

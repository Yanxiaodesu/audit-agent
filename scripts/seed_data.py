"""造测试数据：200 件商品，带「理想结论」标注。

数据构成（刻意设计，用来暴露规则引擎的真实强弱项）：

    137 件 APPROVE   合规商品，理想情况应放行
    30 件 REJECT     明确违规：违禁品 / 学术作弊 / 垃圾广告
    33 件 REVIEW     灰区：联系方式外露 / 价格异常 / 描述缺失

其中 APPROVE 里有 6 件是**故意设置的误伤样本**：
「仓鼠笼子」「学生证卡套」「白酒杯」这类正常配件，因为包含违禁词子串
会被朴素的字符串匹配规则误判为 BLOCK。这是关键词规则的真实缺陷，
留着它是为了能演示「发现问题 → 改规则 → 指标改善」这个闭环。

理想结论写进 audit_ground_truth，评测脚本据此计算：
    违规召回率 / 误杀率 / 自动处理率 / 一致率
"""
from __future__ import annotations

import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymysql  # noqa: E402
from pymysql.cursors import DictCursor  # noqa: E402

from app.config import BIZ_DB, DB_CONFIG, META_DB  # noqa: E402

RNG = random.Random(20261007)      # 固定种子，保证可复现

CATEGORIES = ["教材书籍", "数码电子", "生活用品", "运动户外", "服饰鞋包", "美妆个护", "乐器文具"]

PRODUCTS = {
    "教材书籍": ["高等数学（第七版）上册 同济大学", "线性代数 同济第六版", "概率论与数理统计 浙大版",
                 "大学物理实验教程 第三版", "C程序设计 谭浩强 第四版", "数据结构 C语言版 严蔚敏",
                 "计算机网络 自顶向下方法", "操作系统概念 第九版", "离散数学及其应用",
                 "大学英语综合教程 第三册"],
    "数码电子": ["iPad Air 5 64G 深空灰", "罗技 G304 无线鼠标", "小米手环 8 NFC版",
                 "索尼 WH-1000XM4 头戴耳机", "高斯 GS87C 机械键盘 红轴", "罗技 C920 摄像头",
                 "树莓派 4B 8G 套装", "Kindle Paperwhite 4", "JBL GO3 蓝牙音箱",
                 "西部数据 移动硬盘 1T"],
    "生活用品": ["小米米家 LED 台灯", "大号收纳箱 两个装", "美的电热水壶 1.5L",
                 "折叠床上书桌", "小熊加湿器 4L", "落地晾衣架", "长柄自动雨伞",
                 "膳魔师保温杯 500ml", "宿舍用小风扇 静音"],
    "运动户外": ["斯伯丁篮球 7号", "尤尼克斯羽毛球拍 对拍", "加厚瑜伽垫 10mm",
                 "亚瑟士跑步鞋 42码", "捷安特 ATX 山地车", "可调节哑铃 20kg",
                 "负重跳绳 专业款", "登山包 40L 防泼水"],
    "服饰鞋包": ["优衣库羽绒服 男 L码", "卫衣 全新带吊牌", "小米 90分 双肩包",
                 "匡威帆布鞋 41码", "李维斯牛仔裤 32腰", "卡其色风衣 M码",
                 "棒球帽 全新"],
    "美妆个护": ["戴森吹风机 HD08", "飞利浦电动牙刷 HX3", "带LED灯化妆镜",
                 "卡诗护发精油 全新", "芙丽芳丝洗面奶 未开封", "卷发棒 陶瓷涂层"],
    "乐器文具": ["雅马哈 F310 吉他", "23寸尤克里里", "卡西欧 CT-S300 电子琴",
                 "凌美恒星钢笔 F尖", "百乐 P500 中性笔 一盒", "A4加厚素描本"],
}

CONDITIONS = ["九成新", "九五新", "八成新", "几乎全新", "七成新"]
PLACES = ["东区宿舍楼下", "西区食堂门口", "图书馆南门", "三教门口", "体育馆旁边", "北苑宿舍区"]

DESC_TEMPLATES = [
    "自用{cond}，功能一切正常，因为换新所以出掉。{place}面交优先，可以当面验货。",
    "大二买的，用了不到一学期，{cond}，原包装盒还在。{place}见。",
    "闲置很久了，一直放着没用，{cond}，诚心要的来。{place}交易。",
    "毕业清宿舍，东西都挺好，{cond}，价格好商量。{place}自提。",
    "买回来用得很少，{cond}，功能完好无磕碰。{place}面交，支持验货后再付款。",
    "室友搬走了留下的，我帮他出，{cond}，具体成色可以看图。{place}交易。",
]

# ------------------------------------------------------------
# 明确违规（期望 REJECT）
# ------------------------------------------------------------
BLOCK_SAMPLES = [
    # (分类, 标题, 描述, 违规类型)
    ("生活用品", "全新电子烟 未拆封 多种口味", "朋友店里拿的货，比外面便宜不少，需要的同学来拿。", "BANNED_GOODS"),
    ("生活用品", "管制刀具 收藏级 未开刃", "收藏用的，做工精细，毕业了带不走。", "BANNED_GOODS"),
    ("数码电子", "仿真枪 1:1 金属材质 可拆卸", "纯收藏，做工很好，细节到位。", "BANNED_GOODS"),
    ("生活用品", "仓鼠一只 附带笼子和粮", "养了两个月，性格温顺，因为要搬宿舍没法继续养。", "BANNED_GOODS"),
    ("教材书籍", "学生证 可代买学生票", "多出来的一本，有需要的可以来拿。", "BANNED_GOODS"),
    ("美妆个护", "处方药 安眠药 剩半盒", "之前失眠开的，现在好了用不上。", "BANNED_GOODS"),
    ("生活用品", "白酒 两瓶 未开封", "家里带来的，我不喝酒，转给需要的。", "BANNED_GOODS"),
    ("数码电子", "银行卡 全新未激活", "办卡送的，一直没用过。", "BANNED_GOODS"),
    ("运动户外", "电击器 防身用 带充电器", "晚上出门防身用的，现在不用了。", "BANNED_GOODS"),
    ("服饰鞋包", "香烟 整条 便宜出", "别人送的，我不抽烟，低价处理。", "BANNED_GOODS"),

    ("教材书籍", "论文代写 各专业都接", "包过，不满意可以退款，长期有效。", "ACADEMIC_FRAUD"),
    ("教材书籍", "代考服务 期末考试", "价格可谈，安全可靠，有需要的私信。", "ACADEMIC_FRAUD"),
    ("数码电子", "网课代刷 包过", "各类网课都能刷，效率高，价格便宜。", "ACADEMIC_FRAUD"),
    ("教材书籍", "毕业论文代写 查重包过", "本硕都能写，多年经验，先看稿后付款。", "ACADEMIC_FRAUD"),
    ("教材书籍", "替考 四六级 保过", "有需要的提前联系，名额有限。", "ACADEMIC_FRAUD"),
    ("教材书籍", "作业代做 一科五十", "理工科文科都接，当天出稿。", "ACADEMIC_FRAUD"),
    ("数码电子", "代刷网课 全自动", "挂机就行，不用管，包过。", "ACADEMIC_FRAUD"),
    ("乐器文具", "论文代写 加急", "三天出稿，包查重通过。", "ACADEMIC_FRAUD"),
    ("教材书籍", "保过班内部资料 包过", "配合代考使用，效果很好。", "ACADEMIC_FRAUD"),
    ("生活用品", "刷课代做 各平台都行", "便宜出，量大优惠。", "ACADEMIC_FRAUD"),

    ("生活用品", "刷单兼职 日结", "在家就能做，一天几百块，多劳多得。", "SPAM"),
    ("数码电子", "校园贷 无抵押 当天放款", "学生也能办，手续简单，额度高。", "SPAM"),
    ("服饰鞋包", "博彩平台代理招募", "高返水，稳定可靠，有兴趣聊聊。", "SPAM"),
    ("生活用品", "返利群 扫码进群", "每天都有福利，进群就能领。", "SPAM"),
    ("数码电子", "无抵押贷款 秒到账", "不看征信，当天放款，学生可办。", "SPAM"),
    ("生活用品", "兼职日结 无需经验", "一天两三百，时间自由，适合学生。", "SPAM"),
    ("服饰鞋包", "外围代理招募 待遇好", "有意向的详聊，非诚勿扰。", "SPAM"),
    ("美妆个护", "刷单返利 一单十块", "简单好做，日结不拖欠。", "SPAM"),
    ("乐器文具", "代理招募 扫码进群", "零门槛，多劳多得。", "SPAM"),
    ("运动户外", "校园贷 手续简单", "当天到账，学生可办，额度高。", "SPAM"),
]

# ------------------------------------------------------------
# 灰区（期望 REVIEW）
# ------------------------------------------------------------
CONTACT_SAMPLES = [
    "有意者加微信 abc12345，备注买东西。",
    "联系方式：13812345678，白天上课可能接不到。",
    "QQ：987654321 详聊，站内消息不常看。",
    "加微信 xiaoming_2024 发实拍图。",
    "手机 13998887777，随时可以看货。",
    "加微信 vx889900 谈价格。",
    "QQ 123456789 联系，价格好说。",
    "有意加微信 zhangsan001 私聊。",
    "电话 13700001111，晚上方便接。",
    "加微信 lisi_2023 看视频。",
    "QQ：556677889 发细节图。",
    "微信 wangwu2020，加的时候说下要哪个。",
    "手机号 13655554444，短信也行。",
    "加微信 chenliu88，可以小刀。",
    "QQ 998877665，白天在线。",
]

PRICE_SAMPLES = [
    ("生活用品", "小米米家 LED 台灯", 199.00, 129.00, "买的时候一百多，现在急用钱，你出个价。"),
    ("数码电子", "罗技 G304 无线鼠标", 299.00, 149.00, "用了半年，包装还在，比原价还贵是因为包含接收器。"),
    ("运动户外", "斯伯丁篮球 7号", 399.00, 158.00, "打过几次，成色很好。"),
    ("服饰鞋包", "匡威帆布鞋 41码", 0.00, 369.00, "白送，自己来拿就行。"),
    ("乐器文具", "雅马哈 F310 吉他", 1599.00, 899.00, "手感很好，因为琴太多出掉一把。"),
    ("美妆个护", "戴森吹风机 HD08", 3999.00, 2999.00, "正品，用了三次，包装齐全。"),
    ("教材书籍", "数据结构 C语言版 严蔚敏", 199.00, 39.00, "考研用过的，笔记很全。"),
    ("生活用品", "膳魔师保温杯 500ml", 0.00, 259.00, "搬家带不走，谁要谁拿。"),
    ("数码电子", "Kindle Paperwhite 4", 1299.00, 699.00, "看得少，几乎全新。"),
    ("运动户外", "捷安特 ATX 山地车", 2999.00, 1299.00, "骑了一年，刚保养过。"),
]

DESC_MISSING_SAMPLES = [
    ("生活用品", "大号收纳箱 两个装", ""),
    ("数码电子", "小米手环 8 NFC版", "。"),
    ("教材书籍", "大学物理实验教程 第三版", "全新"),
    ("运动户外", "加厚瑜伽垫 10mm", "便宜出"),
    ("服饰鞋包", "小米 90分 双肩包", "九成新"),
    ("美妆个护", "带LED灯化妆镜", " "),
    ("乐器文具", "百乐 P500 中性笔 一盒", "未拆"),
    ("生活用品", "落地晾衣架", "闲置"),
]

# ------------------------------------------------------------
# 教辅含「答案/真题」—— 教材类目下属正常，期望 APPROVE
# ------------------------------------------------------------
TEXTBOOK_KEY_SAMPLES = [
    ("高等数学（第七版）上册 同济大学", "配套习题答案全解，期末复习用的，书和答案一起出。"),
    ("大学英语综合教程 第三册", "带课后答案和听力原文，四级真题也在里面。"),
    ("线性代数 同济第六版", "含课后习题答案，笔记做得很全。"),
    ("概率论与数理统计 浙大版", "附答案册，考试重点我标出来了。"),
    ("离散数学及其应用", "带习题答案，真题也整理了一份。"),
    ("计算机网络 自顶向下方法", "附课后答案，期末真题重点都标注了。"),
    ("操作系统概念 第九版", "带习题答案，内部资料一份一起给。"),
]

# ------------------------------------------------------------
# 误伤样本：含违禁词子串的正常配件，期望 APPROVE
# 留着它们，是为了能量化关键词规则的真实误杀
# ------------------------------------------------------------
FALSE_POSITIVE_SAMPLES = [
    ("生活用品", "仓鼠笼子 大号 亚克力", "笼子养过仓鼠，已经彻底清洗消毒过了，可以放心用。"),
    ("乐器文具", "学生证卡套 透明 两个装", "买多了，全新没用过，卡套而已。"),
    ("生活用品", "白酒杯 一套六个 玻璃", "家里带的，一直没拆封，杯子挺好。"),
    ("数码电子", "电子烟弹收纳盒 便携", "就是个收纳盒，装小零件的，全新。"),
    ("生活用品", "宠物猫咪零食 未开封", "买给家里的猫的，结果它不吃这个牌子。"),
    ("服饰鞋包", "银行卡包 卡位多", "钱包用不上换了个新的，这个还很新。"),
]


def connect(db=None):
    cfg = {k: v for k, v in DB_CONFIG.items() if k != "database"}
    if db:
        cfg["database"] = db
    return pymysql.connect(cursorclass=DictCursor, **cfg)


def seed():
    conn = connect()
    try:
        with conn.cursor() as cur:
            # 清空（保留表结构）
            for tbl in ("item_image", "item_audit", "item", "item_category", "credit_record", "report", "sys_user"):
                cur.execute(f"DELETE FROM `{BIZ_DB}`.`{tbl}`")
            for tbl in ("audit_steps", "checkpoints", "review_queue", "audit_ground_truth", "eval_runs", "audit_jobs"):
                cur.execute(f"DELETE FROM `{META_DB}`.`{tbl}`")

            # ---- 分类 ----
            cat_ids = {}
            for i, name in enumerate(CATEGORIES, start=1):
                cur.execute(f"INSERT INTO `{BIZ_DB}`.item_category (id, name, sort) VALUES (%s, %s, %s)",
                            (i, name, i))
                cat_ids[name] = i

            # ---- 用户：16 个卖家 + 系统账号 + 管理员 ----
            # 999 是 AI 审核员（audit_source=2 时 audit_user_id 为空，仅作占位）
            # 998 是人工管理员，人工复核/改判时写进 item_audit.audit_user_id
            cur.execute(
                f"""INSERT INTO `{BIZ_DB}`.sys_user (id, username, nickname, role, credit_score)
                    VALUES (999, 'ai_auditor', 'AI 审核员', 9, 100),
                           (998, 'admin', '管理员', 2, 100)""")
            sellers = []
            for i in range(1, 17):
                credit = RNG.choice([100, 98, 95, 92, 88, 85, 80, 75, 72, 68, 55, 45])
                cur.execute(
                    f"""INSERT INTO `{BIZ_DB}`.sys_user (id, username, nickname, role, credit_score)
                        VALUES (%s, %s, %s, 1, %s)""",
                    (i, f"student{i:02d}", f"同学{i:02d}", credit))
                sellers.append({"id": i, "credit": credit})

            # ---- 商品 ----
            items = []          # (cat, title, desc, price, original, expected, violation_type)
            order = [s["id"] for s in sellers]

            def pick_seller():
                return RNG.choice(order)

            def norm_price(lo, hi):
                return round(RNG.uniform(lo, hi), 2)

            # 1) 正常商品 124 件
            for _ in range(124):
                cat = RNG.choice(CATEGORIES)
                title = RNG.choice(PRODUCTS[cat])
                desc = RNG.choice(DESC_TEMPLATES).format(cond=RNG.choice(CONDITIONS), place=RNG.choice(PLACES))
                price = norm_price(10, 800)
                original = round(price * RNG.uniform(1.2, 3.0), 2)
                items.append((cat, title, desc, price, original, "APPROVE", None))

            # 2) 教辅含答案 7 件（教材类目，期望放行）
            for title, desc in TEXTBOOK_KEY_SAMPLES:
                price = norm_price(15, 60)
                items.append(("教材书籍", title, desc, price, round(price * 2.5, 2), "APPROVE", None))

            # 3) 误伤样本 6 件（期望放行，但会被关键词规则误判）
            for cat, title, desc in FALSE_POSITIVE_SAMPLES:
                price = norm_price(10, 120)
                items.append((cat, title, desc, price, round(price * 2, 2), "APPROVE", None))

            # 4) 明确违规 30 件
            for cat, title, desc, vtype in BLOCK_SAMPLES:
                price = norm_price(20, 900)
                items.append((cat, title, desc, price, round(price * 1.5, 2), "REJECT", vtype))

            # 5) 联系方式 15 件
            for desc_extra in CONTACT_SAMPLES:
                cat = RNG.choice(CATEGORIES)
                title = RNG.choice(PRODUCTS[cat])
                desc = RNG.choice(DESC_TEMPLATES).format(cond=RNG.choice(CONDITIONS), place=RNG.choice(PLACES))
                price = norm_price(20, 500)
                items.append((cat, title, desc + " " + desc_extra, price, round(price * 2, 2),
                              "REVIEW", "CONTACT_LEAK"))

            # 6) 价格异常 10 件
            for cat, title, price, original, desc in PRICE_SAMPLES:
                items.append((cat, title, desc, price, original, "REVIEW", "FAKE_INFO"))

            # 7) 描述缺失 8 件
            for cat, title, desc in DESC_MISSING_SAMPLES:
                price = norm_price(15, 300)
                items.append((cat, title, desc, price, round(price * 2, 2), "REVIEW", "FAKE_INFO"))

            RNG.shuffle(items)

            for idx, (cat, title, desc, price, original, expected, vtype) in enumerate(items, start=1):
                seller_id = pick_seller()
                trade = RNG.choice([1, 1, 1, 2, 3])
                cond = RNG.choice([1, 2, 2, 3, 4])
                item_no = f"IT{20260000 + idx}"
                cur.execute(
                    f"""INSERT INTO `{BIZ_DB}`.item
                        (item_no, seller_id, category_id, title, description, price, original_price,
                         cover_img, trade_type, item_condition, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)""",
                    (item_no, seller_id, cat_ids[cat], title[:100], desc, price, original,
                     f"/img/{item_no}.jpg", trade, cond))
                item_id = cur.lastrowid
                cur.execute(f"INSERT INTO `{BIZ_DB}`.item_image (item_id, url, sort) VALUES (%s, %s, 0)",
                            (item_id, f"/img/{item_no}_1.jpg"))
                cur.execute(
                    f"""INSERT INTO `{META_DB}`.audit_ground_truth (item_id, expected, violation_type)
                        VALUES (%s, %s, %s)""",
                    (item_id, expected, vtype))

            # ---- 举报：给少数商品挂举报，用来触发「有举报不自动通过」分支 ----
            report_items = [r for r in range(1, 11)]
            for iid in report_items:
                cur.execute(
                    f"""INSERT INTO `{BIZ_DB}`.report (item_id, reporter_id, reason, status)
                        VALUES (%s, %s, %s, 0)""",
                    (iid, RNG.randint(1, 16), "疑似虚假信息"))

        conn.commit()

        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM `{BIZ_DB}`.item")
            n_items = cur.fetchone()["n"]
            cur.execute(f"SELECT expected, COUNT(*) AS n FROM `{META_DB}`.audit_ground_truth GROUP BY expected")
            dist = {r["expected"]: r["n"] for r in cur.fetchall()}
            cur.execute(f"SELECT COUNT(*) AS n FROM `{BIZ_DB}`.sys_user")
            n_users = cur.fetchone()["n"]
            cur.execute(f"SELECT COUNT(*) AS n FROM `{BIZ_DB}`.report")
            n_reports = cur.fetchone()["n"]
    finally:
        conn.close()

    print(f"[ok] 用户 {n_users} 个（含 1 个 AI 系统账号）")
    print(f"[ok] 商品 {n_items} 件，状态全部为 0（待审核）")
    print(f"[ok] 举报 {n_reports} 条")
    print(f"[ok] 标注分布：{dist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(seed())

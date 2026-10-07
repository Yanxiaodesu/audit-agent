"""混合检索（`search_similar_items`）的测试。

这个工具是审核判定的**参照物来源**，它出错会直接导致误判：

  - 检索不到    → 模型拿不到参照，可能武断驳回
  - 检索太宽    → 一堆不相关商品，模型被噪声带偏
  - **最危险**：把「文本相似但违规情形不同」当成佐证 —— 这正是误杀的来源

所以测试重点在最后一条：工具必须能区分
「**共享关键词**」和「**相同的违规情形**」。

背景案例：「学生证卡套」（正常配件）和「学生证 可代买学生票」（违规服务）
共享"学生证"三个字，文本相似度很高。如果工具只说"5 件相似商品被驳回"，
模型很可能据此驳回那个卡套 —— 这就是需要防住的事。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.tools import search_similar_items  # noqa: E402

# 依赖 seed_data.py 造的数据：191 是正常配件，5 / 43 是真违规
ACCESSORY_ITEM = 191       # 学生证卡套  → 命中 R-ACCESSORY-CONTEXT
SPAM_ITEM = 5              # 校园贷      → 命中 R-SPAM
FRAUD_ITEM = 43            # 学生证代买学生票 → 命中 R-BANNED_GOODS


# ============================================================
# 基本可用性
# ============================================================

def test_能用商品id检索到相似商品():
    r = search_similar_items(item_id=SPAM_ITEM)
    assert "error" not in r, r
    assert r["matched"] > 0, "校园贷应该能检索到相似商品"
    assert len(r["samples"]) == r["matched"]


def test_能用query直接检索():
    r = search_similar_items(query="代写")
    assert "error" not in r, r
    assert r["matched"] > 0, "平台上有多件论文代写，应该检索得到"
    for s in r["samples"]:
        assert "代写" in s["title"], f"检索『代写』却返回了 {s['title']}"


def test_结果按相似度降序():
    r = search_similar_items(item_id=SPAM_ITEM)
    sims = [s["similarity"] for s in r["samples"]]
    assert sims == sorted(sims, reverse=True), f"相似度没有降序：{sims}"


def test_每条结果都带相似度和文本相关度():
    r = search_similar_items(item_id=SPAM_ITEM)
    for s in r["samples"]:
        assert 0 < s["similarity"] <= 1.0
        assert s["text_score"] > 0
        assert s["verdict"] in ("通过", "驳回")


def test_不返回自身():
    r = search_similar_items(item_id=SPAM_ITEM)
    assert all(s["id"] != SPAM_ITEM for s in r["samples"]), "结果里不该包含被检索的商品自己"


def test_每条结果带各自的驳回规则():
    r = search_similar_items(item_id=FRAUD_ITEM)
    assert any(s["rejected_rules"] for s in r["samples"]), "应该有商品带驳回规则"
    for s in r["samples"]:
        assert isinstance(s["rejected_rules"], list)


# ============================================================
# 参数与边界
# ============================================================

def test_两个参数都不给时返回错误():
    r = search_similar_items()
    assert "error" in r


def test_商品不存在时返回错误():
    r = search_similar_items(item_id=99999999)
    assert "error" in r
    assert "不存在" in r["error"]


def test_limit_有上限保护():
    r = search_similar_items(item_id=SPAM_ITEM, limit=9999)
    assert r["matched"] <= 10, "limit 没有做上限保护"


def test_检索不到时是孤立表述而不是报错():
    r = search_similar_items(query="ZZZQQQ氪金赛博朋克XXX")
    assert "error" not in r
    if r["matched"] == 0:
        assert not r["fallback"], "给了 query 而不是 item_id 时不该走分类兜底"
        assert "孤立表述" in r["note"]


def test_全文没命中时退化到同分类并如实标注():
    """兜底必须自曝身份，否则模型会把「同分类最近」误当成「文本相似」。"""
    r = search_similar_items(item_id=ACCESSORY_ITEM, query="ZZZQQQ不存在的词XXX")
    if r["fallback"]:
        assert "退化" in r["note"] or "弱参考" in r["note"], \
            f"走了兜底却没说明，note={r['note']}"


# ============================================================
# 核心：区分「相似关键词」与「相同违规情形」
# ============================================================

def test_规则无重叠时明确说不要据此驳回():
    """这是防误杀的关键断言。

    学生证卡套自身命中 R-ACCESSORY-CONTEXT，而相似商品被驳回的原因是
    SPAM/BANNED/ACADEMIC —— 完全不同的违规情形。
    工具必须明确告诉模型「不构成违规依据」，而不是含糊地说"有几件被驳回"。
    """
    r = search_similar_items(item_id=ACCESSORY_ITEM)
    if r["rejected"] == 0:
        pytest.skip("这件商品的相似项里没有驳回样本，断言不适用")

    assert "R-ACCESSORY-CONTEXT" in r["own_rules"], \
        f"没取到本商品命中的规则：{r['own_rules']}"

    if not r["rule_overlap"]:
        assert "不构成" in r["note"], f"无重叠时必须明确否定，note={r['note']}"
        assert "不要据此驳回" in r["note"]
    else:
        assert "佐证" in r["note"]


def test_规则重叠时给出有力佐证():
    """真违规商品：参照物的驳回规则与自身命中一致，这才算佐证。"""
    r = search_similar_items(item_id=SPAM_ITEM)
    assert r["own_rules"], "校园贷的商品没取到自身规则"
    assert "R-SPAM" in r["own_rules"]
    assert "R-SPAM" in r["rule_overlap"], \
        f"参照物里有 R-SPAM 驳回记录，应该识别出重叠：{r['rejected_rules']}"
    assert "有力佐证" in r["note"], f"重叠时应该给正面结论，note={r['note']}"


def test_返回结构里带自身规则与重叠项():
    """这两个字段是防误杀的抓手，必须稳定存在。"""
    r = search_similar_items(item_id=SPAM_ITEM)
    for key in ("own_rules", "rule_overlap", "rejected_rules", "fallback", "note"):
        assert key in r, f"返回结构缺少 {key}"


def test_关键词相同的两件商品规则却不同():
    """把「文本相似 ≠ 违规相同」这件事实固化成断言。

    学生证卡套（配件）和学生证代买学生票（违规）文本高度相似，
    但一个命中的是配件规则、一个是违禁品规则 —— 检索结果必须能体现这个差别。
    """
    a = search_similar_items(item_id=ACCESSORY_ITEM)
    b = search_similar_items(item_id=FRAUD_ITEM)
    assert set(a["own_rules"]) != set(b["own_rules"]), \
        "两件商品的命中规则不该相同 —— 如果相同说明规则引擎出问题了"

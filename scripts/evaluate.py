"""评测 CLI。

指标计算逻辑在 app/metrics.py —— 页面和命令行共用同一套，口径一致。
这里只负责打印报告和写轮次记录。

用法：
    python scripts/evaluate.py --tag baseline
    python scripts/evaluate.py --tag v3 --no-save     # 只看不记录
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import metrics as M  # noqa: E402


def report(m: dict, tag: str):
    print("=" * 64)
    print(f"  评测报告   tag = {tag}")
    print("=" * 64)
    print(f"  样本总数            {m['total']}")
    print(f"  其中 违规/合规/灰区  {m['violation_total']} / {m['normal_total']} / {m['gray_total']}")
    print()
    print(f"  违规召回率       {m['recall_pct']:>6}%   （漏掉 {m['missed_count']} 件）")
    print(f"  误杀率           {m['false_reject_pct']:>6}%   （误杀 {m['false_reject_count']} 件）")
    print(f"  自动处理率       {m['auto_rate_pct']:>6}%")
    print(f"  人工复核率       {m['review_rate_pct']:>6}%")
    print(f"  与标注一致率     {m['agreement_pct']:>6}%")
    print(f"  LLM 调用次数     {m['llm_calls']}")
    print()
    print("  ---- 按违规类型看召回 ----")
    for t, d in m["recall_by_type"].items():
        print(f"    {t:<18} {d['caught']}/{d['total']}   {d['recall_pct']}%")

    if m["false_reject_items"]:
        print()
        print("  ---- 误杀明细（标注合规却被判驳回）----")
        for r in m["false_reject_items"]:
            print(f"    item#{r['item_id']:<4} {r['title']}")

    if m["missed_items"]:
        print()
        print("  ---- 漏放明细（标注违规却没驳回）----")
        for r in m["missed_items"]:
            print(f"    item#{r['item_id']:<4} [{r['violation_type']}] {r['title']}  -> 判 {r['verdict']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="baseline")
    ap.add_argument("--no-save", action="store_true", help="不写入 eval_runs")
    a = ap.parse_args()

    m = M.compute_metrics()
    if not m:
        print("[!] 没有已完成的审核任务。先提交审核并跑 worker：")
        print("    python -u worker.py --drain")
        return 1

    if not a.no_save:
        M.save_run(a.tag, m)
    report(m, a.tag)

    runs = list(reversed(M.list_runs(20)))     # 从旧到新
    if len(runs) > 1:
        print()
        print("  ---- 历次轮次对比 ----")
        print(f"    {'tag':<26}{'召回':>8}{'误杀':>8}{'自动处理':>10}{'复核':>8}")
        for r in runs:
            print(f"    {r['tag']:<26}{r['recall_pct']:>7}%{r['false_reject_pct']:>7}%"
                  f"{r['auto_rate_pct']:>9}%{r['review_rate_pct']:>7}%")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""M3 必做：L3 判准率实测（PRD v8 §6.6）。

L3 是去重链路的【唯一判定者】，方案把误并风险全部转嫁给它，
却从未验证过它判得准。判准率 <95% 则不得进入 M5 自动滚动，
合并一律改走 voc_proposal 人工确认。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python tests/l3_accuracy.py

前置条件：
  Python 3.11+ 及项目依赖已安装；.env 中已配置可用的百炼/LLM 凭据；
  baseline/l3_testset.json 存在，并允许访问模型服务。本脚本不写数据库。
"""
from __future__ import annotations
import json, sys, time
sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import config as C, llm, resolve  # noqa: E402

DATA = json.load(open("/home/sdy/voc-analytics/baseline/l3_testset.json"))
ctx = C.RunCtx(run_id="l3_acc", week="2026-W33")

rows = ([{**x, "truth": "same"} for x in DATA["should_attach"]]
        + [{**x, "truth": "different"} for x in DATA["should_create"]])

print(f"L3 判准率实测：{len(rows)} 个样本"
      f"（应挂载 {len(DATA['should_attach'])} / 应新建 {len(DATA['should_create'])}）\n")

t0 = time.time()


def judge(r: dict) -> dict:
    v = resolve.l3_verdict(r["b_mode"], "", [r["b_mode"]],
                           {"opp_id": "T", "problem_mode": r["a_mode"],
                            "title": r["a_title"], "rep_snippets": [], "safety_flag": False},
                           ctx)
    return {**r, "got": v["verdict"], "conf": v["confidence"], "why": v["rationale"]}


results = [x for x in llm.parallel_map(judge, rows) if isinstance(x, dict)]

wrong_merge = [r for r in results if r["truth"] == "different" and r["got"] == "same"]
wrong_split = [r for r in results if r["truth"] == "same" and r["got"] == "different"]
correct = len(results) - len(wrong_merge) - len(wrong_split)
acc = correct / len(results) if results else 0

print(f"{'真值':<10}{'判定':<10}{'置信':<7}问题对")
print("-" * 100)
for r in results:
    mark = "  " if r["truth"] == r["got"] else "✗ "
    print(f"{mark}{r['truth']:<8}{r['got']:<10}{r['conf']:<7.2f}{r['b_mode'][:52]}")

print("-" * 100)
print(f"判准率 {correct}/{len(results)} = {acc*100:.1f}%")
print(f"误并（应新建却判 same）: {len(wrong_merge)}   ← 不可逆的信息丢失，最严重")
print(f"误分（应挂载却判 different）: {len(wrong_split)}   ← 产生重复条目，可由 PM 合并挽回")
print(f"耗时 {time.time()-t0:.0f}s，LLM 调用 {llm.usage()['calls']} 次")

if wrong_merge:
    print("\n误并明细（每一条都意味着一个独立问题被永久吞掉）:")
    for r in wrong_merge:
        print(f"  · 新问题「{r['b_mode'][:40]}」被并入「{r['a_title']}」")
        print(f"    L3 理由: {r['why'][:110]}")

print()
if acc >= 0.95:
    print("结论：判准率 ≥95%，允许进入 M5 自动滚动（合并可自动执行）")
else:
    print("结论：判准率 <95%，按 PRD §6.6【不得进入 M5 自动滚动】——")
    print("      合并一律走 voc_proposal 由 PM 确认，L3 只做建议")
sys.exit(0 if acc >= 0.95 else 2)

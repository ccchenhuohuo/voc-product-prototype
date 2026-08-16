#!/usr/bin/env python3
"""M3.5 快速版：产出稳定性 = 语义等价（PRD v8 §5.8 / §9.2）。

判据不是字面相等（托管服务的 temperature=0 不保证逐 token 复现），而是：
  同一批证据连跑 2 次 ——
  · Stage1 切分的组结构一致率（Jaccard）
  · 同组 problem_mode 的 mode_vec 余弦 ≥ 0.95
  · 标题动作词一致
真正的 M3.5（两个真实周次的 opp_id 不变率）需要时间窗，此为其先导测试。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python tests/m35_stability.py

前置条件：
  Python 3.11+ 及项目依赖已安装；数据库连接和百炼/LLM 凭据已配置；事实层中
  至少存在一个含 30--60 条证据的线A桶，并允许访问模型服务。本脚本只读数据库。
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import config as C, db, llm, pipeline  # noqa: E402
from voc_analytics.stages import stage1  # noqa: E402
import re  # noqa: E402
from voc_analytics import prompts  # noqa: E402

# 取一个中等桶（30-60 条）做两轮全流程
rows = db.line_a_pool()
buckets: dict = {}
for r in rows:
    buckets.setdefault((r.get("category") or "未知", r["tag"]), []).append(r)
cand = [(k, v) for k, v in buckets.items() if 30 <= len(v) <= 60]
(cat, tag), items = sorted(cand, key=lambda kv: -len(kv[1]))[0]
print(f"稳定性测试桶: {cat}/{tag}  n={len(items)}\n")

info = {"category": cat, "tag": tag, "tax_path": items[0].get("tax_path", ""),
        "low_star_rate": "?", "prod_line": "支撑"}
ACT = re.compile("|".join(prompts.ACTIONS))

runs = []
for k in (1, 2):
    ctx = C.RunCtx(run_id=f"stab{k}", week="2026-W33")
    split = stage1.split_bucket(items, "线A", info, ctx, vote=False)
    groups = stage1.merge_similar_modes(split["groups"], ctx)
    built = []
    for g in groups[:5]:                       # 每轮取前 5 组做 Stage2，控制成本
        opp = pipeline.build_opportunity(items, g, "线A", info, ctx, [])
        if opp:
            built.append({"members": frozenset(g["members"]),
                          "mode": opp["problem_mode"], "title": opp["title"]})
    runs.append({"groups": [frozenset(g["members"]) for g in groups], "built": built})
    print(f"第 {k} 轮: {len(groups)} 组，Stage2 产出 {len(built)}")

# ---- 1. 组结构一致率 ----
g1, g2 = runs[0]["groups"], runs[1]["groups"]
matched = 0
for a in g1:
    best = max((len(a & b) / len(a | b) for b in g2), default=0)
    if best >= 0.6:
        matched += 1
struct = matched / len(g1) if g1 else 0
print(f"\n组结构一致率(Jaccard≥0.6): {matched}/{len(g1)} = {struct*100:.0f}%")

# ---- 2. 同组 problem_mode 语义等价 ----
pairs = []
for a in runs[0]["built"]:
    cands = [b for b in runs[1]["built"] if len(a["members"] & b["members"]) /
             max(len(a["members"] | b["members"]), 1) >= 0.5]
    if cands:
        pairs.append((a, cands[0]))
if pairs:
    va = llm.embed([p[0]["mode"] for p in pairs])
    vb = llm.embed([p[1]["mode"] for p in pairs])
    print(f"\n同组两轮 problem_mode 余弦（判据 ≥0.95）:")
    ok_sem = ok_act = 0
    for (a, b), x, y in zip(pairs, va, vb):
        cos = llm.cosine(x, y)
        aa = ACT.findall(a["title"]); ab = ACT.findall(b["title"])
        act_same = bool(set(aa) & set(ab))
        ok_sem += cos >= 0.95; ok_act += act_same
        print(f"  cos={cos:.3f}  动作词{'一致' if act_same else '不同'}  "
              f"{a['mode'][:34]} | {b['mode'][:34]}")
    print(f"\n语义等价: {ok_sem}/{len(pairs)}   动作词一致: {ok_act}/{len(pairs)}")
    passed = struct >= 0.7 and ok_sem == len(pairs)
else:
    print("无可比对的组对，结构漂移过大")
    passed = False

u = llm.usage()
print(f"\nLLM 调用 {u['calls']} 次 / {u['tokens']} tokens")
print("结论:", "语义稳定性达标（§5.8）" if passed else "稳定性不足，需要收紧 Stage1 或改用缓存切分")
sys.exit(0 if passed else 2)

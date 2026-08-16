#!/usr/bin/env python3
"""产出质量画像：把 PRD §9.2/§9.4 的指标从纸面变成实测分布。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python tests/quality_report.py

前置条件：
  Python 3.11+ 及项目依赖已安装；数据库连接已配置；voc_opportunity 与
  voc_weekly_metrics 已有当前口径的产出。本脚本只读数据库。
"""
from __future__ import annotations
import re, sys
sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import db, prompts  # noqa: E402
from voc_analytics.stages import validate as V  # noqa: E402

rows = db.q("SELECT * FROM voc_opportunity")
if not rows:
    print("无产出"); sys.exit(1)
n = len(rows)
ACT = re.compile("|".join(prompts.ACTIONS))

def pct(k): return f"{k}/{n} = {100*k/n:.0f}%"

print(f"=== 产出质量画像（n={n}）===\n")
print("【结构合规】")
print("  标题符合公式        ", pct(sum(1 for r in rows if V._TITLE_RE.match(r["title"] or ""))))
print("  标题动作词在词表内  ", pct(sum(1 for r in rows if ACT.search(r["title"] or ""))))
print("  三段齐全            ", pct(sum(1 for r in rows if r["desc_phenomenon"] and r["desc_attribution"]
                                       and (r["desc_suggestion"] or "").strip())))
print("  归因段以推断词开头  ", pct(sum(1 for r in rows
      if any((r["desc_attribution"] or "").startswith(w) for w in prompts.ATTR_STARTERS))))

print("\n【反幻觉】")
print("  建议段引用标准号    ", pct(sum(1 for r in rows if V._STD_PAT.search(r["desc_suggestion"] or ""))), "（应为 0）")
print("  正文泄漏字段名      ", pct(sum(1 for r in rows if V._LEAK_PAT.search(
      (r["desc_phenomenon"] or "") + (r["desc_attribution"] or "")))), "（应为 0）")
print("  出现无支撑程度词    ", pct(sum(1 for r in rows if any(
      w in (r["desc_phenomenon"] or "") + (r["desc_attribution"] or "") for w in prompts.BANNED_WORDS))), "（应为 0）")
print("  建议段已验证类断言  ", pct(sum(1 for r in rows if any(
      w in (r["desc_suggestion"] or "") for w in prompts.HEDGE_WORDS))), "（应为 0）")

print("\n【可复现性】")
for f in ("prompt_ver", "model_ver", "ctx_hash", "mode_vec"):
    print(f"  {f:<20}", pct(sum(1 for r in rows if r[f] is not None)))

print("\n【业务分布】")
print("  needs_review        ", pct(sum(1 for r in rows if r["needs_review"])), "（PRD 门槛 ≤40%）")
print("  safety_flag         ", pct(sum(1 for r in rows if r["safety_flag"])))
print("  dual_source         ", pct(sum(1 for r in rows if r["dual_source"])))
print("  weak_evidence(≤2)   ", pct(sum(1 for r in rows if r["weak_evidence"])))
ev = sorted(r["evi_total"] or 0 for r in rows)
print(f"  证据数分布           min={ev[0]} p50={ev[n//2]} max={ev[-1]} 合计={sum(ev)}")
ln = sorted(len(r["desc_phenomenon"] or "") for r in rows)
print(f"  现象段长度           min={ln[0]} p50={ln[n//2]} max={ln[-1]}")

print("\n【覆盖率】")
m = db.q("SELECT * FROM voc_weekly_metrics")[0]
print(f"  线A 覆盖率           {m['line_a_coverage_pct']}%  （{m['line_a_attached']}/{m['line_a_pool']}）")
print(f"  未归类留存           {m['unclassified_total']} 条")

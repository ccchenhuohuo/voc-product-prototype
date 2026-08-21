#!/usr/bin/env python3
"""守恒等式实测：在全量 G5 池上算 U / F / R，并给出 v3 分桶分布。

只读。G4 判定读 voc_social_gate 缓存，不调 LLM、不写库。
走的是生产代码的 generation_pool() 与 route_social_value_evidence()，
不是重写的近似 SQL —— 口径必须与真实路由一致。

必须显式传入已准备好的 --run-id；探针只读该轮归属快照，不回读消息数组。

规格 §5 定义：
  U = 唯一事实数 count(DISTINCT (message_id, seq))
  F = 扇出后应有行数 Σ count(DISTINCT assigned_spu)
  R = 路由实际产出行数（进入 L1 分桶的行数）
守恒要求 R = F。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voc_analytics import db  # noqa: E402
from voc_analytics.routing import (  # noqa: E402
    classify_evidence_by_lifecycle,
    route_social_value_evidence,
)

GATES = """SELECT message_id, cls FROM voc_social_gate
            WHERE prompt_ver = (SELECT prompt_ver FROM voc_social_gate
                                 ORDER BY judged_at DESC LIMIT 1)"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True,
                        help="已由 voc_prepare_assign_snapshot 准备的运行 ID")
    args = parser.parse_args()

    db.verify_assign_snapshot(args.run_id)
    rows = db.generation_pool(assign_run_id=args.run_id)
    gate = {r["message_id"]: r["cls"] for r in db.q(GATES)}
    print(f"[池] generation_pool 全量 {len(rows)} 行")

    ec = [r for r in rows if r.get("src_line") == "电商"]
    social = [r for r in rows if r.get("src_line") == "社媒"]
    print(f"[池] 电商 {len(ec)}  社媒 {len(social)}")

    ec_classified, ec_invalid = classify_evidence_by_lifecycle(ec)

    # 社媒：套用缓存的 G4 判定，无价值/未判过的直接出局（与生产同口径）
    passed = []
    dropped = Counter()
    for row in social:
        cls = gate.get(row["message_id"])
        if cls not in {"诉求缺口", "产品缺陷"}:
            dropped[cls or "未判过"] += 1
            continue
        item = dict(row)
        item["_value_cls"] = cls
        passed.append(item)
    print(f"[G4] 通过 {len(passed)}，出局 {dict(dropped)}")

    routed = route_social_value_evidence(passed)
    print(f"[G5] eligible {len(routed.eligible)}，"
          f"缺陷不可归属 {len(routed.unassigned_defects)}")

    demoted = sum(1 for r in routed.eligible if r.get("_inherit_demoted"))
    print(f"[G5] 其中诉求因仅继承被降级到新品创新：{demoted}")

    old = [r for r in ec_classified + routed.eligible
           if r.get("_opp_type") == "老品迭代"]
    new = [r for r in ec_classified + routed.eligible
           if r.get("_opp_type") == "新品创新"]
    print(f"\n[生命周期] 老品迭代 {len(old)}  新品创新 {len(new)}"
          f"  无效 {len(ec_invalid)}")

    # ---- 守恒等式：只对老品有意义（新品无 SPU）----
    U = len({(r["message_id"], r["seq"]) for r in old})
    fan: dict[tuple, set[str]] = {}
    for r in old:
        key = (r["message_id"], r["seq"])
        assigned_spu = (r.get("assigned_spu") or "").strip()
        if assigned_spu:
            fan.setdefault(key, set()).add(assigned_spu)
    F = sum(len(v) for v in fan.values())
    R = len(old)
    multi = sum(1 for v in fan.values() if len(v) > 1)

    print("\n" + "=" * 54)
    print("守恒等式（老品迭代，全量池）")
    print("=" * 54)
    print(f"  U 唯一事实数      {U:>6}")
    print(f"  F 扇出后应有行数  {F:>6}")
    print(f"  R 路由产出行数    {R:>6}   {'✓ R = F' if R == F else '✗ 守恒破坏'}")
    print(f"  F/U 扇出倍率      {F/U:>6.3f}")
    print(f"  多 SPU 证据       {multi:>6}  ({100.0*multi/U:.1f}%)")
    print(f"  单条最多 SPU 数   {max((len(v) for v in fan.values()), default=0):>6}")

    # ---- v3 分桶分布 ----
    buckets = Counter()
    for key, spus in fan.items():
        for spu in spus:
            buckets[spu] += 1
    sizes = sorted(buckets.values(), reverse=True)
    n = len(sizes)

    def pct(p: float) -> int:
        return sizes[min(int(n * p), n - 1)] if n else 0

    batches = sum((sz + 49) // 50 for sz in sizes)
    print("\n" + "=" * 54)
    print("v3 SPU 分桶分布（全量池）")
    print("=" * 54)
    print(f"  桶数              {n:>6}")
    print(f"  总行              {sum(sizes):>6}")
    print(f"  中位 / p90 / 最大 {pct(0.5):>6} / {pct(0.1)} / {sizes[0] if sizes else 0}")
    print(f"  >50 行的热门桶    {sum(1 for s in sizes if s > 50):>6}")
    print(f"  Stage1 批次       {batches:>6}")
    print()


if __name__ == "__main__":
    main()

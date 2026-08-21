#!/usr/bin/env python3
"""守恒等式实测：物理快照算 F、实际路由算 R，并给出双向差集。

只读。G4 判定读 voc_social_gate 缓存，不调 LLM、不写库。
走的是生产代码的 generation_pool() 与 route_social_value_evidence()，
不是重写的近似 SQL —— 口径必须与真实路由一致。

必须显式传入已准备好的 --run-id；探针只读该轮归属快照，不回读消息数组。

规格 §5 定义：
  U = 唯一事实数 count(DISTINCT (message_id, seq))
  F = voc_assign_snapshot 当前 run_id 的物理扇出键行数
  R = 路由实际产出行数（进入 L1 分桶的行数）
守恒要求行数相等、无重复，且两侧键的双向差集均为空。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voc_analytics import db, pipeline  # noqa: E402
from voc_analytics.routing import (  # noqa: E402
    classify_evidence_by_lifecycle,
    route_classified_evidence,
    route_social_value_evidence,
)

GATES = """SELECT message_id, cls FROM voc_social_gate
            WHERE prompt_ver = (SELECT prompt_ver FROM voc_social_gate
                                 ORDER BY judged_at DESC LIMIT 1)"""


def print_conservation(
    snapshot_rows: list[dict],
    routed_rows: list[dict],
    *,
    emit=print,
) -> dict:
    """打印可证伪的 F/R 对账；纯内存入口供离线注入测试复用。"""
    stat = pipeline.assignment_route_reconciliation(
        snapshot_rows, routed_rows)
    fan: dict[tuple[str, int], set[str]] = {}
    for row in snapshot_rows:
        key = (row["message_id"], row["seq"])
        fan.setdefault(key, set()).add(row["assigned_spu"])
    unique_facts = len(fan)
    multi = sum(1 for spus in fan.values() if len(spus) > 1)

    emit("\n" + "=" * 54)
    emit("守恒等式（老品迭代，全量池）")
    emit("=" * 54)
    emit(f"  U 唯一事实数      {unique_facts:>6}")
    emit(f"  F 物理快照行数    {stat['snapshot_rows']:>6}")
    outcome = "✓ 键集合一致" if stat["complete"] else "✗ 守恒破坏"
    emit(f"  R 路由产出行数    {stat['routed_rows']:>6}   {outcome}")
    emit("  snapshot EXCEPT routed "
         f"{stat['snapshot_except_routed_rows']:>6}  "
         f"samples={stat['snapshot_except_routed_samples']}")
    emit("  routed EXCEPT snapshot "
         f"{stat['routed_except_snapshot_rows']:>6}  "
         f"samples={stat['routed_except_snapshot_samples']}")
    emit("  重复行（快照/路由） "
         f"{stat['snapshot_duplicate_rows']:>5} / "
         f"{stat['routed_duplicate_rows']}")
    ratio = stat["snapshot_rows"] / unique_facts if unique_facts else 0.0
    emit(f"  F/U 扇出倍率      {ratio:>6.3f}")
    multi_ratio = 100.0 * multi / unique_facts if unique_facts else 0.0
    emit(f"  多 SPU 证据       {multi:>6}  ({multi_ratio:.1f}%)")
    emit(f"  单条最多 SPU 数   "
         f"{max((len(v) for v in fan.values()), default=0):>6}")
    return stat


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True,
                        help="已由 voc_prepare_assign_snapshot 准备的运行 ID")
    args = parser.parse_args()

    verified_snapshot_rows = db.verify_assign_snapshot(args.run_id)
    snapshot_rows = db.load_assign_snapshot_rows(args.run_id)
    if len(snapshot_rows) != verified_snapshot_rows:
        print("✗ 快照物理读取与指纹计数不一致："
              f"verified={verified_snapshot_rows}, loaded={len(snapshot_rows)}")
        return 1
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

    # ---- 守恒等式：F 与 R 必须来自独立来源 ----
    old_routing = route_classified_evidence(old)
    routed_old_rows = [
        row
        for bucket, items in old_routing.buckets.items()
        if bucket.opp_type == "老品迭代"
        for row in items
    ]
    conservation = print_conservation(snapshot_rows, routed_old_rows)

    # ---- v3 分桶分布 ----
    buckets = Counter(row["assigned_spu"] for row in routed_old_rows)
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
    return 0 if conservation["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

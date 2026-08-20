#!/usr/bin/env python3
"""G4 判定预热：把整池社媒的价值门判定一次性物化到 voc_social_gate。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python scripts/warm_value_gate.py --week 2026-W33 --full-history

为什么需要它
------------
`pipeline.generate_opportunities()` 里 `opp_types` 过滤发生在
`apply_value_gate()` **之后**（见 pipeline.py 的 scoped_rows）。因此
rerun_both.sh 并发拉起 `--lifecycle existing` 与 `--lifecycle innovation`
时，两个进程都会对**全池**社媒各判一次：

  * 冷缓存下重复约 8000 次 LLM 判定，成本翻倍；
  * 两边抢写 voc_social_gate 同一主键（PK 为 message_id），last-writer-wins，
    落库缓存与各进程实际用于路由的结论可能不是同一个；
  * 若同一条无 SPU 消息一边判「诉求」、一边判「产品缺陷」，前者留给新品创新、
    后者按「缺陷不可归属」丢弃，同一条消息在两个生命周期里得到互斥处置，
    而两份对账各自都能判 complete。

先跑本脚本把判定物化，随后两个生成进程即全部命中缓存、不再写入，
三个问题一起消失，且 rerun_both.sh 的双进程监督逻辑不用改动。

本脚本只写 voc_social_gate（G4 判定缓存），不动机会层、不做路由、不收尾。
"""
from __future__ import annotations
import argparse, sys, time, traceback

sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import config as C, db, ingest, llm  # noqa: E402
from voc_analytics.stages import value_gate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--full-history", action="store_true",
                    help="忽略周窗口，在整个事实层上预热（全量重建用）")
    args = ap.parse_args()

    if args.full_history:
        window_start = window_end = None
        print("[全历史] 忽略周窗口，对整个事实层预热 G4")
    else:
        window_start, window_end = ingest.window_bounds(args.week)

    ctx = C.RunCtx(run_id=args.run_id or f"warm_{args.week}_{int(time.time())}",
                   week=args.week)
    llm.reset_usage()
    t0 = time.time()
    try:
        rows = db.generation_pool(window_start, window_end)
        social_rows = [r for r in rows if r.get("src_line") == "社媒"]
        print(f"生成池 {len(rows)} 行，其中社媒 {len(social_rows)} 行")
        if not social_rows:
            # 空池不是成功。清层重跑前若社媒整片消失，必须在这里就炸，
            # 而不是让后续两个进程各自「成功生成 0 条」。
            raise SystemExit("社媒证据为空：生成池取数异常，拒绝继续")

        gate = value_gate.apply_value_gate(social_rows, ctx)
        s = gate.stats
        print(f"  输入消息 {s['input_messages']}　缓存命中 {s['cache_hit_messages']}"
              f"　新判定 {s['llm_messages']}　票数 {s['llm_votes']}")
        print(f"  无价值 {len(gate.no_value_rows)}　诉求过泛 {len(gate.generic_claim_rows)}"
              f"　通过 {len(gate.passed_rows)}　失败 {len(gate.failed_rows)}")
        if gate.failed_rows:
            raise SystemExit(f"G4 单消息判定失败 {len(gate.failed_rows)} 条，"
                             f"预热未完成；修复后重跑本脚本")
        print(f"预热完成，用时 {time.time()-t0:.0f}s；{llm.format_cost()}")
        return 0
    except BaseException as error:
        traceback.print_exc()
        print(f"预热失败：{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

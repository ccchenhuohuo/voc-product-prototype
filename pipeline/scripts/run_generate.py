#!/usr/bin/env python3
"""阶段 2c：按生命周期生成，并在两个生命周期成功后统一收尾。

本脚本会连接数据库并调用 LLM；仓库交付阶段只产代码，不应在本机直接执行。
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime

sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import config as C, db, ingest, lifecycle, llm, pipeline, resolve  # noqa: E402


LIFECYCLE_ARGS = {
    "existing": {"老品迭代"},
    "innovation": {"新品创新"},
    "both": {"老品迭代", "新品创新"},
}


class SupervisorTermination(RuntimeError):
    """监督脚本取消本进程；转成可记录异常而非默认静默退出。"""


def _raise_on_termination(signum, _frame) -> None:
    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = str(signum)
    reason = f"收到监督信号 {name}"
    llm.cancel(reason)
    raise SupervisorTermination(reason)


def _save_log(ctx, stage: str, status: str, started: float,
              error: BaseException | None = None) -> None:
    usage = pipeline.sync_llm_usage(ctx)
    generation = ctx.metrics.get("generation", {})
    finalize = ctx.metrics.get("finalize", {})
    reconciliation = (pipeline.generation_reconciliation(ctx)
                      if "generation" in ctx.metrics else None)
    db.save_run_log(
        ctx, stage, status=status,
        matched_rows=(ctx.metrics.get("pool", {}).get("scoped_evidence_rows")
                      if generation else finalize.get("planned_targets")),
        exported_rows=(generation.get("completed_persistence")
                       if generation else finalize.get("attached_evidence")),
        batch_count=((reconciliation or {}).get("planned_batches")
                     if generation else finalize.get("planned_targets")),
        unclassified_rows=generation.get("unclassified_rows"),
        error_message=(f"{type(error).__name__}: {error}"[:1000] if error else None),
        started_at=datetime.fromtimestamp(started), finished_at=datetime.now())
    print(f"LLM: {usage['calls']} 次 / {usage['tokens']} tokens", flush=True)


def _finalize(args, ctx) -> dict:
    created = [r for r in db.q("""
      SELECT opp_id, mode_vec::text AS v, opp_type, source_lines
        FROM voc_opportunity
       WHERE classification_state='确定' AND merged_into IS NULL
         AND mode_vec IS NOT NULL
       ORDER BY opp_id
    """)]
    print(f"[收尾] 覆盖库内全部 {len(created)} 个有效机会点")

    ctx.metric_update(
        ("finalize",), status="running", planned_targets=len(created),
        completed_targets=0, failed_targets=0, cancelled_targets=0,
        attached_evidence=0)
    attached_total = 0
    try:
        for row in created:
            try:
                opp_type, source_lines = pipeline.validate_lifecycle_sources(row)
                vec = [float(value) for value in row["v"].strip("[]").split(",")]
                attached = resolve.cross_source_merge(
                    row["opp_id"], vec, opp_type, source_lines, args.week, ctx)
                attached_count = 0
                if attached:
                    attached_count = pipeline.attach_evidence(row["opp_id"], attached)
                    attached_total += attached_count
                ctx.metric_incr(
                    ("finalize",), completed_targets=1,
                    attached_evidence=attached_count)
            except BaseException:
                ctx.metric_incr(("finalize",), failed_targets=1)
                raise

        split_total = 0
        for row in created:
            proposal = resolve.detect_split(row["opp_id"])
            if proposal:
                db.execute(
                    "INSERT INTO voc_proposal(op_type,opp_ids,rationale,week) "
                    "VALUES(%s,%s,%s,%s)",
                    [proposal["op_type"], proposal["opp_ids"],
                     proposal["rationale"], args.week])
                split_total += 1

        db.execute("""
          INSERT INTO voc_opp_snapshot(opp_id,week,evi_total,rank_score,base_total,neg_total,
                                       title,problem_mode,desc_phenomenon,desc_attribution,
                                       desc_suggestion,prompt_ver,model_ver)
          SELECT o.opp_id,%s,o.evi_total,o.rank_score,
                 (SELECT count(*) FROM voc_evidence e JOIN voc_message m USING(message_id)
                   WHERE e.is_product AND e.tag IS NOT DISTINCT FROM o.core_tag),
                 (SELECT count(*) FROM voc_evidence e JOIN voc_message m USING(message_id)
                   WHERE e.is_product AND e.sentiment='负面'
                     AND e.tag IS NOT DISTINCT FROM o.core_tag),
                 o.title,o.problem_mode,o.desc_phenomenon,o.desc_attribution,
                 o.desc_suggestion,o.prompt_ver,o.model_ver
            FROM voc_opportunity o WHERE o.last_week=%s
          ON CONFLICT (opp_id,week) DO UPDATE SET
            evi_total=EXCLUDED.evi_total, rank_score=EXCLUDED.rank_score,
            title=EXCLUDED.title, desc_suggestion=EXCLUDED.desc_suggestion
        """, [args.week, args.week])
        release = lifecycle.release_to_pm()
        ctx.metric_update(
            ("finalize",), status="completed", opportunities=len(created),
            attached_evidence=attached_total, split_proposals=split_total,
            release=release)
        return {"opportunities": len(created), "attached": attached_total,
                "splits": split_total, "release": release}
    except BaseException:
        ledger = ctx.metrics.get("finalize", {})
        ctx.metric_update(
            ("finalize",), status="failed",
            cancelled_targets=max(
                int(ledger.get("planned_targets", 0))
                - int(ledger.get("completed_targets", 0))
                - int(ledger.get("failed_targets", 0)), 0))
        raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week", required=True)
    parser.add_argument("--lifecycle", choices=tuple(LIFECYCLE_ARGS), default="both")
    parser.add_argument("--run-id")
    parser.add_argument("--limit-buckets", type=int, default=0)
    parser.add_argument("--limit-rows", "--limit", dest="limit_rows", type=int, default=0)
    parser.add_argument("--no-vote", action="store_true")
    parser.add_argument("--skip-finalize", "--skip-cross", action="store_true",
                        dest="skip_finalize")
    parser.add_argument("--finalize-only", "--cross-only", action="store_true",
                        dest="finalize_only")
    # 默认按 --week 的窗口取池，服务周度增量。全量重建必须显式打开本开关：
    # 机会点层是【跨周去重】的结构，逐周分别生成得到的结果与一次全量生成
    # 并不等价（消解在整池上做才能把跨周的同一问题收敛成一条）。
    # 0.0.1 基线的 bucket_line_a() 本就不传周边界，即全历史；2c 统一入口时
    # 改成按周取池，若在此基础上执行 rerun_both.sh（先清空再按单周重建），
    # 会用一周的产物替换掉全部历史。2026-08-17 排查发现，加此开关修正。
    parser.add_argument("--full-history", action="store_true",
                        help="忽略周窗口，在整个事实层上生成（全量重建用）")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    signal.signal(signal.SIGTERM, _raise_on_termination)
    signal.signal(signal.SIGINT, _raise_on_termination)
    run_id = args.run_id or f"gen_{args.week}_{int(time.time())}"
    ctx = C.RunCtx(run_id=run_id, week=args.week)
    llm.reset_usage()
    started = time.time()
    stage = "finalize" if args.finalize_only else f"generate_{args.lifecycle}"

    try:
        if args.finalize_only:
            result = _finalize(args, ctx)
            print(f"收尾完成：{result}")
        else:
            if args.full_history:
                window_start = window_end = None
                print("[全历史] 忽略周窗口，在整个事实层上生成")
            else:
                window_start, window_end = ingest.window_bounds(args.week)
            result = pipeline.generate_opportunities(
                args.week, ctx, week_start=window_start, week_end=window_end,
                opp_types=LIFECYCLE_ARGS[args.lifecycle],
                limit_buckets=args.limit_buckets, limit_rows=args.limit_rows,
                # 未指定 --no-vote 时交给配置的 VOTE_ENABLED，不强制打开。
                vote=False if args.no_vote else None, verbose=True)
            print(f"生成完成：{len(set(result['created']))} 个 canonical 机会点")
            if not args.skip_finalize:
                finalized = _finalize(args, ctx)
                print(f"收尾完成：{finalized}")
        _save_log(ctx, stage, "success", started)
        return 0
    except BaseException as error:  # 顶层必须把所有失败变成非零进程状态
        try:
            # 业务校验、数据库或普通重试耗尽也必须让同 run 的兄弟进程停止。
            llm.cancel(f"{stage} 失败：{type(error).__name__}: {error}")
        except Exception as cancel_error:
            if hasattr(error, "add_note"):
                error.add_note(f"发布取消熔断也失败：{cancel_error}")
        try:
            _save_log(ctx, stage, "failed", started, error)
        except Exception as log_error:  # 原始失败优先，日志写失败只追加到 stderr
            print(f"!! 失败日志写入也失败：{log_error}", file=sys.stderr)
        print(f"!! {stage} 失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""阶段 2c：按生命周期生成，并在两个生命周期成功后统一收尾。

本脚本会连接数据库并调用 LLM；仓库交付阶段只产代码，不应在本机直接执行。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime

sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import (  # noqa: E402
    config as C, db, execute, explode, ingest, lifecycle, llm, pipeline, resolve,
    strategy)


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
    # finalize 后的战略重算使用同一进程，但有自己的 run log。先冻结主管线
    # 用量，避免战略调用被重复算进 generate/finalize 台账。
    frozen_usage = ctx.metrics.get("pipeline_llm_usage")
    if isinstance(frozen_usage, dict):
        usage = frozen_usage
        ctx.set_llm_usage(int(usage.get("calls", 0)), int(usage.get("tokens", 0)))
    else:
        usage = pipeline.sync_llm_usage(ctx)
    # 费用进 metrics 才能事后按 run 复盘；只留一行 print 的话，日志一转就没了。
    ctx.metric_update(("cost",), **llm.cost(usage))
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
    # 费用按 config.PRICE_PER_MTOK 估算，真实账单以百炼控制台为准。
    print(llm.format_cost(usage), flush=True)


def _execute_proposals(week: str, ctx) -> dict:
    """在生成前消费 PM 裁决，确保新证据挂到合并后的目标条目。"""
    # 静默自动融合默认关闭；只有显式设置 VOC_AUTO_MERGE=1 才改变 pending
    # 提案状态，沿用原资产图的开关语义。
    auto = (execute.run_auto(week, opp_id_prefix="OPP2-")
            if os.environ.get("VOC_AUTO_MERGE") == "1"
            else {"auto_accepted": "已关闭"})
    stat = execute.run(week, opp_id_prefix="OPP2-")
    result = {**auto, **stat}
    # 提案动作会改写机会点层，属于 finalize 账本；run log 会完整保存 metrics。
    ctx.metric_update(("finalize",), proposal_execution=result)
    print(f"[提案落地] {result}", flush=True)
    return result


def _check_revive(week: str, ctx) -> int:
    """收尾快照后检查完成态/墓碑条目并提 REVIVE 提案。"""
    count = lifecycle.check_revive(week, opp_id_prefix="OPP2-")
    ctx.metric_update(("finalize",), revive_proposals=count)
    print(f"[复活检测] 新增 {count} 条 REVIVE 提案", flush=True)
    return count


def _run_strategy_after_finalize(args, ctx) -> dict | None:
    """收尾成功后的独立派生层；任何失败都不得改变主管线退出码。"""
    if args.skip_strategy:
        ctx.metric_update(("strategy_hook",), status="skipped")
        print("[战略层] 已按 --skip-strategy 跳过", flush=True)
        return None

    # 主管线尚未调用 _save_log，必须在战略 LLM 调用前冻结它自己的用量。
    pipeline_usage = llm.usage()
    ctx.metrics["pipeline_llm_usage"] = pipeline_usage
    strategy_started = datetime.now()
    ctx.metric_update(
        ("strategy_hook",), status="running",
        generation=C.STRATEGY_GENERATION, stage="strategy",
    )
    try:
        result = strategy.run_strategy(
            axis_type="all",
            generation=C.STRATEGY_GENERATION,
            run_id=ctx.run_id,
        )
        metrics = result["metrics"]
        ctx.metric_update(
            ("strategy_hook",), status="completed", stage="strategy",
            axes_written=metrics["axes_written"],
            members_written=metrics["members_written"],
            pairs_recalled=metrics["pairs_recalled"],
            pairs_evaluated=metrics["pairs_evaluated"],
        )
        print(
            f"[战略层] 轴 {metrics['axes_written']} 根 / "
            f"成员 {metrics['members_written']} 行",
            flush=True,
        )
        return result
    except BaseException as error:
        failure = {
            "exception_type": type(error).__name__,
            "stage": "strategy",
            "message": str(error)[:500],
        }
        ctx.metric_update(
            ("strategy_hook",), status="failed", stage="strategy",
            failure=failure,
        )
        # run_strategy 会先写 parent:strategy；再补一个 hook 段，覆盖连
        # strategy 台账本身都写不成的失败路径。两段都与主管线账本隔离。
        fallback_ctx = C.RunCtx(run_id=ctx.run_id, week=args.week)
        fallback_ctx.metric_update(("strategy_hook",), **failure)
        try:
            db.save_run_log(
                fallback_ctx,
                "strategy_hook",
                status="failed",
                error_message=f"{type(error).__name__}: {error}"[:1000],
                started_at=strategy_started,
                finished_at=datetime.now(),
            )
        except Exception as log_error:
            print(f"!! strategy 独立失败台账写入也失败：{log_error}", file=sys.stderr)
        print(
            f"!! strategy 失败（主管线继续成功退出）："
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return None


def _finalize(args, ctx) -> dict:
    assignment_stat = pipeline.validate_assignment_projection(ctx.run_id, ctx)
    created = [r for r in db.q("""
      SELECT opp_id
        FROM voc_opportunity
       WHERE opp_id LIKE 'OPP2-%%'
         AND classification_state='确定' AND merged_into IS NULL
       ORDER BY opp_id
    """)]
    print(f"[收尾] 仅处理 v3 命名空间 {len(created)} 个有效机会点")

    ctx.metric_update(
        ("finalize",), status="running", planned_targets=len(created),
        completed_targets=0, failed_targets=0, cancelled_targets=0,
        attached_evidence=0, assignment_projection=assignment_stat)
    try:
        split_total = 0
        for row in created:
            try:
                proposal = resolve.detect_split(row["opp_id"])
                if proposal:
                    db.execute(
                        "INSERT INTO voc_proposal(op_type,opp_ids,rationale,week) "
                        "VALUES(%s,%s,%s,%s)",
                        [proposal["op_type"], proposal["opp_ids"],
                         proposal["rationale"], args.week])
                    split_total += 1
                ctx.metric_incr(("finalize",), completed_targets=1)
            except BaseException:
                ctx.metric_incr(("finalize",), failed_targets=1)
                raise

        db.execute("""
          INSERT INTO voc_opp_snapshot(opp_id,week,evi_total,rank_score,
                                       base_total,neg_total,denominator_scope,
                                       title,problem_mode,desc_phenomenon,desc_attribution,
                                       desc_suggestion,prompt_ver,model_ver)
          SELECT o.opp_id,%s,o.evi_total,o.rank_score,
                 CASE WHEN o.opp_type = '老品迭代' THEN (
                   SELECT count(*)::int
                     FROM (
                       SELECT DISTINCT e.message_id, e.seq
                         FROM voc_assign_snapshot s
                         JOIN voc_evidence e
                           ON e.message_id = s.message_id AND e.seq = s.seq
                        WHERE s.run_id = %s
                          AND s.assigned_spu = o.core_tag
                     ) base_rows
                 ) END,
                 CASE WHEN o.opp_type = '老品迭代' THEN (
                   SELECT count(*)::int
                     FROM (
                       SELECT DISTINCT e.message_id, e.seq
                         FROM voc_assign_snapshot s
                         JOIN voc_evidence e
                           ON e.message_id = s.message_id AND e.seq = s.seq
                        WHERE s.run_id = %s
                          AND s.assigned_spu = o.core_tag
                          AND e.sentiment = '负面'
                     ) negative_rows
                 ) END,
                 CASE WHEN o.opp_type = '老品迭代'
                      THEN 'assigned_spu' ELSE 'not_applicable' END,
                 o.title,o.problem_mode,o.desc_phenomenon,o.desc_attribution,
                 o.desc_suggestion,o.prompt_ver,o.model_ver
            FROM voc_opportunity o
           WHERE o.last_week=%s AND o.opp_id LIKE 'OPP2-%%'
          ON CONFLICT (opp_id,week) DO UPDATE SET
            evi_total=EXCLUDED.evi_total, rank_score=EXCLUDED.rank_score,
            base_total=EXCLUDED.base_total, neg_total=EXCLUDED.neg_total,
            denominator_scope=EXCLUDED.denominator_scope,
            title=EXCLUDED.title, problem_mode=EXCLUDED.problem_mode,
            desc_phenomenon=EXCLUDED.desc_phenomenon,
            desc_attribution=EXCLUDED.desc_attribution,
            desc_suggestion=EXCLUDED.desc_suggestion,
            prompt_ver=EXCLUDED.prompt_ver, model_ver=EXCLUDED.model_ver
        """, [args.week, ctx.run_id, ctx.run_id, args.week])
        bad_denominator = int(db.q1("""
          SELECT count(*)
            FROM voc_opp_snapshot s
            JOIN voc_opportunity o ON o.opp_id = s.opp_id
           WHERE s.week = %s
             AND o.opp_id LIKE 'OPP2-%%'
             AND o.opp_type = '老品迭代'
             AND (s.denominator_scope IS DISTINCT FROM 'assigned_spu'
                  OR COALESCE(s.base_total, 0) <= 0
                  OR COALESCE(s.neg_total, 0) > COALESCE(s.base_total, 0))
        """, [args.week]) or 0)
        if bad_denominator:
            raise RuntimeError(
                f"v3 快照分母自检失败：{bad_denominator} 张老品卡分母为空或倒挂")
        revive_total = _check_revive(args.week, ctx)
        # 派生层必须在证据落库与快照完成之后刷新，否则产品页仍读上一轮。
        # 派生层必须由手工收尾刷新；全量重跑不能只更新机会点机器表。
        spu_stat = explode.refresh(args.week)
        print(f"[派生层] SPU {spu_stat['spu_count']} 个 / "
              f"兼容问题 {spu_stat['issue_count']} 条 / "
              f"v2 {spu_stat['v2_issue_count']} 条 / "
              f"v3 {spu_stat['v3_issue_count']} 条")
        nn_stat = pipeline.refresh_opportunity_neighbors(ctx)
        print(f"[最近邻] 刷新 {nn_stat['nn_rows']} 条 / "
              f"悬空 {nn_stat['nn_orphan_rows']} 条")
        # 最近邻断链是收尾失败，必须在改变 PM 可见性之前拦住。
        release = lifecycle.release_to_pm(opp_id_prefix="OPP2-")

        ctx.metric_update(
            ("finalize",), status="completed", opportunities=len(created),
            attached_evidence=0, split_proposals=split_total,
            revive_proposals=revive_total, release=release,
            assignment_projection=assignment_stat,
            spu_layer=spu_stat, opp_nn=nn_stat)
        return {"opportunities": len(created), "attached": 0,
                "splits": split_total, "revive": revive_total,
                "release": release,
                "spu_layer": spu_stat, "opp_nn": nn_stat}
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
    parser.add_argument("--skip-finalize", action="store_true")
    parser.add_argument("--skip-strategy", action="store_true")
    parser.add_argument("--finalize-only", action="store_true")
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
            _run_strategy_after_finalize(args, ctx)
        else:
            if args.full_history:
                window_start = window_end = None
                print("[全历史] 忽略周窗口，在整个事实层上生成")
            else:
                window_start, window_end = ingest.window_bounds(args.week)
            _execute_proposals(args.week, ctx)
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
                _run_strategy_after_finalize(args, ctx)
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

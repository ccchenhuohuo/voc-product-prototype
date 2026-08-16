"""Dagster 资产图（PRD v8 §8.3–8.5）。

周分区设计让【冷启动就是回填】——6 个月历史用 backfill 一次跑完，
与周度增量走完全相同的代码路径，不必维护两套逻辑。
"""
# 注意：本模块【不能】用 from __future__ import annotations —— 它会把
# context 的类型注解变成字符串，Dagster 的运行时类型校验解析不了，
# 报 "Cannot annotate context parameter with type AssetExecutionContext"。
import os
import time
from datetime import datetime, timedelta

from dagster import (AssetCheckResult, AssetExecutionContext, Definitions,
                     MaterializeResult, MetadataValue, RetryPolicy, Backoff,
                     ScheduleDefinition, WeeklyPartitionsDefinition, asset, asset_check,
                     define_asset_job)

from . import config as C, db, execute, explode, ingest, lifecycle, llm, pipeline, resolve

WEEKLY = WeeklyPartitionsDefinition(start_date="2026-02-16", day_offset=0)
RETRY = RetryPolicy(max_retries=C.LLM_RETRY, delay=2, backoff=Backoff.EXPONENTIAL)


def _week(ctx: AssetExecutionContext) -> str:
    d = datetime.fromisoformat(ctx.partition_key)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _is_backfill(ctx: AssetExecutionContext) -> bool:
    """回填模式：零命中仅告警、量级检查跳过（§8.5）。"""
    return len(getattr(ctx, "partition_keys", []) or [ctx.partition_key]) > 1 \
        or datetime.fromisoformat(ctx.partition_key) < datetime.now() - timedelta(days=14)


# ================================================================ 抽取
@asset(partitions_def=WEEKLY, retry_policy=RETRY, group_name="facts",
       description="从已注册来源抽取原始消息与证据三元组")
def voc_facts(context: AssetExecutionContext) -> MaterializeResult:
    week = _week(context)
    rc = C.RunCtx(run_id=f"dag_{week}_{int(time.time())}", week=week,
                  mode="backfill" if _is_backfill(context) else "incremental")
    # T-8 到 T，不是严格周边界：多回溯一天吸收上游迟到数据（§4.1），
    # 重复部分由 message_id 主键幂等吸收
    start, end = ingest.window_bounds(week)
    st = ingest.ingest_window(rc, start, end)
    ingest.snapshot_taxonomy(week)
    db.save_run_log(rc, "ingest", window_start=start, window_end=end,
                    matched_rows=sum(st.get("source_messages", {}).values()),
                    exported_rows=st["messages_written"], status="success",
                    started_at=start, finished_at=datetime.now())
    return MaterializeResult(metadata={
        "各来源消息": MetadataValue.json(st.get("source_messages", {})),
        "证据写入": st["evidence_written"], "尾部修复": st["tail_fixed"],
        "数组错位": st["misaligned"], "耗时秒": st["elapsed_s"],
        "模式": rc.mode})


@asset_check(asset=voc_facts, blocking=True, description="任一抽取分片零命中即阻断（回填模式仅告警）")
def check_no_zero_match(context) -> AssetCheckResult:
    n = db.q1("""SELECT count(*) FROM voc_run_log
                  WHERE stage='ingest' AND status='failed'
                    AND error_message LIKE '%零命中%'
                    AND started_at > now() - interval '1 hour'""") or 0
    return AssetCheckResult(passed=(n == 0),
                            metadata={"零命中失败批次": n,
                                      "说明": "matched_count=0 与'本周无数据'无法区分，增量模式下必须阻断"})


@asset_check(asset=voc_facts, blocking=True, description="消息量低于前四周均值 50% 即阻断；基线不足 4 周时跳过")
def check_volume_not_collapsed(context) -> AssetCheckResult:
    rows = db.q("""SELECT week, exported_rows FROM voc_run_log
                    WHERE stage='ingest' AND status='success'
                    ORDER BY week DESC LIMIT 5""")
    if len(rows) < 5:
        return AssetCheckResult(passed=True, metadata={"跳过": "基线不足 4 周（回填最早分区必然如此）"})
    cur = rows[0]["exported_rows"] or 0
    base = sum(r["exported_rows"] or 0 for r in rows[1:]) / 4
    return AssetCheckResult(passed=(cur >= base * 0.5),
                            metadata={"本周": cur, "前四周均值": round(base, 1)})


@asset_check(asset=voc_facts, blocking=True, description="标签/情感数组错位占比 >1% 即阻断")
def check_array_alignment(context) -> AssetCheckResult:
    mis = db.q1("""SELECT COALESCE(sum((metrics->'ingest'->>'misaligned')::int),0)
                     FROM voc_run_log WHERE stage='ingest'""") or 0
    tot = db.q1("SELECT count(*) FROM voc_evidence") or 1
    ratio = mis / tot
    return AssetCheckResult(passed=(ratio <= 0.01),
                            metadata={"错位条数": mis, "证据总数": tot,
                                      "占比": round(ratio * 100, 4)})


# ================================================================ 执行 PM 裁决
@asset(partitions_def=WEEKLY, deps=[voc_facts], group_name="resolve",
       description="执行 PM 已通过的 MERGE / SPLIT 提案，写血缘（二期 §6.3）")
def execute_proposals(context: AssetExecutionContext) -> MaterializeResult:
    """必须排在生成【之前】：合并会改变去重版图，本周新证据应挂到合并后的
    目标条目上。看板只能把提案标成 accepted（voc_human 无机器表写权限），
    真正落地在这里。"""
    week = _week(context)
    # 静默自动融合【默认关闭】。它属于 PM 交互设计的一部分，而该设计正在重新
    # 梳理；开着会让每次周度运行继续改写机会点层（搬移证据、置 merged_into），
    # 给后续设计讨论带来既成事实。设计定稿后再由 VOC_AUTO_MERGE=1 打开。
    auto = (execute.run_auto(week)
            if os.environ.get("VOC_AUTO_MERGE") == "1" else {"auto_accepted": "已关闭"})
    stat = execute.run(week)
    return MaterializeResult(metadata={**auto, **stat,
                                       "说明": "仅执行 PM 明确通过的提案；静默融合需显式开启"})


# ================================================================ 生成
def _save_generation_log(rc: C.RunCtx, status: str, started_at: datetime,
                         window_start, window_end,
                         error: BaseException | None = None) -> dict | None:
    """将生成帐本与 LLM 客户端计数固化到 run log。"""
    pipeline.sync_llm_usage(rc)
    generation = rc.metrics.get("generation", {})
    reconciliation = (pipeline.generation_reconciliation(rc)
                      if generation else None)
    db.save_run_log(
        rc, "generate", status=status,
        window_start=window_start, window_end=window_end,
        matched_rows=rc.metrics.get("pool", {}).get("scoped_evidence_rows"),
        exported_rows=generation.get("completed_persistence"),
        batch_count=(reconciliation or {}).get(
            "planned_batches", (reconciliation or {}).get("planned_buckets")),
        unclassified_rows=generation.get("unclassified_rows"),
        error_message=(f"{type(error).__name__}: {error}"[:1000]
                       if error else None),
        started_at=started_at, finished_at=datetime.now())
    return reconciliation


@asset(partitions_def=WEEKLY, deps=[voc_facts, execute_proposals],
       group_name="generate", op_tags={"voc/llm": "true"},
       description="统一内容池 → 前置分类 → 按老品迭代/新品创新生成与消解")
def opportunities_by_lifecycle(context: AssetExecutionContext) -> MaterializeResult:
    """生成入口只跑一次统一证据池，来源不再是 Dagster 分支。

    本资产故意不配置 ``retry_policy``：底层只对可重试的限流/网络
    失败做局部重试，欠费、鉴权和硬配额失败不得由调度器重跑。
    """
    week = _week(context)
    rc = C.RunCtx(run_id=f"dag_gen_{week}_{int(time.time())}", week=week)
    started_at = datetime.now()
    window_start, window_end = ingest.window_bounds(week)
    llm.reset_usage()
    try:
        result = pipeline.generate_opportunities(
            week, rc, week_start=window_start, week_end=window_end)
        reconciliation = pipeline.generation_reconciliation(rc)
        if not reconciliation["complete"]:
            raise RuntimeError(f"生成对账失败：{reconciliation}")
        _save_generation_log(
            rc, "success", started_at, window_start, window_end)
        usage = llm.usage()
        pool = rc.metrics.get("pool", {})
        return MaterializeResult(metadata={
            "证据池行数": result["pool_rows"],
            "无效证据": result["invalid_rows"],
            "分类后行数": MetadataValue.json(pool.get("by_lifecycle", {})),
            "生成机会点": len(set(result["created"])),
            "对账": MetadataValue.json(reconciliation),
            "LLM调用": usage["calls"], "tokens": usage["tokens"],
        })
    except BaseException as error:
        try:
            _save_generation_log(
                rc, "failed", started_at, window_start, window_end, error)
        except Exception as log_error:
            if hasattr(error, "add_note"):
                error.add_note(f"写入 failed run log 也失败：{log_error}")
        raise


@asset_check(asset=opportunities_by_lifecycle, blocking=False,
             description="Stage4 打回率 >40% 告警")
def check_stage4_reject_rate(context) -> AssetCheckResult:
    tot = db.q1("SELECT count(*) FROM voc_opportunity") or 1
    nr = db.q1("SELECT count(*) FROM voc_opportunity WHERE needs_review") or 0
    return AssetCheckResult(passed=(nr / tot <= 0.4),
                            metadata={"needs_review": nr, "总数": tot,
                                      "打回率%": round(nr / tot * 100, 1)})


# ================================================================ 消解与交付
@asset(partitions_def=WEEKLY, deps=[opportunities_by_lifecycle],
       group_name="resolve", op_tags={"voc/llm": "true"},
       description="同生命周期跨来源补证，来源数量可扩展（§6.5）")
def cross_source_merged(context: AssetExecutionContext) -> MaterializeResult:
    """跨来源收尾也不配置 Dagster 重试，任一目标失败即整体失败。"""
    week = _week(context)
    rc = C.RunCtx(run_id=f"dag_cross_{week}_{int(time.time())}", week=week)
    started_at = datetime.now()
    llm.reset_usage()
    try:
        rows = db.q("""
          SELECT opp_id, mode_vec::text AS v, opp_type, source_lines
            FROM voc_opportunity
           WHERE last_week=%s
             AND classification_state='确定'
             AND merged_into IS NULL
             AND mode_vec IS NOT NULL
           ORDER BY opp_id
        """, [week])
        rc.metric_update(
            ("cross_source",), planned_targets=len(rows), completed_targets=0,
            failed_targets=0, cancelled_targets=0, attached_evidence=0)
        total = 0
        for row in rows:
            try:
                opp_type, source_lines = pipeline.validate_lifecycle_sources(row)
                vec = [float(value) for value in row["v"].strip("[]").split(",")]
                attached = resolve.cross_source_merge(
                    row["opp_id"], vec, opp_type, source_lines, week, rc)
                attached_count = 0
                if attached:
                    # 关系与 recount 同事务；分类与来源统计不会半更新。
                    attached_count = pipeline.attach_evidence(row["opp_id"], attached)
                    total += attached_count
                rc.metric_incr(
                    ("cross_source",), completed_targets=1,
                    attached_evidence=attached_count)
            except Exception:
                rc.metric_incr(("cross_source",), failed_targets=1)
                raise
        pipeline.sync_llm_usage(rc)
        db.save_run_log(
            rc, "cross_source", status="success", matched_rows=len(rows),
            exported_rows=total, batch_count=len(rows), started_at=started_at,
            finished_at=datetime.now())
        multi_source = db.q1(
            "SELECT count(*) FROM voc_opportunity WHERE dual_source") or 0
        return MaterializeResult(metadata={
            "跨来源挂载": total, "多来源印证条目": multi_source,
            "处理机会点": len(rows),
        })
    except BaseException as error:
        cross = rc.metrics.get("cross_source", {})
        if cross:
            rc.metric_update(
                ("cross_source",),
                cancelled_targets=max(
                    int(cross.get("planned_targets", 0))
                    - int(cross.get("completed_targets", 0))
                    - int(cross.get("failed_targets", 0)), 0))
        try:
            pipeline.sync_llm_usage(rc)
            db.save_run_log(
                rc, "cross_source", status="failed",
                matched_rows=cross.get("planned_targets"),
                exported_rows=cross.get("attached_evidence"),
                batch_count=cross.get("planned_targets"),
                error_message=f"{type(error).__name__}: {error}"[:1000],
                started_at=started_at, finished_at=datetime.now())
        except Exception as log_error:
            if hasattr(error, "add_note"):
                error.add_note(f"写入 failed run log 也失败：{log_error}")
        raise


@asset(partitions_def=WEEKLY, deps=[cross_source_merged], group_name="resolve",
       description="纯 SQL 展开：SPU 容器、问题条目与共性度")
def spu_layer(context: AssetExecutionContext) -> MaterializeResult:
    stat = explode.refresh(_week(context))
    return MaterializeResult(metadata={
        "SPU卡": stat["spu_count"],
        "问题条目": stat["issue_count"],
        "n_eff覆盖": stat["n_eff_count"],
        "分区周": stat["week"],
    })


@asset(partitions_def=WEEKLY, deps=[spu_layer], group_name="resolve",
       description="拆分检测 + 墓碑唤醒 + 陈旧检测 → 提案队列")
def proposals(context: AssetExecutionContext) -> MaterializeResult:
    week = _week(context)
    n_split = 0
    for r in db.q("SELECT opp_id FROM voc_opportunity WHERE last_week=%s", [week]):
        p = resolve.detect_split(r["opp_id"])
        if p:
            db.execute("INSERT INTO voc_proposal(op_type,opp_ids,rationale,week) "
                       "VALUES(%s,%s,%s,%s)",
                       [p["op_type"], p["opp_ids"], p["rationale"], week])
            n_split += 1
    n_revive = lifecycle.check_revive(week)
    stale = lifecycle.stale_items()
    pend = db.q1("SELECT count(*) FROM voc_proposal WHERE status='pending'") or 0
    return MaterializeResult(metadata={"拆分提议": n_split, "墓碑唤醒": n_revive,
                                       "陈旧条目": len(stale), "待决提案": pend})


@asset(partitions_def=WEEKLY, deps=[proposals], group_name="resolve",
       description="周度快照：含分母与内容版本，支撑回滚与闭环验证")
def snapshots(context: AssetExecutionContext) -> MaterializeResult:
    week = _week(context)
    db.execute("""
      INSERT INTO voc_opp_snapshot(opp_id,week,evi_total,rank_score,base_total,neg_total,
                                   title,problem_mode,desc_phenomenon,desc_attribution,
                                   desc_suggestion,prompt_ver,model_ver)
      SELECT o.opp_id,%s,o.evi_total,o.rank_score,
             (SELECT count(*) FROM voc_evidence e
               WHERE e.is_product AND e.tag IS NOT DISTINCT FROM o.core_tag),
             (SELECT count(*) FROM voc_evidence e
               WHERE e.is_product AND e.sentiment='负面'
                 AND e.tag IS NOT DISTINCT FROM o.core_tag),
             o.title,o.problem_mode,o.desc_phenomenon,o.desc_attribution,
             o.desc_suggestion,o.prompt_ver,o.model_ver
        FROM voc_opportunity o
      ON CONFLICT (opp_id,week) DO UPDATE SET
        evi_total=EXCLUDED.evi_total, rank_score=EXCLUDED.rank_score,
        title=EXCLUDED.title, desc_suggestion=EXCLUDED.desc_suggestion""", [week])
    m = db.q("SELECT * FROM voc_weekly_metrics")[0]
    return MaterializeResult(metadata={k: (v if v is not None else 0) for k, v in m.items()})


@asset(partitions_def=WEEKLY, deps=[snapshots], group_name="deliver",
       op_tags={"voc/external-writer": "lark"},
       description="放行策略：安全类/双源印证/Top-N 进 PM 视野，其余留 backlog（§12.2）")
def release_to_pm(context: AssetExecutionContext) -> MaterializeResult:
    # 实现在 lifecycle 里，与 scripts/run_generate.py 共用同一份——
    # 早期这段 SQL 只存在于本资产内，脚本路径跑完全不放行。
    stat = lifecycle.release_to_pm()
    return MaterializeResult(metadata={**stat, "周度推送上限": C.WEEKLY_PUSH_CAP})


weekly_job = define_asset_job("voc_weekly", selection="*", partitions_def=WEEKLY)
weekly_schedule = ScheduleDefinition(job=weekly_job, cron_schedule="0 2 * * 1",
                                     execution_timezone="Asia/Shanghai")

defs = Definitions(
    assets=[voc_facts, execute_proposals, opportunities_by_lifecycle,
            cross_source_merged, spu_layer, proposals, snapshots, release_to_pm],
    asset_checks=[check_no_zero_match, check_volume_not_collapsed,
                  check_array_alignment, check_stage4_reject_rate],
    jobs=[weekly_job], schedules=[weekly_schedule])

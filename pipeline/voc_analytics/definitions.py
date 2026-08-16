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
from .stages import stage1

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
       description="从云听抽取两条线的原始消息与证据三元组")
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
                    exported_rows=st["messages_written"], status="success",
                    started_at=start, finished_at=datetime.now())
    return MaterializeResult(metadata={
        "电商消息": st.get("电商_messages", 0), "社媒消息": st.get("社媒_messages", 0),
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
@asset(partitions_def=WEEKLY, deps=[voc_facts, execute_proposals], retry_policy=RETRY,
       group_name="generate", op_tags={"voc/llm": "true"},
       description="线A：分桶 → Stage1 分批投票 → Stage2/3/4 → 消解")
def opportunities_line_a(context: AssetExecutionContext) -> MaterializeResult:
    week = _week(context)
    rc = C.RunCtx(run_id=f"genA_{week}_{int(time.time())}", week=week)
    llm.reset_usage()
    # 【必须按分区周过滤】。早期这里不传日期，等于每周把 6 个月的池子整个重算一遍：
    # 成本随历史线性膨胀，而且同一批证据每周重新分组、重新命名，产出的 opp_id
    # 跟着漂移——PM 上周标的「项目中」下周就找不到对应条目了。
    # 窗口与抽取共用 window_bounds（T-8 到 T），否则迟到数据入了库却永远不被生成看到。
    ws, we = ingest.window_bounds(week)
    buckets = pipeline.bucket_line_a(ws, we)
    ranked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    created, groups_total = [], 0
    hist = [r["problem_mode"] for r in db.q(
        "SELECT problem_mode FROM voc_opportunity WHERE problem_mode IS NOT NULL "
        "ORDER BY opp_id LIMIT 20")]
    for (cat, tag), items in ranked:
        info = {"category": cat, "tag": tag, "tax_path": items[0].get("tax_path", ""),
                "prod_line": "灯光" if "灯光" in (cat or "") else "支撑"}
        split = stage1.split_bucket(items, "线A", info, rc)   # vote 由 config.VOTE_ENABLED 决定
        gs = stage1.merge_similar_modes(split["groups"], rc)
        groups_total += len(gs)
        db.save_unclassified([(items[i]["message_id"], items[i]["seq"])
                              for i in split["unclassified"]], week, "unclassified")
        db.save_unclassified([(items[i]["message_id"], items[i]["seq"])
                              for i in split["dropped"]], week, "vote_dropped")
        # 必须落库。早期这里只 append 进列表就完了，Dagster 路径跑完
        # 一条机会点都没写进数据库（落库逻辑当时只在 scripts/run_generate.py 里）。
        built = [o for o in llm.parallel_map(
            lambda g: pipeline.build_opportunity(items, g, "线A", info, rc, hist), gs)
            if isinstance(o, dict)]
        created += pipeline.persist_opportunities([(o, items) for o in built], week, rc)
    u = llm.usage()
    db.save_run_log(rc, "generate_a", status="success")
    return MaterializeResult(metadata={
        "桶数": len(ranked), "模式组": groups_total, "产出": len(created),
        "LLM调用": u["calls"], "tokens": u["tokens"], "失败模式": rc.llm_failed_modes})


@asset(partitions_def=WEEKLY, deps=[voc_facts, execute_proposals], retry_policy=RETRY,
       group_name="generate", op_tags={"voc/llm": "true"},
       description="线B：诉求门 → Stage1' → Stage2'/3/4（需求缺口 + 竞品对标）")
def opportunities_line_b(context: AssetExecutionContext) -> MaterializeResult:
    week = _week(context)
    rc = C.RunCtx(run_id=f"genB_{week}_{int(time.time())}", week=week)
    llm.reset_usage()
    # 早期这个资产只统计候选数就返回了，从不真正产出机会点——线B 的完整管线
    # 只存在于 scripts/run_generate.py 里。Dagster 是唯一的调度入口（§8），
    # 资产里缺一段就等于周度调度根本不跑线B。这里与脚本对齐。
    ws, we = ingest.window_bounds(week)   # 与抽取同窗口，见 voc_facts
    out: dict = {}
    created: list[str] = []
    for channel, types in (("需求缺口", list(C.DEWATER_GAP)),
                           ("竞品对标", list(C.DEWATER_COMP))):
        rows = db.line_b_pool(types, ws, we, multi_brand_only=(channel == "竞品对标"))
        if channel == "需求缺口":
            rows, st = pipeline.intent_gate(rows, rc)
            out[f"{channel}_诉求门"] = st
        out[f"{channel}_候选"] = len(rows)
        if not rows:
            continue
        info = {"channel": channel, "category": "SOCIAL-NA", "prod_line": "未定"}
        split = stage1.split_bucket(rows, "线B", info, rc)
        groups = stage1.merge_similar_modes(split["groups"], rc)
        # 线B 的未归类逐条独立成组（R11：弱证据也是机会，不可错过）
        groups += [{"mode_name": (rows[u].get("content") or "")[:40], "members": [u]}
                   for u in split["unclassified"]]
        hist = [r["problem_mode"] for r in db.q(
            "SELECT problem_mode FROM voc_opportunity WHERE src_line='线B' "
            "AND problem_mode IS NOT NULL ORDER BY opp_id LIMIT 20")]
        built = llm.parallel_map(
            lambda g: pipeline.build_opportunity(
                rows, g, "线B", {**info, "tag": g["mode_name"][:60]}, rc, hist), groups)
        pairs = [(o, rows) for o in built if isinstance(o, dict)]
        created += pipeline.persist_opportunities(pairs, week, rc)
        out[f"{channel}_产出"] = len(pairs)
    u = llm.usage()
    db.save_run_log(rc, "generate_b", status="success")
    return MaterializeResult(metadata={**{k: MetadataValue.json(v) if isinstance(v, dict) else v
                                          for k, v in out.items()},
                                       "产出合计": len(set(created)),
                                       "LLM调用": u["calls"], "tokens": u["tokens"]})


@asset_check(asset=opportunities_line_a, blocking=False,
             description="Stage4 打回率 >40% 告警")
def check_stage4_reject_rate(context) -> AssetCheckResult:
    tot = db.q1("SELECT count(*) FROM voc_opportunity") or 1
    nr = db.q1("SELECT count(*) FROM voc_opportunity WHERE needs_review") or 0
    return AssetCheckResult(passed=(nr / tot <= 0.4),
                            metadata={"needs_review": nr, "总数": tot,
                                      "打回率%": round(nr / tot * 100, 1)})


# ================================================================ 消解与交付
@asset(partitions_def=WEEKLY, deps=[opportunities_line_a, opportunities_line_b],
       retry_policy=RETRY, group_name="resolve", op_tags={"voc/llm": "true"},
       description="跨线汇聚：dual_source 成立的唯一途径（§6.5）")
def cross_line_merged(context: AssetExecutionContext) -> MaterializeResult:
    week = _week(context)
    rc = C.RunCtx(run_id=f"cross_{week}_{int(time.time())}", week=week)
    rows = db.q("SELECT opp_id, mode_vec::text v, core_tag, src_line FROM voc_opportunity "
                "WHERE last_week=%s AND mode_vec IS NOT NULL", [week])
    total = 0
    for r in rows:
        vec = [float(x) for x in r["v"].strip("[]").split(",")]
        n = resolve.cross_line_merge(r["opp_id"], vec, r["core_tag"],
                                     r["src_line"], week, rc)
        if n:
            # 必须重算：dual_source 由触发器从 evi_ec/evi_social 派生，
            # 而这两个字段只有 recount 会更新。不调它，挂载了对侧证据
            # dual_source 依然是 false，跨线汇聚等于白做。
            pipeline.recount(r["opp_id"])
            total += n
    dual = db.q1("SELECT count(*) FROM voc_opportunity WHERE dual_source") or 0
    return MaterializeResult(metadata={"跨线挂载": total, "双源印证条目": dual})


@asset(partitions_def=WEEKLY, deps=[cross_line_merged], group_name="resolve",
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
    assets=[voc_facts, execute_proposals, opportunities_line_a, opportunities_line_b,
            cross_line_merged, spu_layer, proposals, snapshots, release_to_pm],
    asset_checks=[check_no_zero_match, check_volume_not_collapsed,
                  check_array_alignment, check_stage4_reject_rate],
    jobs=[weekly_job], schedules=[weekly_schedule])

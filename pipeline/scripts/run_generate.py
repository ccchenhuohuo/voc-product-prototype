#!/usr/bin/env python3
"""M2/M3：生成 + 消解 + 跨线汇聚（PRD v8 §5–6）。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python scripts/run_generate.py --week 2026-W33 --limit-buckets 3
  .venv/bin/python scripts/run_generate.py --week 2026-W33 --line B --limit 200
  .venv/bin/python scripts/run_generate.py --week 2026-W33 --skip-cross
  .venv/bin/python scripts/run_generate.py --week 2026-W33 --cross-only

前置条件：
  从 /home/sdy/voc-analytics 运行；Python 3.11+ 及项目依赖已安装；.env 中的数据库、
  百炼/LLM 配置已按上例导出；数据库已完成当前迁移且事实层已有待处理数据；
  --cross-only 仅在两条线均已落库后运行。--week 标记产出周，并不限定事实层查询
  窗口。本脚本会调用 LLM，并写入机会点、汇聚/拆分、快照和放行相关数据。
"""
from __future__ import annotations
import argparse, sys, time
from datetime import datetime

sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import config as C, db, lifecycle, llm, pipeline, resolve  # noqa: E402
from voc_analytics.stages import stage1  # noqa: E402


# 落库逻辑已移入 voc_analytics.pipeline，与 Dagster 资产共用——
# 这里保留同名薄封装，只是把 verbose 打开（脚本要在终端看进度）。
recount = pipeline.recount


def _persist(pairs: list, week: str, ctx) -> list[str]:
    return pipeline.persist_opportunities(pairs, week, ctx, verbose=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", required=True)
    ap.add_argument("--line", choices=["A", "B", "both"], default="both")
    ap.add_argument("--limit-buckets", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-vote", action="store_true")
    ap.add_argument("--all", action="store_true")
    # 跨线汇聚要求【两条线都已落库】。两条线并行跑时先完成的那条做汇聚，
    # 看到的对侧是残缺的——实测电商 13:00 收工时社媒还在跑（13:41 才完），
    # 这是 dual_source 只有 1 的第二个原因。故拆成两段：
    #   并行阶段  --skip-cross  两条线各自生成
    #   收尾阶段  --cross-only  两条线都完成后统一做汇聚/拆分/快照/放行
    ap.add_argument("--skip-cross", action="store_true",
                    help="只生成，不做跨线汇聚与收尾（供两条线并行时用）")
    ap.add_argument("--cross-only", action="store_true",
                    help="只做跨线汇聚与收尾，对库内全部机会点执行")
    a = ap.parse_args()
    if a.cross_only:
        a.line = "none"

    ctx = C.RunCtx(run_id=f"gen_{a.week}_{int(time.time())}", week=a.week)
    llm.reset_usage()
    t0 = time.time()
    created: list[str] = []

    # ---------------- 电商 ----------------
    if a.line in ("A", "both"):
        buckets = pipeline.bucket_line_a()
        ranked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        if a.limit_buckets:
            ranked = ranked[:a.limit_buckets]
        print(f"[电商] 桶数 {len(ranked)}  证据合计 {sum(len(v) for _, v in ranked)}")
        history: list[str] = [r["problem_mode"] for r in
                              db.q("SELECT problem_mode FROM voc_opportunity "
                                   "WHERE problem_mode IS NOT NULL ORDER BY opp_id LIMIT 20")]
        # 桶级并行：早期桶是串行处理的，12 个桶排队跑，并发配额闲置。
        # 现在整桶（Stage1 + 组级生成）作为一个任务并行，内部再并行。
        def _bucket(entry):
            (cat, tag), items = entry
            info = {"category": cat, "tag": tag,
                    "tax_path": items[0].get("tax_path", ""),
                    "prod_line": "灯光" if "灯光" in (cat or "") else "支撑"}
            split = stage1.split_bucket(items, "电商", info, ctx, vote=not a.no_vote)
            groups = stage1.merge_similar_modes(split["groups"], ctx)
            db.save_unclassified([(items[i]["message_id"], items[i]["seq"])
                                  for i in split["unclassified"]], a.week, "unclassified")
            db.save_unclassified([(items[i]["message_id"], items[i]["seq"])
                                  for i in split["dropped"]], a.week, "vote_dropped")
            print(f"  {cat}/{tag}  n={len(items)}  组={len(groups)}  "
                  f"未归类={len(split['unclassified'])}", flush=True)
            built = llm.parallel_map(
                lambda g: pipeline.build_opportunity(items, g, "电商", info, ctx, history),
                groups)
            return [(o, items) for o in built if isinstance(o, dict)]

        # 桶级并行度 = 桶数：全部桶同时开跑。总并发由 llm._GATE 这个全局信号量
        # 压在拐点上，不再靠"桶级 8 × 桶内 64"这种乘法去猜，所以这里放开没有风险。
        # 用 imap 而非 map：map 要等【所有】桶跑完才返回，冷启动一个多小时
        # 中途崩溃则一条都没入库。imap 谁先完成谁先落库，崩了只丢在跑的那几桶。
        for batch in llm.parallel_imap(_bucket, ranked, workers=len(ranked) or 1):
            if not isinstance(batch, list):
                continue
            created += _persist(batch, a.week, ctx)

    # ---------------- 社媒 ----------------
    if a.line in ("B", "both"):
        for channel, types in (("需求缺口", list(C.DEWATER_GAP)),
                               ("竞品对标", list(C.DEWATER_COMP))):
            rows = db.line_b_pool(types, multi_brand_only=(channel == "竞品对标"))
            if a.limit:
                rows = rows[:a.limit]
            print(f"[社媒/{channel}] 候选 {len(rows)}")
            if channel == "需求缺口":
                rows, st = pipeline.intent_gate(rows, ctx)
                print(f"  诉求门: 规则通过 {st['rule_pass']} -> 缺口 {st['passed']}  {st['intent']}")
            if not rows:
                continue
            info = {"channel": channel, "category": "SOCIAL-NA", "prod_line": "未定"}
            split = stage1.split_bucket(rows, "社媒", info, ctx, vote=not a.no_vote)
            groups = stage1.merge_similar_modes(split["groups"], ctx)
            print(f"  组={len(groups)}  未归类={len(split['unclassified'])}")
            # 社媒的 unclassified 逐条独立成组（R11：任何机会不可错过）
            for u in split["unclassified"]:
                groups.append({"mode_name": (rows[u].get("content") or "")[:40], "members": [u]})
            history = [r["problem_mode"] for r in
                       db.q("SELECT problem_mode FROM voc_opportunity "
                            "WHERE src_line='社媒' AND problem_mode IS NOT NULL "
                            "ORDER BY opp_id LIMIT 20")]
            built = llm.parallel_map(
                lambda g: pipeline.build_opportunity(
                    rows, g, "社媒", {**info, "tag": g["mode_name"][:60]}, ctx, history),
                groups)
            created += _persist([(o, rows) for o in built if isinstance(o, dict)],
                                a.week, ctx)

    if a.skip_cross:
        u = llm.usage()
        db.save_run_log(ctx, f"generate_{a.line}", status="success",
                        started_at=datetime.fromtimestamp(t0), finished_at=datetime.now())
        print(f"\n生成完成：{len(set(created))} 个机会点（跨线汇聚与收尾留给 --cross-only）")
        print(f"LLM: {u['calls']} 次调用 / {u['tokens']} tokens / 耗时 {time.time()-t0:.0f}s")
        return 0

    # --cross-only 时对库内全部机会点做收尾，而不是只对本进程新建的那批
    if a.cross_only:
        created = [r["opp_id"] for r in db.q("SELECT opp_id FROM voc_opportunity")]
        print(f"[收尾] 覆盖库内全部 {len(created)} 个机会点")

    # ---------------- 跨线汇聚 ----------------
    print("\n[跨线汇聚]")
    def _cross(oid: str) -> tuple[str, int]:
        row = db.q("SELECT mode_vec::text v, core_tag, src_line FROM voc_opportunity "
                   "WHERE opp_id=%s", [oid])
        if not row or not row[0]["v"]:
            return oid, 0
        vec = [float(x) for x in row[0]["v"].strip("[]").split(",")]
        return oid, resolve.cross_line_merge(oid, vec, row[0]["core_tag"],
                                             row[0]["src_line"], a.week, ctx)
    n_cross = 0
    for res in llm.parallel_map(_cross, sorted(set(created))):
        if not isinstance(res, tuple):
            continue
        oid, k = res
        if k:
            recount(oid); n_cross += k
            print(f"  {oid}  +{k} 条对侧证据", flush=True)
    print(f"  跨线挂载合计 {n_cross} 条")

    # ---------------- 拆分检测 + 快照 ----------------
    n_split = 0
    for oid in set(created):
        p = resolve.detect_split(oid)
        if p:
            db.execute("INSERT INTO voc_proposal(op_type,opp_ids,rationale,week) "
                       "VALUES(%s,%s,%s,%s)", [p["op_type"], p["opp_ids"], p["rationale"], a.week])
            n_split += 1
    db.execute("""
      INSERT INTO voc_opp_snapshot(opp_id,week,evi_total,rank_score,base_total,neg_total,
                                   title,problem_mode,desc_phenomenon,desc_attribution,
                                   desc_suggestion,prompt_ver,model_ver)
      SELECT o.opp_id,%s,o.evi_total,o.rank_score,
             (SELECT count(*) FROM voc_evidence e JOIN voc_message m USING(message_id)
               WHERE e.is_product AND e.tag IS NOT DISTINCT FROM o.core_tag),
             (SELECT count(*) FROM voc_evidence e JOIN voc_message m USING(message_id)
               WHERE e.is_product AND e.sentiment='负面' AND e.tag IS NOT DISTINCT FROM o.core_tag),
             o.title,o.problem_mode,o.desc_phenomenon,o.desc_attribution,
             o.desc_suggestion,o.prompt_ver,o.model_ver
        FROM voc_opportunity o WHERE o.last_week=%s
      ON CONFLICT (opp_id,week) DO UPDATE SET
        evi_total=EXCLUDED.evi_total, rank_score=EXCLUDED.rank_score,
        title=EXCLUDED.title, desc_suggestion=EXCLUDED.desc_suggestion""", [a.week, a.week])

    # 放行：与 Dagster 的 release_to_pm 资产共用同一实现。脚本路径早期漏了这步，
    # 冷启动 593 条全留在 backlog，PM 侧一条都看不到。
    print(f"\n[放行] {lifecycle.release_to_pm()}")

    u = llm.usage()
    db.save_run_log(ctx, "generate", status="success",
                    started_at=datetime.fromtimestamp(t0), finished_at=datetime.now())
    print(f"\n完成：新建/挂载 {len(set(created))} 个机会点，拆分提议 {n_split} 条")
    print(f"LLM: {u['calls']} 次调用 / {u['tokens']} tokens / "
          f"失败模式 {ctx.llm_failed_modes} / 耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

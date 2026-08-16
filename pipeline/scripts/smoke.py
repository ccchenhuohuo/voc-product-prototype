#!/usr/bin/env python3
"""机会点生成与收尾最小探针：基于已有事实层跑小切片并执行验收口径。

为什么需要它——冷启动跑了两轮才发现的问题，本来都该在这里被挡住：
  · 放行策略从没执行（逻辑只在 Dagster 资产里，脚本路径绕过）
  · Dagster 生成资产 build 完不落库
  · 跨来源汇聚 350/350 空转（旧按渠道分组使 core_tag 取值域不相交）
  · problem_mode 退化成分类名，593 条只有 63 个不同向量
  · opp_id 碰撞导致「evi_total=91 而描述只讲了 1 条」
上一个探针漏掉它们，是因为：在脏库上跑、被 timeout 砍在跨线汇聚之前、只跑了电商。
所以本脚本的三条硬要求是：干净切片、跑到最后一步、两个生命周期都覆盖。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python scripts/smoke.py            # 脚本路径；哨兵周产出结束时清理
  .venv/bin/python scripts/smoke.py --keep     # 脚本路径；保留哨兵周产出供检查
  .venv/bin/python scripts/smoke.py --dagster  # 物化最近真实 ingest 周分区，不自动清理

前置条件：
  从 /home/sdy/voc-analytics 运行；Python 3.11+ 及项目依赖已安装；数据库与百炼/LLM
  配置已按上例导出，事实层已有探针样本，且没有 run_generate.py 正在运行；宿主机
  需提供 pgrep。--dagster 还要求 dagster 命令在 PATH 中且资产入口可加载。所有模式
  都会写数据库，勿并发运行。
"""
from __future__ import annotations
import argparse, re, subprocess, sys, time, unicodedata

sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import (  # noqa: E402
    config as C, db, lifecycle, llm, pipeline, resolve, routing,
)
from voc_analytics.stages import stage1, validate  # noqa: E402

SMOKE_WEEK = "9999-W01"          # 哨兵周：探针产出全部打这个标，便于精确清理
P = F = 0
NOTE: list[str] = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    global P, F
    if ok:
        P += 1; print(f"  PASS  {name}" + (f"   {detail}" if detail else ""))
    else:
        F += 1; print(f"  FAIL  {name}   {detail}")


def cleanup() -> None:
    """原子清理哨兵周产出，不删真实机会点或事实证据。"""
    with db.conn() as c:
        ids = [r["opp_id"] for r in c.execute(
            "SELECT opp_id FROM voc_opportunity WHERE first_week=%s", [SMOKE_WEEK]).fetchall()]
        attached_ids = [r["opp_id"] for r in c.execute(
            "SELECT DISTINCT opp_id FROM voc_opp_evidence WHERE attach_week=%s",
            [SMOKE_WEEK]).fetchall()]
        real_targets = sorted(set(attached_ids) - set(ids))

        # 先锁所有将改变证据集的机会点。即使上次探针已没有哨兵机会点，
        # attach_week 残留关系也必须被清理，不能因 ids 为空提前返回。
        pipeline.lock_opportunities(c, [*ids, *real_targets])

        # 两条删除路径缺一不可：按 opp_id 删哨兵机会自己的挂靠；
        # 按 attach_week 删探针挂到真实机会上的哨兵关系。
        if ids:
            c.execute("DELETE FROM voc_opp_evidence WHERE opp_id = ANY(%s)", [ids])
        c.execute("DELETE FROM voc_opp_evidence WHERE attach_week = %s", [SMOKE_WEEK])

        if ids:
            c.execute("DELETE FROM voc_opp_snapshot WHERE opp_id = ANY(%s)", [ids])
            # 血缘表存的是数组（parent_ids/child_ids），不是单值列。
            c.execute("DELETE FROM voc_opp_lineage WHERE parent_ids && %s OR child_ids && %s",
                      [ids, ids])
        c.execute("DELETE FROM voc_proposal WHERE week=%s", [SMOKE_WEEK])
        c.execute("DELETE FROM voc_unclassified_evidence WHERE week=%s", [SMOKE_WEEK])
        if ids:
            c.execute("DELETE FROM voc_opportunity WHERE opp_id = ANY(%s)", [ids])

        # 删掉挂到真实机会的哨兵关系后，在同一事务内恢复权威分类与计数。
        for opp_id in real_targets:
            pipeline.recount(opp_id, c)

    print(f"  已清理哨兵机会 {len(ids)} 条，回算真实目标 {len(real_targets)} 条")


def guard_no_concurrent_run() -> None:
    """真实生成在跑时不许探针动手——两边会互相把对方当去重目标。"""
    # 必须锚定到 python 进程本身：裸 "run_generate.py" 会匹配上任何命令行里
    # 提到它的东西——监控用的 bash -c 循环、ssh 转发的命令行，都算。实测有个
    # 监控循环因此匹配到自己空转了 28 小时，并让这里长期误判成「正在生成」。
    out = subprocess.run(["pgrep", "-f", r"python.*run_generate\.py"],
                         capture_output=True, text=True).stdout.strip()
    if out:
        sys.exit("!! 检测到 run_generate.py 正在运行，探针中止（避免互相污染）")


# ---------------------------------------------------------------- 验收口径
_QP = (re.compile(r"‘([^‘’]{5,150}?)’"), re.compile(r"“([^“”]{5,150}?)”"),
       re.compile(r"「([^「」]{5,150}?)」"))
_ELL = re.compile(r"…+|\.{3,}|、|，|\|")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).lower()


def audit_quotes(week: str) -> tuple[int, int]:
    """引文溯源：描述里引号中的每一句必须能在挂载证据原文里找到。"""
    rows = db.q("""
      SELECT o.opp_id, o.desc_phenomenon,
             COALESCE(string_agg(COALESCE(e.snippet,'')||' '||COALESCE(m.content,'')||' '
                      ||COALESCE(m.content_zh,''),' '),'') corpus
        FROM voc_opportunity o
        LEFT JOIN voc_opp_evidence oe ON oe.opp_id=o.opp_id
        LEFT JOIN voc_evidence e ON e.message_id=oe.message_id AND e.seq=oe.seq
        LEFT JOIN voc_message m ON m.message_id=oe.message_id
       WHERE o.first_week=%s GROUP BY 1,2""", [week])
    total = bad = 0
    for r in rows:
        corpus = _norm(r["corpus"])
        for q in [q for p in _QP for q in p.findall(r["desc_phenomenon"] or "")]:
            total += 1
            if _norm(q) in corpus:
                continue
            frags = [f for f in _ELL.split(q) if len(f.strip()) >= 5]
            if frags and all(_norm(f) in corpus for f in frags):
                continue
            bad += 1
            if len(NOTE) < 5:
                NOTE.append(f"{r['opp_id']} 未溯源引文: {q[:50]}")
    return total, bad


def run_acceptance(week: str) -> None:
    n = db.q1("SELECT count(*) FROM voc_opportunity WHERE first_week=%s", [week]) or 0
    chk("产出了机会点", n > 0, f"{n} 条")
    if not n:
        return

    # 1. problem_mode —— 去重层的语义载体
    generic = db.q1("""SELECT count(*) FROM voc_opportunity WHERE first_week=%s
                        AND problem_mode = ANY(%s)""",
                    [week, list(validate._GENERIC_MODE)]) or 0
    chk("problem_mode 无分类名", generic == 0, f"分类名 {generic} 条")
    uniq = db.q1("SELECT count(DISTINCT problem_mode) FROM voc_opportunity "
                 "WHERE first_week=%s", [week]) or 0
    chk("problem_mode 基本互异（>=90%）", uniq >= n * 0.9, f"{uniq}/{n}")

    # 2. 向量 —— 没有向量则 L2/墓碑/跨线全废
    novec = db.q1("SELECT count(*) FROM voc_opportunity WHERE first_week=%s "
                  "AND mode_vec IS NULL", [week]) or 0
    chk("每条都有 mode_vec", novec == 0, f"缺失 {novec} 条")
    vuniq = db.q1("SELECT count(DISTINCT mode_vec::text) FROM voc_opportunity "
                  "WHERE first_week=%s", [week]) or 0
    chk("向量基本互异（>=90%）", vuniq >= n * 0.9, f"{vuniq}/{n}")

    # 3. opp_id 碰撞 —— 表现为证据数远超描述所述
    bad_id = db.q1("""SELECT count(*) FROM voc_opportunity o
                       WHERE o.first_week=%s AND o.evi_total > %s""",
                   [week, C.MAX_GROUP_SIZE * 2]) or 0
    chk(f"无异常膨胀条目（evi_total > {C.MAX_GROUP_SIZE*2}）", bad_id == 0, f"{bad_id} 条")

    # 4. 引文溯源
    tq, bq = audit_quotes(week)
    rate = 100 * bq / tq if tq else 0
    chk("引文未溯源率 <= 5%", rate <= 5, f"{bq}/{tq} = {rate:.1f}%")

    # 5. 兜底占位符不得外泄
    leak = db.q1("""SELECT count(*) FROM voc_opportunity WHERE first_week=%s
                     AND (title LIKE %s OR problem_mode LIKE %s OR core_tag LIKE %s)""",
                 [week, f"%{stage1.PLACEHOLDER_MODE}%", f"%{stage1.PLACEHOLDER_MODE}%",
                  f"%{stage1.PLACEHOLDER_MODE}%"]) or 0
    chk("兜底占位符未泄漏到语义字段", leak == 0, f"{leak} 条")

    # 6. 下钻链路 —— PM 点开要能看到原文
    orphan = db.q1("""SELECT count(*) FROM voc_opp_evidence oe
                       JOIN voc_opportunity o USING(opp_id)
                       LEFT JOIN voc_message m ON m.message_id=oe.message_id
                      WHERE o.first_week=%s AND m.message_id IS NULL""", [week]) or 0
    chk("挂载证据都能溯到原始消息", orphan == 0, f"断链 {orphan} 条")

    # 7. 跨来源汇聚 —— 关系表 match_by 保留兼容值 cross_line
    xline = db.q1("""SELECT count(*) FROM voc_opp_evidence oe JOIN voc_opportunity o USING(opp_id)
                      WHERE o.first_week=%s AND oe.match_by='cross_line'""", [week]) or 0
    chk("跨来源汇聚有挂载产生", xline > 0, f"{xline} 条其他来源证据")

    # 8. 放行 —— 不放行等于 PM 什么都看不到
    rel = db.q1("SELECT count(*) FROM voc_opportunity WHERE first_week=%s "
                "AND NOT backlog", [week]) or 0
    chk("放行策略已执行", rel > 0, f"放行 {rel}/{n}")

    # 9. 快照 —— 回滚与闭环验证的依据
    snap = db.q1("SELECT count(*) FROM voc_opp_snapshot WHERE week=%s", [week]) or 0
    chk("已写周度快照", snap > 0, f"{snap} 条")


# ---------------------------------------------------------------- 两条执行路径
def run_script_path(ctx) -> None:
    """脚本路径：直接调 pipeline 的函数，与 run_generate.py 同一套实现。"""
    routed = routing.route_evidence_by_lifecycle(db.generation_pool())

    old_buckets = [(bucket, items) for bucket, items in routed.buckets.items()
                   if bucket.opp_type == "老品迭代"]
    ranked = sorted(old_buckets, key=lambda kv: len(kv[1]))
    # 取一个【小】桶：目的是跑通全链路，不是压测
    small = [(k, v) for k, v in ranked if 6 <= len(v) <= 14][:1]
    if not small:
        small = ranked[-1:]
    if not small:
        raise RuntimeError("统一生成池中没有可用的老品迭代桶")
    old_bucket, items = small[0]
    for item in items:
        item.setdefault("_opp_type", old_bucket.opp_type)
    info = pipeline._bucket_context(old_bucket, items)
    print(f"[老品迭代] {info['category']}/{old_bucket.topic}  n={len(items)}")
    split = stage1.split_bucket(items, old_bucket.opp_type, info, ctx)
    groups = stage1.merge_similar_modes(split["groups"], ctx)
    built = [o for o in llm.parallel_map(
        lambda g: pipeline.build_opportunity(
            items, g, old_bucket.opp_type, info, ctx, []), groups)
        if isinstance(o, dict)]
    pipeline.persist_opportunities([(o, items) for o in built], SMOKE_WEEK, ctx, verbose=True)

    new_buckets = [(bucket, items) for bucket, items in routed.buckets.items()
                   if bucket.opp_type == "新品创新" and bucket.channel == "竞品对标"]
    new_buckets.sort(key=lambda kv: -len(kv[1]))
    if not new_buckets:
        raise RuntimeError("统一生成池中没有可用的新品创新·竞品对标桶")
    new_bucket, rows = new_buckets[0][0], new_buckets[0][1][:8]
    print(f"[新品创新] 竞品对标 n={len(rows)}")
    if rows:
        for row in rows:
            row.setdefault("_opp_type", new_bucket.opp_type)
        binfo = pipeline._bucket_context(new_bucket, rows)
        bsplit = stage1.split_bucket(rows, new_bucket.opp_type, binfo, ctx)
        bgroups = stage1.merge_similar_modes(bsplit["groups"], ctx)
        bgroups += [{"mode_name": (rows[u].get("evidence_text")
                                    or rows[u].get("content") or "")[:40],
                     "members": [u]}
                    for u in bsplit["unclassified"]]
        bbuilt = [o for o in llm.parallel_map(
            lambda g: pipeline.build_opportunity(
                rows, g, new_bucket.opp_type, binfo, ctx, []), bgroups)
            if isinstance(o, dict)]
        pipeline.persist_opportunities([(o, rows) for o in bbuilt], SMOKE_WEEK, ctx,
                                       verbose=True)

    # 收尾三件套：上一个探针就是死在没跑到这里
    print("[收尾] 跨来源汇聚 / 拆分 / 快照 / 放行")
    for r in db.q("SELECT opp_id, mode_vec::text v, opp_type, source_lines, src_line "
                  "FROM voc_opportunity "
                  "WHERE first_week=%s AND mode_vec IS NOT NULL", [SMOKE_WEEK]):
        vec = resolve._parse_vec(r["v"])
        if vec:
            attached = resolve.cross_source_merge(
                r["opp_id"], vec, r["opp_type"],
                r.get("source_lines") or [r["src_line"]], SMOKE_WEEK, ctx)
            if attached:
                pipeline.attach_evidence(r["opp_id"], attached)
    db.execute("""INSERT INTO voc_opp_snapshot(opp_id,week,evi_total,rank_score,title,
                    problem_mode,desc_phenomenon,desc_attribution,desc_suggestion)
                  SELECT opp_id,%s,evi_total,rank_score,title,problem_mode,desc_phenomenon,
                         desc_attribution,desc_suggestion
                    FROM voc_opportunity WHERE first_week=%s
                  ON CONFLICT (opp_id,week) DO NOTHING""", [SMOKE_WEEK, SMOKE_WEEK])
    print(f"  放行: {lifecycle.release_to_pm()}")


def run_dagster_path() -> None:
    """Dagster 路径：物化一个周分区。四个缺口都藏在这条路径里，必须单独覆盖。"""
    week = db.q1("SELECT max(week) FROM voc_run_log WHERE stage='ingest'") or "2026-W33"
    y, w = week.split("-W")
    from datetime import datetime
    key = datetime.fromisocalendar(int(y), int(w), 1).strftime("%Y-%m-%d")
    cmd = ["dagster", "asset", "materialize", "-m", "voc_analytics.definitions",
           "--select", "opportunities_by_lifecycle,cross_source_merged,"
                       "snapshots,release_to_pm", "--partition", key]
    print(f"[Dagster] {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    print(r.stdout[-2500:] or "(无 stdout)")
    if r.returncode:
        print(r.stderr[-1500:])
    chk("Dagster 物化成功", r.returncode == 0, f"exit={r.returncode}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dagster", action="store_true", help="改走 Dagster 资产路径")
    ap.add_argument("--keep", action="store_true", help="保留哨兵周产出以便人工查看")
    a = ap.parse_args()

    guard_no_concurrent_run()
    cleanup()                       # 先清残留，保证从干净切片开始
    t0 = time.time()
    ctx = C.RunCtx(run_id=f"smoke_{int(t0)}", week=SMOKE_WEEK)
    llm.reset_usage()

    print(f"=== 最小探针（{'Dagster' if a.dagster else '脚本'}路径）===\n")
    try:
        if a.dagster:
            run_dagster_path()
            week = db.q1("SELECT max(first_week) FROM voc_opportunity") or SMOKE_WEEK
        else:
            run_script_path(ctx)
            week = SMOKE_WEEK
        print("\n=== 验收 ===")
        run_acceptance(week)
    finally:
        if not a.keep and not a.dagster:
            print()
            cleanup()

    u = llm.usage()
    for s in NOTE:
        print(f"  · {s}")
    print(f"\n探针: PASS={P} FAIL={F}  |  LLM {u['calls']} 次 / {u['tokens']} tokens "
          f"/ {time.time()-t0:.0f}s")
    return 1 if F else 0


if __name__ == "__main__":
    raise SystemExit(main())

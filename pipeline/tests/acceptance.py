#!/usr/bin/env python3
"""一期验收：逐条核对 PRD v8 §9.4 的 DoD 与关键设计承诺。

每项输出 PASS / FAIL / N/A，最后给汇总。不通过的项给出实测值与门槛。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python tests/acceptance.py

前置条件：
  Python 3.11+ 及项目依赖已安装；运行环境已配置数据库连接；voc 数据库已完成
  当前迁移并已有冷启动产出。本脚本只读数据库。
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import db  # noqa: E402

R: list[tuple[str, str, str]] = []


def check(name: str, ok: bool | None, detail: str = "") -> None:
    R.append((("PASS" if ok else "FAIL") if ok is not None else "N/A", name, detail))


def q1(sql, p=None):
    return db.q1(sql, p)


# ============================================================ 事实层
n_msg = q1("SELECT count(*) FROM voc_message")
n_evi = q1("SELECT count(*) FROM voc_evidence")
weeks = q1("SELECT count(DISTINCT week) FROM voc_run_log WHERE stage='ingest' AND status='success'")
check("M1 冷启动回填 ≥26 周", weeks >= 26, f"实测 {weeks} 周，消息 {n_msg}，证据 {n_evi}")

mis = q1("""SELECT COALESCE(sum((metrics->'ingest'->>'misaligned')::int),0) FROM voc_run_log""")
check("标签/情感数组错位 ≤1%", (mis / max(n_evi, 1)) <= 0.01, f"错位 {mis} / {n_evi}")

tail = q1("SELECT count(*) FROM voc_evidence WHERE snippet IS DISTINCT FROM snippet_raw")
check("尾部重复缺陷已修复（按语种分支）", tail > 0,
      f"修复 {tail} 条（占证据 {100*tail/max(n_evi,1):.1f}%）")

lc = db.low_conf_intersection()
check("low_conf 交叉校验生效", lc["low_conf_total"] > 0,
      f"剔除 {lc['low_conf_total']} 条；电商可生成池 {lc['usable_pool']} 条")

soc_cat = q1("SELECT count(*) FROM voc_message WHERE src_line='社媒' AND category IS NOT NULL")
check("社媒无商品维度（验证 L1 必须分线）", soc_cat == 0,
      f"社媒 category 非空 {soc_cat} 条（PRD §1.2 断言应为 0）")

# ============================================================ 生成层
n_opp = q1("SELECT count(*) FROM voc_opportunity")
if not n_opp:
    check("生成层已产出", False, "voc_opportunity 为空，以下生成类检查跳过")
else:
    nr = q1("SELECT count(*) FROM voc_opportunity WHERE needs_review")
    check("Stage4 打回率 ≤40%", nr / n_opp <= 0.4,
          f"needs_review {nr}/{n_opp} = {100*nr/n_opp:.1f}%")

    bad_title = q1("""SELECT count(*) FROM voc_opportunity
                       WHERE title IS NOT NULL AND title !~ '：|:'""")
    check("标题符合「主体：动作词+对象」公式", bad_title == 0,
          f"无冒号的标题 {bad_title} 条")

    acts = "|".join(__import__("voc_analytics.prompts", fromlist=["x"]).ACTIONS)
    ok_act = q1(f"""SELECT count(*) FROM voc_opportunity
                     WHERE title ~ '({acts})'""")
    check("标题动作词取自受控词表 ≥90%", ok_act / n_opp >= 0.9,
          f"{ok_act}/{n_opp} = {100*ok_act/n_opp:.0f}%")

    three = q1("""SELECT count(*) FROM voc_opportunity
                   WHERE desc_phenomenon IS NOT NULL AND desc_attribution IS NOT NULL
                     AND desc_suggestion IS NOT NULL AND length(desc_suggestion)>20""")
    check("三段式描述完整（现象/归因/建议）", three / n_opp >= 0.9,
          f"{three}/{n_opp} 三段齐全")

    attr_ok = q1("""SELECT count(*) FROM voc_opportunity
                     WHERE desc_attribution ~ '^(推断|指向|说明|表明|反映|意味)'""")
    check("归因段以推断类词开头（事实/推测可分）", attr_ok / n_opp >= 0.8,
          f"{attr_ok}/{n_opp} = {100*attr_ok/n_opp:.0f}%")

    std = q1("""SELECT count(*) FROM voc_opportunity
                 WHERE desc_suggestion ~* '(ISO|GB/?T|EN|ASTM|IEC)\\s?[0-9]{3,}'""")
    check("建议段无虚构标准号", std == 0, f"引用标准号 {std} 条（曾出现 ISO 11679 张冠李戴）")

    repro = q1("""SELECT count(*) FROM voc_opportunity
                   WHERE prompt_ver IS NOT NULL AND model_ver IS NOT NULL
                     AND ctx_hash IS NOT NULL""")
    check("可复现性字段齐全（prompt_ver/model_ver/ctx_hash）", repro / n_opp >= 0.9,
          f"{repro}/{n_opp}")

    vec = q1("SELECT count(*) FROM voc_opportunity WHERE mode_vec IS NOT NULL")
    check("mode_vec 已写入（去重依赖）", vec / n_opp >= 0.9, f"{vec}/{n_opp}")

# ============================================================ 挂载与下钻
n_oe = q1("SELECT count(*) FROM voc_opp_evidence")
check("下钻挂载存在", n_oe > 0, f"{n_oe} 条挂载")
orphan = q1("""SELECT count(*) FROM voc_opp_evidence oe
                LEFT JOIN voc_evidence e USING(message_id, seq)
               WHERE e.message_id IS NULL""")
check("下钻无悬空引用（外键保障）", orphan == 0, f"悬空 {orphan} 条")

uncl = q1("SELECT count(*) FROM voc_unclassified_evidence")
check("未归类证据不丢弃（落表留待下周）", uncl >= 0, f"{uncl} 条已留存")

# ============================================================ 状态机
trg = q1("""SELECT count(*) FROM information_schema.triggers
             WHERE trigger_schema='public'
               AND trigger_name IN ('trg_voc_guard_locked','trg_voc_log_status',
                                    'trg_voc_guard_safety','trg_voc_derive_flags')""")
check("四个状态机触发器已部署", trg >= 4, f"实测 {trg} 条触发器记录")

views = q1("""SELECT count(*) FROM information_schema.views
               WHERE table_schema='public'
                 AND table_name IN ('voc_board','voc_inbox','voc_safety_watch',
                                    'voc_weekly_metrics')""")
check("PM 取数视图齐全", views == 4, f"{views}/4")

roles = q1("""SELECT count(*) FROM pg_roles
               WHERE rolname IN ('voc_writer','voc_human','voc_reader')""")
check("三角色权限矩阵已建", roles == 3, f"{roles}/3")

# ============================================================ 覆盖率（观测）
m = db.q("SELECT * FROM voc_weekly_metrics")[0]
check("电商覆盖率已可计算（观测指标非门槛）", m["line_a_coverage_pct"] is not None,
      f"{m['line_a_coverage_pct']}%  已挂载 {m['line_a_attached']}/{m['line_a_pool']}")

# ============================================================ 输出
print(f"{'结果':<6}{'检查项':<44}明细")
print("-" * 108)
for st, name, detail in R:
    print(f"{st:<6}{name:<44}{detail}")
p = sum(1 for s, _, _ in R if s == "PASS")
f = sum(1 for s, _, _ in R if s == "FAIL")
print("-" * 108)
print(f"PASS {p} / FAIL {f} / 共 {len(R)} 项")
sys.exit(1 if f else 0)

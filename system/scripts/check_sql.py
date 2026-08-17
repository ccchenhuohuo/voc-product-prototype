#!/usr/bin/env python3
"""SQL 真库规划检查命令。

用途：将 ``app/queries.py`` 中每条 SQL 提交给 PostgreSQL 做 ``EXPLAIN``，
      暴露 mock 单元测试无法发现的列歧义、类型不符和字段拼写错误。
用法：在 ``system/`` 目录执行 ``python scripts/check_sql.py``。
前置条件：已安装本项目依赖，已配置 ``VOC_PG_HOST``、``VOC_PG_PORT``、
          ``VOC_PG_DB``、``VOC_PG_HUMAN_USER`` 和 ``VOC_PG_HUMAN_PASSWORD``，
          且 PostgreSQL 可连通、当前账号具有规划所有查询的权限。

``EXPLAIN`` 不带 ``ANALYZE``，只生成执行计划，不执行被检查的语句。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import queries as Q          # noqa: E402
from app.db import db                 # noqa: E402

T, D = "__probe__", None

# 每条查询的占位符实参。类型必须对：布尔位用在 (%s OR ...) 里，给字符串会报错。
PARAMS = {
    "SHELL_COUNTS": (),
    "HOME_EVIDENCE_FUNNEL": (), "HOME_EVIDENCE_PER_OPP": (),
    "HOME_SIMILARITY": (), "HOME_ISSUE_STATUS": (),
    "HOME_COVERAGE": (), "HOME_FRESHNESS": (),
    "BOARD_SPUS": ("", ""), "BOARD_SPUS_REVIVED": ("", ""),
    "BOARD_ISSUES": (), "BOARD_INNOVATIONS": (),
    "SEARCH_SPUS": ("", "%%", "%%", "%%", "", "", "", ""),
    "SEARCH_SPUS_BY_DOMAIN": (
        "", "%%", "%%", "%%", "", "", T, "", ""
    ),
    "SEARCH_SPUS_BY_SUB": (
        "", "%%", "%%", "%%", "", "", T, T, "", ""
    ),
    "SEARCH_SPUS_BY_LEAF": (
        "", "%%", "%%", "%%", "", "", T, T, T, "", ""
    ),
    "SEARCH_FACETS": (),
    "SEARCH_TAG_FACETS": ("", "%%", "%%", "%%", "", ""),
    "SPU_DETAIL": (T,), "SPU_ISSUES": (T, T),
    "ISSUE_DETAIL": (T, T), "ISSUE_VOICES": (T, T),
    "INNOVATION_DETAIL": (T,), "INNOVATION_EVIDENCE": (T,),
    "STRATEGY_OPPORTUNITIES": (),
    "UPDATE_ISSUE_STATUS": (T, T, "考虑中", "", "", D, T),
    "UPDATE_INNOVATION_STATUS": (T, "考虑中", "", "", D, T),
    "REVIVE_DISMISSED": (), "REVIVE_COMPLETED": (),
    "APPLY_REVIVAL": (T, T, T, T),
}


def main() -> int:
    names = [n for n in dir(Q) if n.isupper() and isinstance(getattr(Q, n), str)]
    unmapped = [n for n in names if n not in PARAMS]
    stale = [n for n in PARAMS if n not in names]
    bad = 0
    for name in sorted(names):
        sql = getattr(Q, name)
        params = PARAMS.get(name)
        if params is None:
            print(f"  跳过  {name}  （未登记实参）")
            continue
        try:
            with db.conn() as c:
                if params:
                    c.execute("EXPLAIN " + sql, params)
                else:
                    c.execute("EXPLAIN " + sql)
            print(f"  OK    {name}")
        except Exception as exc:                     # noqa: BLE001
            bad += 1
            print(f"  失败  {name}\n        {type(exc).__name__}: {exc}")
    if unmapped:
        print(f"\n!! 有 {len(unmapped)} 条查询没登记实参，等于没验：{', '.join(unmapped)}")
    if stale:
        print(f"\n!! PARAMS 含 {len(stale)} 个已不存在的查询：{', '.join(stale)}")
    print(f"\nSQL 体检：通过 {len(names)-bad-len(unmapped)} / 失败 {bad} / 未验 {len(unmapped)}")
    return 1 if bad or unmapped or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())

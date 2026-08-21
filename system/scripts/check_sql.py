#!/usr/bin/env python3
"""SQL 真库规划检查命令。

用途：将 ``app/queries.py`` 中每条 SQL 提交给 PostgreSQL 做 ``EXPLAIN``，
      暴露 mock 单元测试无法发现的列歧义、类型不符和字段拼写错误。
用法：在 ``system/`` 目录执行 ``python scripts/check_sql.py``。
      禁止连库的开发环境可执行 ``python scripts/check_sql.py --static``，仅核对
      公共查询登记与占位符数量；它不替代部署验收方的 PostgreSQL EXPLAIN。
前置条件：已安装本项目依赖，已配置 ``VOC_PG_HOST``、``VOC_PG_PORT``、
          ``VOC_PG_DB``、``VOC_PG_HUMAN_USER`` 和 ``VOC_PG_HUMAN_PASSWORD``，
          且 PostgreSQL 可连通、当前账号具有规划所有查询的权限。

``EXPLAIN`` 不带 ``ANALYZE``，只生成执行计划，不执行被检查的语句。
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import queries as Q          # noqa: E402
from app.db import db                 # noqa: E402

T, D = "__probe__", None
V = "[" + ",".join(["0"] * 1024) + "]"

# 每条查询的占位符实参。类型必须对：布尔位用在 (%s OR ...) 里，给字符串会报错。
PARAMS = {
    "SHELL_COUNTS": (),
    "HOME2_SOURCES": (), "HOME2_FLOW_SOCIAL": (), "HOME2_FLOW_EC": (),
    "HOME2_STATUS": (), "HOME2_FRESHNESS": (), "HOME2_WEEKLY": (),
    "HOME2_DIST": ("社媒", "语种"),
    "BOARD_SPUS": ("tracked", "", "%%", "%%", "%%", "", "", ""),
    "BOARD_ISSUES": (), "BOARD_INNOVATIONS": (),
    "SPU_DETAIL": (T,), "SPU_RAW_VOICES": (T,), "SPU_ISSUES": (T, T),
    "ISSUE_DETAIL": (T, T), "ISSUE_VOICES": (T, T),
    "INNOVATION_DETAIL": (T,), "INNOVATION_EVIDENCE": (T,),
    "STRATEGY_AXES": ("", "", "OPP2-", "通病"),
    "STRATEGY_AXIS": ("OPP2-", T),
    "STRATEGY_AXIS_MEMBERS": ("OPP2-", T),
    "UPDATE_ISSUE_STATUS": (T, T, "考虑中", "", "", D, T),
    "UPDATE_INNOVATION_STATUS": (T, "考虑中", "", "", D, T),
    "REVIVE_DISMISSED": (), "REVIVE_COMPLETED": (),
    "APPLY_REVIVAL": (T, T, T, T),
}

# MCP queries use the same four-part gate as the Web queries: named constant,
# parameter fixture, static placeholder validation, and deployment-time EXPLAIN.
PARAMS.update({
    "MCP_SCHEMA_OBJECTS": (),
    "MCP_SCHEMA_CHANNEL": (),
    "MCP_DATA_AS_OF": (),
    "MCP_SOURCE_VALUES": (),
    "MCP_STATUS_CONSTRAINTS": (),
    "MCP_KNN_ITERATIVE_CONFIG": (),
    "MCP_FIND_SEMANTIC_OPPORTUNITIES": (
        V, 40, True, [T], "", "", "", "", "", "", "", "", 10, True,
    ),
    "MCP_FIND_SEMANTIC_STATS": (
        True, [T], "", "", "", "", "", "", "", "",
    ),
    "MCP_FIND_KEYWORD_OPPORTUNITIES": (
        T, f"%{T}%", "", "", "", "", "", "", "", "", 10,
    ),
    "MCP_FIND_KEYWORD_SPUS": (
        T, f"%{T}%", "", "", "", "", "", "", "", "", 10,
    ),
    "MCP_FIND_KEYWORD_VOICES": (
        T, f"%{T}%", "", "", "", "", "", "", "", "", 10,
    ),
    "MCP_FIND_KEYWORD_VOICE_STATS": (
        f"%{T}%", "", "", "", "", "", "", "", "",
    ),
    "MCP_FIND_EXPAND_SPUS": ([T], "", "", "", "", 10),
    "MCP_FIND_EXPAND_VOICES": ([T], "", "", "", "", 10),
    "MCP_FIND_EXPAND_VOICE_STATS": ([T], "", "", "", ""),
    "MCP_OPPORTUNITY_CORE": (T,),
    "MCP_OPPORTUNITY_SOURCE_DETAIL": (T,),
    "MCP_OPPORTUNITY_STATUSES": (T, T),
    "MCP_SPU_ISSUE_MSG_COUNTS": (T,),
    "MCP_LIST_SPUS_DEGRADED": (),
    "MCP_VOICES_OPPORTUNITY": (T, 0, 20),
    "MCP_VOICES_OPPORTUNITY_COUNTS": (T,),
    "MCP_VOICE_THREAD_FIELDS": ([T],),
    "MCP_VOICE_MSG_SENTIMENTS": ([T],),
    "MCP_VOICES_UNCLAIMED": (T, 0, 20),
    "MCP_VOICES_UNCLAIMED_INHERITED": (T, 0, 20),
    "MCP_VOICES_UNCLAIMED_COUNTS": (T,),
    "MCP_VOICES_UNCLAIMED_COUNTS_INHERITED": (T,),
    "MCP_BUNDLE_SPU_VOICES_BASE": (T, 20),
    "MCP_BUNDLE_SPU_VOICES_INHERITED": (T, 20),
    "MCP_BUNDLE_SPU_VOICE_COUNTS_BASE": (T,),
    "MCP_BUNDLE_SPU_VOICE_COUNTS_INHERITED": (T,),
    "MCP_TAXONOMY": ("%",),
})


def main(*, static_only: bool = False) -> int:
    names = [n for n in dir(Q) if n.isupper() and isinstance(getattr(Q, n), str)]
    unmapped = [n for n in names if n not in PARAMS]
    stale = [n for n in PARAMS if n not in names]
    mismatched = [
        name for name in names
        if name in PARAMS and getattr(Q, name).count("%s") != len(PARAMS[name])
    ]
    if static_only:
        if unmapped:
            print(f"!! 有 {len(unmapped)} 条查询没登记实参：{', '.join(unmapped)}")
        if stale:
            print(f"!! PARAMS 含 {len(stale)} 个已不存在的查询：{', '.join(stale)}")
        if mismatched:
            print(f"!! 占位符数量不匹配：{', '.join(mismatched)}")
        checked = len(names) - len(unmapped) - len(mismatched)
        print(f"SQL 静态体检：通过 {checked} / 失败 {len(mismatched)} / 未验 {len(unmapped)}")
        return 1 if unmapped or stale or mismatched else 0

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
    parser = argparse.ArgumentParser(description="检查 app/queries.py 的公共 SQL")
    parser.add_argument(
        "--static", action="store_true",
        help="只核对查询登记与占位符，不连接数据库或提交 EXPLAIN",
    )
    args = parser.parse_args()
    raise SystemExit(main(static_only=args.static))

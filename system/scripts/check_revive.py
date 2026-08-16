#!/usr/bin/env python3
"""SPU 问题条目复活检测命令。

用途：检测符合条件的已关闭问题，并在非预演模式下写入复活状态。
用法：在 ``system/`` 目录执行 ``python scripts/check_revive.py --dry-run``；
      确认输出后去掉 ``--dry-run`` 正式执行，可用 ``--actor NAME`` 指定执行者。
前置条件：已安装本项目依赖，已配置 ``VOC_PG_HOST``、``VOC_PG_PORT``、
          ``VOC_PG_DB``、``VOC_PG_HUMAN_USER`` 和 ``VOC_PG_HUMAN_PASSWORD``，
          且 PostgreSQL 可连通；正式执行还需对人工表的写权限。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

SYSTEM_DIR = Path(__file__).resolve().parents[1]
if str(SYSTEM_DIR) not in sys.path:
    sys.path.insert(0, str(SYSTEM_DIR))

from app import queries as Q  # noqa: E402
from app.db import db  # noqa: E402


@dataclass(frozen=True)
class Revival:
    spu: str
    opp_id: str
    trigger: str
    reason: str


def detect_revivals(
    dismissed: Iterable[Mapping[str, Any]],
    completed: Iterable[Mapping[str, Any]],
) -> list[Revival]:
    """纯函数判定，供命令行与单元测试共用。"""
    results: list[Revival] = []
    seen: set[tuple[str, str]] = set()
    for row in dismissed:
        baseline = int(row.get("baseline_evi_count") or 0)
        current = int(row.get("current_evi_count") or 0)
        key = (str(row["spu"]), str(row["opp_id"]))
        if baseline > 0 and current >= 3 * baseline:
            results.append(Revival(*key, "evidence_tripled", f"证据从 {baseline} 条增长至 {current} 条，已达到不考虑时基准的 3 倍"))
            seen.add(key)
    for row in completed:
        key = (str(row["spu"]), str(row["opp_id"]))
        count = int(row.get("new_feedback_count") or 0)
        if key in seen or not row.get("release_date") or count <= 0:
            continue
        release = row.get("target_release") or "该版本"
        results.append(Revival(*key, "post_release_feedback", f"{release} 上市后仍有 {count} 条新反馈"))
    return results


def run(dry_run: bool = False, actor: str = "voc-revive-check") -> list[Revival]:
    dismissed = db.query(Q.REVIVE_DISMISSED)
    completed = db.query(Q.REVIVE_COMPLETED)
    revivals = detect_revivals(dismissed, completed)
    for item in revivals:
        prefix = "[dry-run]" if dry_run else "[revived]"
        print(f"{prefix} {item.spu} / {item.opp_id}: {item.reason}")
        if not dry_run:
            db.execute(Q.APPLY_REVIVAL, (item.reason, actor, item.spu, item.opp_id))
    if not revivals:
        print("没有符合条件的复活条目")
    return revivals


def main() -> int:
    parser = argparse.ArgumentParser(description="检测 VOC SPU 问题条目的两类复活条件")
    parser.add_argument("--dry-run", action="store_true", help="只打印将复活的条目，不写数据库")
    parser.add_argument("--actor", default="voc-revive-check", help="写入 updated_by 的执行者标识")
    args = parser.parse_args()
    try:
        run(dry_run=args.dry_run, actor=args.actor)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

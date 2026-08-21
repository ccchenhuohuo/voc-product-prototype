#!/usr/bin/env python3
"""v3 归属快照数据库验收；会写入并清理专用夹具，交由验收方执行。

覆盖：
  1. ``voc_has_spu`` 的仅事实 / 仅继承 / 两者都有 / 两者皆空；
  2. 两个独立进程并发准备同一 run_id，只得到一份相同快照；
  3. 同一 run_id 再调用幂等返回既有行数；
  4. 不同 run_id 的快照与指纹日志相互独立。

本文件不是 pytest 自动收集项。施工交付阶段不得运行；只由验收方在已应用
030--033 的隔离数据库中显式执行。
"""
from __future__ import annotations

import concurrent.futures
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voc_analytics import db  # noqa: E402


def _prepare(run_id: str) -> int:
    return db.prepare_assign_snapshot(run_id)


def _insert_fixtures(prefix: str) -> dict[str, str]:
    ids = {
        "fact": f"{prefix}-fact",
        "root": f"{prefix}-root",
        "both": f"{prefix}-both",
        "empty": f"{prefix}-empty",
    }
    rows = [
        (ids["fact"], [f"{prefix}-SPU-A"], None),
        (ids["root"], None, [f"{prefix}-SPU-B"]),
        (ids["both"], [f"{prefix}-SPU-C"], [f"{prefix}-SPU-D"]),
        (ids["empty"], None, None),
    ]
    with db.conn() as connection:
        cursor = connection.cursor()
        cursor.executemany(
            """INSERT INTO voc_message
                      (message_id, src_line, pull_batch_id, content,
                       spu, spu_inherited)
               VALUES (%s, '电商', %s, 'v3 snapshot fixture', %s, %s)""",
            [(message_id, prefix, fact, root)
             for message_id, fact, root in rows],
        )
        # 两条事实 SPU 行保证快照非空；继承-only 电商不属于冻结 G5 池。
        cursor.executemany(
            """INSERT INTO voc_evidence
                      (message_id, seq, sentiment, snippet, is_product)
               VALUES (%s, 0, '负面', 'v3 snapshot fixture', true)""",
            [(ids["fact"],), (ids["both"],)],
        )
    return ids


def _assert_has_spu(ids: dict[str, str]) -> None:
    assert db.q1(
        "SELECT has_table_privilege(current_user, "
        "'public.voc_assign_snapshot', 'INSERT')"
    ) is False, "运行角色不得绕过单写者函数直接 INSERT 快照"
    rows = db.q(
        """SELECT message_id, voc_has_spu(message_id) AS actual
             FROM voc_message
            WHERE message_id = ANY(%s)
            ORDER BY message_id""",
        [sorted(ids.values())],
    )
    actual = {row["message_id"]: row["actual"] for row in rows}
    expected = {
        ids["fact"]: True,
        ids["root"]: True,
        ids["both"]: True,
        ids["empty"]: False,
    }
    assert actual == expected, (actual, expected)


def _assert_run_isolation(run_a: str, run_b: str, fixture_ids: set[str]) -> None:
    rows = db.q(
        """SELECT run_id, message_id, seq, assigned_spu, source
             FROM voc_assign_snapshot
            WHERE run_id = ANY(%s) AND message_id = ANY(%s)
            ORDER BY run_id, message_id, seq, assigned_spu""",
        [[run_a, run_b], sorted(fixture_ids)],
    )
    by_run = {
        run_id: {
            (row["message_id"], row["seq"], row["assigned_spu"], row["source"])
            for row in rows if row["run_id"] == run_id
        }
        for run_id in (run_a, run_b)
    }
    assert by_run[run_a]
    assert by_run[run_a] == by_run[run_b]
    assert db.verify_assign_snapshot(run_a) == db.verify_assign_snapshot(run_b)


def _cleanup(prefix: str, run_ids: list[str]) -> None:
    # 单条语句对每个 run_id 都是整轮删除，满足 031 的删除保护。
    db.execute("DELETE FROM voc_assign_snapshot WHERE run_id = ANY(%s)", [run_ids])
    db.execute(
        "DELETE FROM voc_run_log WHERE run_id = ANY(%s)",
        [[f"{run_id}:assign_snapshot" for run_id in run_ids]],
    )
    db.execute("DELETE FROM voc_message WHERE pull_batch_id = %s", [prefix])


def main() -> int:
    token = uuid.uuid4().hex
    prefix = f"V3SNAP-{token}"
    run_a = f"{prefix}-A"
    run_b = f"{prefix}-B"
    run_ids = [run_a, run_b]
    try:
        fixture = _insert_fixtures(prefix)
        _assert_has_spu(fixture)

        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_prepare, [run_a, run_a]))
        assert results[0] == results[1] > 0, results
        assert db.prepare_assign_snapshot(run_a) == results[0]
        assert db.verify_assign_snapshot(run_a) == results[0]

        second_count = db.prepare_assign_snapshot(run_b)
        assert second_count == results[0]
        _assert_run_isolation(run_a, run_b, set(fixture.values()))
        print("PASS: v3 snapshot concurrency/idempotence/isolation + has_spu four states")
        return 0
    finally:
        _cleanup(prefix, run_ids)


if __name__ == "__main__":
    raise SystemExit(main())

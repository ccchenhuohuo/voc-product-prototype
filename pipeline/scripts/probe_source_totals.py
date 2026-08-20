#!/usr/bin/env python3
"""来源总量探针：缓存云听同窗口可用量，供首页只读展示（不调 LLM）。

对 COMMENT / SOCIAL / SERVICE 各创建一次 ``total=1`` 的导出任务，不下载
xlsx，只读取任务元数据里的 ``matched_count``，按来源覆盖写入
``voc_source_probe``。默认以库内最新发布时间所在周为末周，向前覆盖 26 个
ISO 周，并以执行时点为窗口终点；也可显式传入窗口。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python scripts/probe_source_totals.py
  .venv/bin/python scripts/probe_source_totals.py \
    --start '2026-02-16 00:00:00+08:00' --end '2026-08-18 00:00:00+08:00'

前置条件：
  Python 3.11+ 及项目依赖已安装；.env 中已配置云听导出与 PostgreSQL 写入
  所需凭据且已导出到进程环境；已执行 022_source_probe_cache.sql；运行主机
  可访问云听 API 与数据库。脚本会写探针缓存，但不下载业务数据、不调用 LLM。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import config as C, db, yunting  # noqa: E402


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
SOURCE_PROBES = (
    ("COMMENT", C.COMMENT_FILTER),
    ("SOCIAL", C.SOCIAL_FILTER),
    ("SERVICE", None),
)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=LOCAL_TZ) if parsed.tzinfo is None else parsed


def _window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    end = _parse_time(args.end) if args.end else datetime.now(LOCAL_TZ)
    if args.start:
        start = _parse_time(args.start)
    elif args.end:
        start = end - timedelta(weeks=26)
    else:
        rows = db.q("""
          SELECT date_trunc('week', max(publish_time)) - interval '25 weeks'
                   AS window_start
            FROM voc_message
           WHERE publish_time IS NOT NULL
        """)
        aligned = rows[0].get("window_start") if rows else None
        start = aligned or end - timedelta(weeks=26)
        if start.tzinfo is None:
            start = start.replace(tzinfo=LOCAL_TZ)
    if start >= end:
        raise ValueError("--start 必须早于 --end")
    return start, end


def _cloud_time(value: datetime) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def probe_source(qtype: str, start: datetime, end: datetime,
                 topic_filter: dict | None) -> dict:
    """创建一个最小导出任务，只保留 export_slice 同口径的 matched 元数据。"""
    task_id = yunting._create_task(  # noqa: SLF001 - 同包运维脚本复用生产协议
        qtype, _cloud_time(start), _cloud_time(end), 1, topic_filter)
    result = yunting._await_task(task_id)  # noqa: SLF001
    meta = {
        "start": start,
        "end": end,
        "matched": int(result.get("matched_count", 0)),
    }
    return {
        "query_task_type": qtype,
        "matched_count": meta["matched"],
        "window_start": meta["start"],
        "window_end": meta["end"],
        "probed_at": datetime.now(LOCAL_TZ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="ISO 时间；缺省为结束时间前 26 周")
    parser.add_argument("--end", help="ISO 时间；缺省为当前时间")
    args = parser.parse_args()
    start, end = _window(args)

    yunting._handshake()  # noqa: SLF001 - 三个任务复用一次 MCP 会话初始化
    rows = [
        probe_source(qtype, start, end, topic_filter)
        for qtype, topic_filter in SOURCE_PROBES
    ]
    db.upsert(
        "voc_source_probe", rows, ["query_task_type"],
        ["matched_count", "window_start", "window_end", "probed_at"],
    )
    for row in rows:
        print(f"{row['query_task_type']}: matched={row['matched_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

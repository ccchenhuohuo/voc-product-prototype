#!/usr/bin/env python3
"""按周分区跑 M1 事实层抽取。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python scripts/run_ingest.py 2026-W33
  .venv/bin/python scripts/run_ingest.py 2026-W20 2026-W33 --backfill

前置条件：
  从 /home/sdy/voc-analytics 运行；Python 3.11+ 及项目依赖已安装；.env 中的数据库与
  云听导出配置已按上例导出；数据库已完成当前迁移且运行主机可访问云听 API。
  本脚本会写事实层；可加 -v 在失败时输出 traceback。
"""
from __future__ import annotations
import sys, time, traceback
from datetime import datetime

sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import config as C, db, ingest  # noqa: E402


def weeks_between(a: str, b: str) -> list[str]:
    ya, wa = map(int, a.split("-W")); yb, wb = map(int, b.split("-W"))
    out, y, w = [], ya, wa
    while (y, w) <= (yb, wb):
        out.append(f"{y}-W{w:02d}")
        d = datetime.fromisocalendar(y, w, 1)
        nxt = datetime.fromordinal(d.toordinal() + 7).isocalendar()
        y, w = nxt[0], nxt[1]
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    backfill = "--backfill" in sys.argv
    if not args:
        print(__doc__); return 2
    weeks = weeks_between(args[0], args[1]) if len(args) > 1 else [args[0]]
    mode = "backfill" if backfill else "incremental"
    print(f"模式={mode}  周数={len(weeks)}  {weeks[0]} .. {weeks[-1]}\n")

    ok = failed = 0
    for wk in weeks:
        ctx = C.RunCtx(run_id=f"ingest_{wk}_{int(time.time())}", week=wk, mode=mode)
        s, e = ingest.week_bounds(wk)
        t0 = time.time()
        try:
            st = ingest.ingest_window(ctx, s, e)
            ingest.snapshot_taxonomy(wk)
            db.save_run_log(ctx, "ingest", window_start=s, window_end=e,
                            matched_rows=st.get("电商_messages", 0) + st.get("社媒_messages", 0),
                            exported_rows=st["messages_written"],
                            status="success", started_at=datetime.fromtimestamp(t0),
                            finished_at=datetime.now())
            print(f"  {wk}  电商 {st.get('电商_messages',0):>4}  社媒 {st.get('社媒_messages',0):>4}"
                  f"  证据 {st['evidence_written']:>5}  尾部修复 {st['tail_fixed']:>4}"
                  f"  错位 {st['misaligned']}  {st['elapsed_s']}s")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  {wk}  FAILED: {type(exc).__name__}: {exc}")
            if "-v" in sys.argv:
                traceback.print_exc()
            db.save_run_log(ctx, "ingest", window_start=s, window_end=e,
                            status="failed", error_message=str(exc)[:2000],
                            started_at=datetime.fromtimestamp(t0), finished_at=datetime.now())
    print(f"\n完成：成功 {ok} / 失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

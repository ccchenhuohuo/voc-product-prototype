"""SPU 展开层：手工收尾调用数据库业务规则并回报统计。"""
from __future__ import annotations

from . import db


def refresh(week: str) -> dict:
    """刷新全量 SPU 派生层；week 仅用于调度元数据，不参与全量 SQL 口径。"""
    db.execute("SELECT voc_refresh_spu_layer()")
    # 036 已把生产切到纯 v3，并删除 voc_spu_issue_v2。保留这个统计键供
    # 日志/指标消费者兼容，但不能让已退役的 v2 关系阻断 v3 收尾。
    v2_issue_count = 0
    if db.q1("SELECT to_regclass('public.voc_spu_issue_v2')"):
        v2_issue_count = int(
            db.q1("SELECT count(*) FROM voc_spu_issue_v2") or 0)
    stat = db.q("""
      SELECT (SELECT count(*) FROM voc_spu) AS spu_count,
             (SELECT count(*) FROM voc_spu_issue) AS issue_count,
             (SELECT count(*) FROM voc_spu_issue_v3) AS v3_issue_count,
             (SELECT count(*) FROM voc_opportunity
               WHERE scope_source = 'v3-未计算') AS scope_pending_count
    """)[0]
    return {"week": week, **stat, "v2_issue_count": v2_issue_count}

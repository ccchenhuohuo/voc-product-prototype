"""SPU 展开层：手工收尾调用数据库业务规则并回报统计。"""
from __future__ import annotations

from . import db


def refresh(week: str) -> dict:
    """刷新全量 SPU 派生层；week 仅用于调度元数据，不参与全量 SQL 口径。"""
    db.execute("SELECT voc_refresh_spu_layer()")
    stat = db.q("""
      SELECT (SELECT count(*) FROM voc_spu) AS spu_count,
             (SELECT count(*) FROM voc_spu_issue) AS issue_count,
             (SELECT count(*) FROM voc_opportunity WHERE n_eff IS NOT NULL)
               AS n_eff_count
    """)[0]
    return {"week": week, **stat}

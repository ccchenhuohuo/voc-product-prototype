#!/usr/bin/env python3
"""只读盘点旧 OPP-* 引用在 ``voc_opp_id_map`` 中的缺失映射。

本脚本不归档、不截断、不改写任何表。若发现已有 PM 裁决的旧提案，返回非零
并明确停工；具体处置由验收方决定。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voc_analytics import db  # noqa: E402


AUDITS: tuple[tuple[str, str], ...] = (
    (
        "voc_opp_snapshot.opp_id",
        """
        SELECT count(*)::bigint AS reference_rows,
               count(DISTINCT s.opp_id)::bigint AS distinct_legacy_ids,
               count(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = s.opp_id
               ))::bigint AS unmapped_rows,
               count(DISTINCT s.opp_id) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = s.opp_id
               ))::bigint AS unmapped_legacy_ids
          FROM voc_opp_snapshot s
         WHERE s.opp_id LIKE 'OPP-%%'
        """,
    ),
    (
        "voc_opp_nn.opp_id",
        """
        SELECT count(*)::bigint AS reference_rows,
               count(DISTINCT n.opp_id)::bigint AS distinct_legacy_ids,
               count(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = n.opp_id
               ))::bigint AS unmapped_rows,
               count(DISTINCT n.opp_id) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = n.opp_id
               ))::bigint AS unmapped_legacy_ids
          FROM voc_opp_nn n
         WHERE n.opp_id LIKE 'OPP-%%'
        """,
    ),
    (
        "voc_opp_nn.neighbor_id",
        """
        SELECT count(*)::bigint AS reference_rows,
               count(DISTINCT n.neighbor_id)::bigint AS distinct_legacy_ids,
               count(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = n.neighbor_id
               ))::bigint AS unmapped_rows,
               count(DISTINCT n.neighbor_id) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = n.neighbor_id
               ))::bigint AS unmapped_legacy_ids
          FROM voc_opp_nn n
         WHERE n.neighbor_id LIKE 'OPP-%%'
        """,
    ),
    (
        "voc_proposal.opp_ids[]",
        """
        WITH refs AS (
          SELECT p.proposal_id, x.opp_id
            FROM voc_proposal p
           CROSS JOIN LATERAL unnest(p.opp_ids) x(opp_id)
           WHERE x.opp_id LIKE 'OPP-%%'
        )
        SELECT count(*)::bigint AS reference_rows,
               count(DISTINCT opp_id)::bigint AS distinct_legacy_ids,
               count(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = refs.opp_id
               ))::bigint AS unmapped_rows,
               count(DISTINCT opp_id) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = refs.opp_id
               ))::bigint AS unmapped_legacy_ids
          FROM refs
        """,
    ),
    (
        "voc_opp_lineage.parent_ids[]",
        """
        WITH refs AS (
          SELECT l.lineage_id, x.opp_id
            FROM voc_opp_lineage l
           CROSS JOIN LATERAL unnest(COALESCE(l.parent_ids, ARRAY[]::text[])) x(opp_id)
           WHERE x.opp_id LIKE 'OPP-%%'
        )
        SELECT count(*)::bigint AS reference_rows,
               count(DISTINCT opp_id)::bigint AS distinct_legacy_ids,
               count(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = refs.opp_id
               ))::bigint AS unmapped_rows,
               count(DISTINCT opp_id) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = refs.opp_id
               ))::bigint AS unmapped_legacy_ids
          FROM refs
        """,
    ),
    (
        "voc_opp_lineage.child_ids[]",
        """
        WITH refs AS (
          SELECT l.lineage_id, x.opp_id
            FROM voc_opp_lineage l
           CROSS JOIN LATERAL unnest(COALESCE(l.child_ids, ARRAY[]::text[])) x(opp_id)
           WHERE x.opp_id LIKE 'OPP-%%'
        )
        SELECT count(*)::bigint AS reference_rows,
               count(DISTINCT opp_id)::bigint AS distinct_legacy_ids,
               count(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = refs.opp_id
               ))::bigint AS unmapped_rows,
               count(DISTINCT opp_id) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = refs.opp_id
               ))::bigint AS unmapped_legacy_ids
          FROM refs
        """,
    ),
    (
        "voc_opportunity_manual.opp_id",
        """
        SELECT count(*)::bigint AS reference_rows,
               count(DISTINCT x.opp_id)::bigint AS distinct_legacy_ids,
               count(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = x.opp_id
               ))::bigint AS unmapped_rows,
               count(DISTINCT x.opp_id) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = x.opp_id
               ))::bigint AS unmapped_legacy_ids
          FROM voc_opportunity_manual x
         WHERE x.opp_id LIKE 'OPP-%%'
        """,
    ),
    (
        "voc_spu_issue_manual.(spu,opp_id)",
        """
        SELECT count(*)::bigint AS reference_rows,
               count(DISTINCT x.opp_id)::bigint AS distinct_legacy_ids,
               count(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = x.opp_id AND m.legacy_spu = x.spu
               ))::bigint AS unmapped_rows,
               count(DISTINCT x.opp_id) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = x.opp_id AND m.legacy_spu = x.spu
               ))::bigint AS unmapped_legacy_ids
          FROM voc_spu_issue_manual x
         WHERE x.opp_id LIKE 'OPP-%%'
        """,
    ),
    (
        "voc_status_log.opp_id",
        """
        SELECT count(*)::bigint AS reference_rows,
               count(DISTINCT x.opp_id)::bigint AS distinct_legacy_ids,
               count(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = x.opp_id
               ))::bigint AS unmapped_rows,
               count(DISTINCT x.opp_id) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = x.opp_id
               ))::bigint AS unmapped_legacy_ids
          FROM voc_status_log x
         WHERE x.opp_id LIKE 'OPP-%%'
        """,
    ),
    (
        "voc_spu_issue_log.(spu,opp_id)",
        """
        SELECT count(*)::bigint AS reference_rows,
               count(DISTINCT x.opp_id)::bigint AS distinct_legacy_ids,
               count(*) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = x.opp_id AND m.legacy_spu = x.spu
               ))::bigint AS unmapped_rows,
               count(DISTINCT x.opp_id) FILTER (WHERE NOT EXISTS (
                 SELECT 1 FROM voc_opp_id_map m
                  WHERE m.legacy_opp_id = x.opp_id AND m.legacy_spu = x.spu
               ))::bigint AS unmapped_legacy_ids
          FROM voc_spu_issue_log x
         WHERE x.opp_id LIKE 'OPP-%%'
        """,
    ),
)


DECIDED_PROPOSALS = """
SELECT count(DISTINCT p.proposal_id)::bigint
  FROM voc_proposal p
 WHERE p.decided_by IS NOT NULL
   AND EXISTS (
     SELECT 1 FROM unnest(p.opp_ids) x(opp_id)
      WHERE x.opp_id LIKE 'OPP-%%'
   )
"""


def main() -> int:
    report: dict[str, dict[str, int]] = {}
    for label, sql in AUDITS:
        row = db.q(sql)[0]
        report[label] = {key: int(value or 0) for key, value in row.items()}

    decided = int(db.q1(DECIDED_PROPOSALS) or 0)
    output = {
        "legacy_reference_audit": report,
        "decided_legacy_proposals": decided,
        "action": "STOP_AND_ESCALATE" if decided else "REPORT_ONLY",
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    if decided:
        print("发现 decided_by 非空的旧提案：停工并交验收方裁决。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

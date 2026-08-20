-- 024：删除 channel 概念。
-- 必须与全量重跑同批执行，且需先部署配套应用代码。
BEGIN;

-- voc_inbox / voc_safety_watch 依赖 voc_board，先按依赖顺序拆掉再以
-- 015 之后的最新列契约重建。
DROP VIEW IF EXISTS voc_safety_watch;
DROP VIEW IF EXISTS voc_inbox;
DROP VIEW IF EXISTS voc_board;

ALTER TABLE voc_opportunity DROP COLUMN IF EXISTS channel;

CREATE VIEW voc_board AS
SELECT
  o.opp_id,
  COALESCE(m.status, '考虑中') AS status,
  m.owner,
  o.opp_type, o.src_line, o.prod_line,
  o.category, o.category_set, o.core_tag,
  o.title, o.problem_mode,
  o.desc_phenomenon, o.desc_attribution, o.desc_suggestion,
  o.safety_flag, o.safety_evidence_ids,
  o.evi_total, o.evi_ec, o.evi_social, o.low_star_rate,
  o.dual_source, o.weak_evidence, o.rank_score,
  o.countries, o.product_names, o.rep_snippets,
  o.first_week, o.last_week, o.needs_review, o.backlog,
  o.prompt_ver, o.model_ver,
  m.note, m.decision_note, m.target_release,
  m.release_date, m.improved_product_ids,
  m.updated_at AS status_updated_at,
  (COALESCE(m.status, '考虑中') IN ('在跟进', '项目中')
   AND m.updated_at < now() - interval '8 weeks') AS status_stale,
  o.merged_into,
  o.released_at
FROM voc_opportunity o
LEFT JOIN voc_opportunity_manual m USING (opp_id)
WHERE o.classification_state = '确定';

CREATE VIEW voc_inbox AS
SELECT 'proposal' AS kind, p.proposal_id::text AS ref, p.op_type AS sub_kind,
       p.opp_ids, p.rationale AS detail, p.created_at,
       EXTRACT(day FROM now() - p.created_at)::int AS waiting_days
FROM voc_proposal p
WHERE p.status = 'pending'
  AND NOT EXISTS (
    SELECT 1
      FROM unnest(p.opp_ids) AS x(opp_id)
      JOIN voc_opportunity o ON o.opp_id = x.opp_id
     WHERE o.classification_state = '无效'
  )
UNION ALL
SELECT 'needs_review', o.opp_id, o.opp_type, ARRAY[o.opp_id],
       o.title, o.updated_at,
       EXTRACT(day FROM now() - o.updated_at)::int
FROM voc_opportunity o
WHERE o.needs_review AND NOT o.backlog
  AND o.classification_state = '确定'
UNION ALL
SELECT 'stale_status', b.opp_id, b.status, ARRAY[b.opp_id],
       b.title, b.status_updated_at,
       EXTRACT(day FROM now() - b.status_updated_at)::int
FROM voc_board b WHERE b.status_stale;

CREATE VIEW voc_safety_watch AS
SELECT b.opp_id, b.title, b.status, b.owner, b.evi_total, b.last_week,
       b.desc_phenomenon, b.safety_evidence_ids, b.decision_note
FROM voc_board b
WHERE b.safety_flag
ORDER BY b.last_week DESC, b.evi_total DESC;

GRANT SELECT ON voc_board, voc_inbox, voc_safety_watch
  TO voc_writer, voc_human, voc_reader;

COMMIT;

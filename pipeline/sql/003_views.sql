-- ============================================================
-- 视图：PM 取数入口（PRD v8 §3.4 / §7.5）
-- ============================================================

-- 机器字段 ⋈ 人工字段。注意：合并的是人机两侧，不是线A与线B
-- （线A/线B 的汇聚在管线层完成，见 §6.5）
CREATE OR REPLACE VIEW voc_board AS
SELECT
  o.opp_id,
  COALESCE(m.status, '考虑中')            AS status,
  m.owner,
  o.opp_type, o.src_line, o.channel, o.prod_line,
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
  -- 状态陈旧检测（§7.8）：在跟进/项目中 且 >8 周未更新
  (COALESCE(m.status,'考虑中') IN ('在跟进','项目中')
   AND m.updated_at < now() - interval '8 weeks') AS status_stale
FROM voc_opportunity o
LEFT JOIN voc_opportunity_manual m USING (opp_id);

-- 待办队列：PM 每周要过的三类
CREATE OR REPLACE VIEW voc_inbox AS
SELECT 'proposal' AS kind, p.proposal_id::text AS ref, p.op_type AS sub_kind,
       p.opp_ids, p.rationale AS detail, p.created_at,
       EXTRACT(day FROM now() - p.created_at)::int AS waiting_days
FROM voc_proposal p WHERE p.status = 'pending'
UNION ALL
SELECT 'needs_review', o.opp_id, o.opp_type, ARRAY[o.opp_id],
       o.title, o.updated_at,
       EXTRACT(day FROM now() - o.updated_at)::int
FROM voc_opportunity o WHERE o.needs_review AND NOT o.backlog
UNION ALL
SELECT 'stale_status', b.opp_id, b.status, ARRAY[b.opp_id],
       b.title, b.status_updated_at,
       EXTRACT(day FROM now() - b.status_updated_at)::int
FROM voc_board b WHERE b.status_stale;

-- 安全类清单：强制推送用（§10.1）
CREATE OR REPLACE VIEW voc_safety_watch AS
SELECT b.opp_id, b.title, b.status, b.owner, b.evi_total, b.last_week,
       b.desc_phenomenon, b.safety_evidence_ids, b.decision_note
FROM voc_board b
WHERE b.safety_flag
ORDER BY b.last_week DESC, b.evi_total DESC;

-- 周度指标（§9.2）
CREATE OR REPLACE VIEW voc_weekly_metrics AS
WITH pool AS (
  -- 必须过滤 src_line='电商'：线A 的定义就是电商评论。
  -- 漏了这个条件会把社媒证据算进分母，覆盖率虚低近一倍。
  SELECT count(*) AS line_a_pool
  FROM voc_evidence e JOIN voc_message m USING (message_id)
  WHERE e.is_product AND NOT e.low_conf AND e.sentiment = '负面'
    AND e.snippet IS NOT NULL AND m.src_line = '电商'
), attached AS (
  SELECT count(DISTINCT (oe.message_id, oe.seq)) AS n
  FROM voc_opp_evidence oe
  JOIN voc_evidence e USING (message_id, seq)
  JOIN voc_message m USING (message_id)
  WHERE m.src_line = '电商' AND e.sentiment = '负面' AND e.is_product AND NOT e.low_conf
)
SELECT
  (SELECT line_a_pool FROM pool)                                   AS line_a_pool,
  (SELECT n FROM attached)                                         AS line_a_attached,
  ROUND((SELECT n FROM attached)::numeric
        / NULLIF((SELECT line_a_pool FROM pool),0) * 100, 2)       AS line_a_coverage_pct,
  (SELECT count(*) FROM voc_opportunity WHERE NOT backlog)         AS active_opps,
  (SELECT count(*) FROM voc_opportunity WHERE backlog)             AS backlog_opps,
  (SELECT count(*) FROM voc_opportunity WHERE dual_source)         AS dual_source_opps,
  (SELECT count(*) FROM voc_opportunity WHERE weak_evidence)       AS weak_opps,
  (SELECT count(*) FROM voc_opportunity WHERE safety_flag)         AS safety_opps,
  (SELECT count(*) FROM voc_opportunity WHERE needs_review)        AS needs_review_opps,
  (SELECT count(*) FROM voc_proposal WHERE status='pending')       AS pending_proposals,
  (SELECT count(*) FROM voc_unclassified_evidence)                 AS unclassified_total;

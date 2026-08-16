-- ============================================================
-- 阶段 2a：机会点分类改由完整证据集的 SPU 挂载判定。
-- 本迁移只标记无效机会点，不删除机会点或证据。
-- 以 voc_admin 执行；固定 public schema，保持物化视图 owner 与刷新函数一致。
-- ============================================================

BEGIN;

SET LOCAL search_path = public, pg_temp;

DO $$
BEGIN
  IF current_user <> 'voc_admin' THEN
    RAISE EXCEPTION '015 必须以 voc_admin 执行，当前角色为 %', current_user;
  END IF;
END $$;

-- 与周度 REFRESH 的串行化靠下方 DROP MATERIALIZED VIEW 自带的 ACCESS EXCLUSIVE 锁：
-- REFRESH 与 DROP 争的是同一把物化视图锁，先到先得，不会并发。
-- 这里不能写 LOCK TABLE voc_spu_issue —— PostgreSQL 明确拒绝对物化视图加显式表锁
-- （cannot lock relation ... not supported for materialized views），2026-08-17 实测。
-- 真正的保护仍是受控窗口：执行前停止 Dagster 调度。
LOCK TABLE public.voc_opportunity, public.voc_opp_evidence IN SHARE ROW EXCLUSIVE MODE;

ALTER TABLE voc_opportunity
  ADD COLUMN IF NOT EXISTS classification_state text NOT NULL DEFAULT '确定'
    CHECK (classification_state IN ('确定', '无效')),
  ADD COLUMN IF NOT EXISTS classify_rule text;

COMMENT ON COLUMN voc_opportunity.classification_state IS
  '机会点有效性：确定/无效。与老品迭代/新品创新分类正交。';
COMMENT ON COLUMN voc_opportunity.classify_rule IS
  '最后一次按完整证据集分类时命中的规则：R0/R1/R2/R3。';

-- PostgreSQL READ COMMITTED 默认每条语句取新快照。迁移的回填、物化视图
-- 与自检必须看到同一份证据，因此在事务内阻止事实/关系层并发写入。
LOCK TABLE voc_message, voc_evidence, voc_opp_evidence
  IN SHARE ROW EXCLUSIVE MODE;

-- opp_type 从本迁移起是证据派生的路由字段，不再是人工接管后
-- 应冻结的语义内容。正式演进 locked guard 契约，不禁用或绕过触发器。
-- evi_total 原本就在统计白名单内，因此本次实际证据数回填可正常放行。
CREATE OR REPLACE FUNCTION voc_guard_locked() RETURNS trigger AS $$
DECLARE st text;
BEGIN
  SELECT status INTO st FROM voc_opportunity_manual WHERE opp_id = NEW.opp_id;

  IF st IS NULL OR st = '考虑中' THEN
    RETURN NEW;
  END IF;

  NEW.title            := OLD.title;
  NEW.problem_mode     := OLD.problem_mode;
  NEW.desc_phenomenon  := OLD.desc_phenomenon;
  NEW.desc_attribution := OLD.desc_attribution;
  NEW.desc_suggestion  := OLD.desc_suggestion;
  NEW.core_tag         := OLD.core_tag;
  NEW.category         := OLD.category;
  NEW.mode_vec         := OLD.mode_vec;
  -- 白名单保持 NEW 值：opp_type / classification_state / classify_rule /
  --   evi_total / evi_ec / evi_social / low_star_rate / countries /
  --   product_names / rep_snippets / rank_score / safety_flag /
  --   safety_evidence_ids / dual_source / weak_evidence / category_set /
  --   last_week / needs_review / backlog / updated_at
  RETURN NEW;
END $$ LANGUAGE plpgsql;

COMMENT ON FUNCTION voc_guard_locked() IS
  'locked 行保护人工接管的语义字段；分类与统计按完整证据集派生，允许重算。';

-- 用关系表实际行数作为唯一证据总数，不信任旧 evi_total。
-- R2/R3 使用证据所属消息的 src_line，不使用机会点的「发现方」。
WITH evidence_summary AS (
  SELECT o.opp_id,
         count(oe.opp_id)::int AS evidence_count,
         COALESCE(bool_or(cardinality(m.spu) > 0), false) AS has_spu,
         COALESCE(bool_or(m.src_line = '社媒'), false) AS has_social,
         COALESCE(bool_or(m.src_line = '电商'), false) AS has_ecommerce
    FROM voc_opportunity o
    LEFT JOIN voc_opp_evidence oe ON oe.opp_id = o.opp_id
    LEFT JOIN voc_message m ON m.message_id = oe.message_id
   GROUP BY o.opp_id
), classified AS (
  SELECT opp_id,
         evidence_count,
         CASE
           WHEN evidence_count = 0 THEN NULL
           WHEN has_spu THEN '老品迭代'
           WHEN has_social THEN '新品创新'
           WHEN has_ecommerce THEN NULL
         END AS opp_type,
         CASE
           WHEN evidence_count = 0 THEN '无效'
           WHEN has_spu THEN '确定'
           WHEN has_social THEN '确定'
           WHEN has_ecommerce THEN '无效'
         END AS classification_state,
         CASE
           WHEN evidence_count = 0 THEN 'R0'
           WHEN has_spu THEN 'R1'
           WHEN has_social THEN 'R2'
           WHEN has_ecommerce THEN 'R3'
         END AS classify_rule
    FROM evidence_summary
)
UPDATE voc_opportunity o
   SET evi_total = c.evidence_count,
       opp_type = c.opp_type,
       classification_state = c.classification_state,
       classify_rule = c.classify_rule
  FROM classified c
 WHERE c.opp_id = o.opp_id;

-- 数据库通用看板入口也要隔离无效行。保持 007 已发布的列顺序
-- 不变，只收紧行集；避免 CREATE OR REPLACE VIEW 改列导致依赖方失效。
CREATE OR REPLACE VIEW voc_board AS
SELECT
  o.opp_id,
  COALESCE(m.status, '考虑中') AS status,
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
  (COALESCE(m.status, '考虑中') IN ('在跟进', '项目中')
   AND m.updated_at < now() - interval '8 weeks') AS status_stale,
  o.merged_into,
  o.released_at
FROM voc_opportunity o
LEFT JOIN voc_opportunity_manual m USING (opp_id)
WHERE o.classification_state = '确定';

CREATE OR REPLACE VIEW voc_inbox AS
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

CREATE OR REPLACE VIEW voc_weekly_metrics AS
WITH pool AS (
  SELECT count(*) AS line_a_pool
  FROM voc_evidence e JOIN voc_message m USING (message_id)
  WHERE e.is_product AND NOT e.low_conf AND e.sentiment = '负面'
    AND e.snippet IS NOT NULL AND m.src_line = '电商'
    AND cardinality(m.spu) > 0
), attached AS (
  SELECT count(DISTINCT (oe.message_id, oe.seq)) AS n
  FROM voc_opp_evidence oe
  JOIN voc_evidence e USING (message_id, seq)
  JOIN voc_message m USING (message_id)
  WHERE m.src_line = '电商' AND cardinality(m.spu) > 0
    AND e.sentiment = '负面' AND e.is_product AND NOT e.low_conf
)
SELECT
  (SELECT line_a_pool FROM pool) AS line_a_pool,
  (SELECT n FROM attached) AS line_a_attached,
  ROUND((SELECT n FROM attached)::numeric
        / NULLIF((SELECT line_a_pool FROM pool), 0) * 100, 2) AS line_a_coverage_pct,
  (SELECT count(*) FROM voc_opportunity
    WHERE NOT backlog AND classification_state = '确定') AS active_opps,
  (SELECT count(*) FROM voc_opportunity
    WHERE backlog AND classification_state = '确定') AS backlog_opps,
  (SELECT count(*) FROM voc_opportunity
    WHERE dual_source AND classification_state = '确定') AS dual_source_opps,
  (SELECT count(*) FROM voc_opportunity
    WHERE weak_evidence AND classification_state = '确定') AS weak_opps,
  (SELECT count(*) FROM voc_opportunity
    WHERE safety_flag AND classification_state = '确定') AS safety_opps,
  (SELECT count(*) FROM voc_opportunity
    WHERE needs_review AND classification_state = '确定') AS needs_review_opps,
  (SELECT count(*) FROM voc_proposal p
    WHERE p.status = 'pending'
      AND NOT EXISTS (
        SELECT 1
          FROM unnest(p.opp_ids) AS x(opp_id)
          JOIN voc_opportunity o ON o.opp_id = x.opp_id
         WHERE o.classification_state = '无效'
      )) AS pending_proposals,
  (SELECT count(*) FROM voc_unclassified_evidence) AS unclassified_total;

-- 解耦后「老品迭代」的语义已是「任一证据挂 SPU」，不再是电商渠道。
-- 因此去掉旧 m.src_line='电商' 条件，改为同时约束机会点类型与有效性；
-- 挂到 SPU 的社媒证据也能路由到对应产品卡。每个 SPU/问题 >=2 的阈值不变。
DROP MATERIALIZED VIEW IF EXISTS voc_spu_issue;

CREATE MATERIALIZED VIEW voc_spu_issue AS
WITH attached AS (
  SELECT DISTINCT
         NULLIF(btrim(s.spu), '') AS spu,
         oe.opp_id,
         oe.message_id,
         oe.seq,
         oe.attach_week,
         e.tax_path
    FROM voc_opp_evidence oe
    JOIN voc_opportunity o ON o.opp_id = oe.opp_id
    JOIN voc_message m USING (message_id)
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
   CROSS JOIN LATERAL unnest(m.spu) AS s(spu)
   WHERE o.opp_type = '老品迭代'
     AND o.classification_state = '确定'
     AND cardinality(m.spu) > 0
     AND NULLIF(btrim(s.spu), '') IS NOT NULL
)
SELECT spu,
       opp_id,
       count(*) AS evi_count,
       mode() WITHIN GROUP (ORDER BY tax_path)
         FILTER (WHERE tax_path IS NOT NULL) AS tax_path,
       min(attach_week) AS first_attach_week,
       max(attach_week) AS last_attach_week
  FROM attached
 GROUP BY spu, opp_id
HAVING count(*) >= 2;

CREATE UNIQUE INDEX ux_voc_spu_issue_key
  ON voc_spu_issue (spu, opp_id);
CREATE INDEX ix_voc_spu_issue_opp
  ON voc_spu_issue (opp_id);

GRANT SELECT ON voc_spu_issue TO voc_writer, voc_human, voc_reader;

DO $$
DECLARE
  old_valid_count bigint;
  new_valid_count bigint;
  invalid_count bigint;
  total_count bigint;
  null_rule_count bigint;
  evi_total_mismatch_count bigint;
  r0_count bigint;
  r1_count bigint;
  r2_count bigint;
  r3_count bigint;
BEGIN
  SELECT count(*) FILTER (
           WHERE opp_type = '老品迭代' AND classification_state = '确定'),
         count(*) FILTER (
           WHERE opp_type = '新品创新' AND classification_state = '确定'),
         count(*) FILTER (WHERE classification_state = '无效'),
         count(*),
         count(*) FILTER (WHERE classify_rule IS NULL)
    INTO old_valid_count, new_valid_count, invalid_count, total_count, null_rule_count
    FROM voc_opportunity;

  SELECT count(*) FILTER (WHERE classify_rule = 'R0'),
         count(*) FILTER (WHERE classify_rule = 'R1'),
         count(*) FILTER (WHERE classify_rule = 'R2'),
         count(*) FILTER (WHERE classify_rule = 'R3')
    INTO r0_count, r1_count, r2_count, r3_count
    FROM voc_opportunity;

  SELECT count(*) INTO evi_total_mismatch_count
    FROM voc_opportunity o
   WHERE o.evi_total IS DISTINCT FROM (
     SELECT count(*)::int FROM voc_opp_evidence oe WHERE oe.opp_id = o.opp_id
   );

  IF old_valid_count <> 264
     OR new_valid_count <> 368
     OR invalid_count <> 13
     OR total_count <> 645
     OR null_rule_count <> 0
     OR evi_total_mismatch_count <> 0
     OR r0_count <> 12
     OR r1_count <> 264
     OR r2_count <> 368
     OR r3_count <> 1 THEN
    RAISE EXCEPTION
      '分类契约回填自检失败：老品/确定=%，新品/确定=%，无效=%，总计=%，规则为NULL=%，evi_total漂移=%，R0/R1/R2/R3=%/%/%/%',
      old_valid_count, new_valid_count, invalid_count, total_count,
      null_rule_count, evi_total_mismatch_count,
      r0_count, r1_count, r2_count, r3_count;
  END IF;

  RAISE NOTICE
    '分类契约回填自检通过：老品/确定=%，新品/确定=%，无效=%，总计=%，规则为NULL=%，evi_total漂移=%，R0/R1/R2/R3=%/%/%/%',
    old_valid_count, new_valid_count, invalid_count, total_count,
    null_rule_count, evi_total_mismatch_count,
    r0_count, r1_count, r2_count, r3_count;
END $$;

COMMIT;

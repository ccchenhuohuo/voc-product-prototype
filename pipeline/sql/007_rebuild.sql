-- ============================================================
-- 看板重建配套（冻结 PRD v1.1 / 设计稿 v2）。以 voc_admin 执行。
-- ============================================================

-- 1) 放行时间戳：批阅截止日与波次的依据。
--    此前 release_to_pm 只翻 backlog 布尔，不知道「哪周放行的」，
--    截止日与逾期判定无从谈起。
ALTER TABLE voc_opportunity ADD COLUMN IF NOT EXISTS released_at timestamptz;
UPDATE voc_opportunity SET released_at = now()
 WHERE NOT backlog AND released_at IS NULL;

-- 2) PM 责任域偏好（软筛选，非权限墙）：批阅页默认只看自己勾选的品类
CREATE TABLE IF NOT EXISTS voc_pm_pref (
  pm_name    text PRIMARY KEY,
  prod_lines text[] NOT NULL DEFAULT '{}',
  updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON voc_pm_pref TO voc_human;
GRANT SELECT ON voc_pm_pref TO voc_reader;

-- 3) 撤销首标（15 分钟内收回）需要删除人工行。
--    审计不受影响：voc_status_log 的历史行永不删除；
--    应用层限定只能撤自己的、且仅限刚写入的行。
GRANT DELETE ON voc_opportunity_manual TO voc_human;

-- 4) voc_board 追加 released_at（只能追加在末尾，见 006 的教训）
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
  (COALESCE(m.status,'考虑中') IN ('在跟进','项目中')
   AND m.updated_at < now() - interval '8 weeks') AS status_stale,
  o.merged_into,
  o.released_at
FROM voc_opportunity o
LEFT JOIN voc_opportunity_manual m USING (opp_id);

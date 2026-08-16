-- ============================================================
-- 提案执行器所需（二期 PRD §6.3）。以 voc_admin 执行。
-- ============================================================

-- MERGE 后源条目的去向。不物理删除源条目，因为
-- voc_opportunity_manual 的外键【没有】ON DELETE CASCADE——
-- 那是有意的保护，删机器行不得静默抹掉人工决策。
-- 源条目改为 backlog=true 移出视野，并用本列标注并入了谁，
-- 让 PM 从旧链接进来时能一路跳到目标条目。
ALTER TABLE voc_opportunity ADD COLUMN IF NOT EXISTS merged_into text;

CREATE INDEX IF NOT EXISTS ix_opp_merged_into
  ON voc_opportunity (merged_into) WHERE merged_into IS NOT NULL;

-- match_by 记录「这条证据因何挂到该机会点」。人工裁决 MERGE 迁移过来的证据
-- 是一种独立来源：既不是规则命中，也不是模型判同，而是 PM 拍板的结果。
-- 单列一个取值，下钻页才能如实说明来源。
ALTER TABLE voc_opp_evidence DROP CONSTRAINT IF EXISTS voc_opp_evidence_match_by_check;
ALTER TABLE voc_opp_evidence ADD CONSTRAINT voc_opp_evidence_match_by_check
  CHECK (match_by = ANY (ARRAY['rule','vector','llm','cross_line','merge']));

-- voc_board 暴露该列，看板据此显示「已并入 →」提示。
-- 注意：CREATE OR REPLACE VIEW 只能在【末尾追加】列，不能插在中间
-- （报 "cannot change name of view column"），所以 merged_into 放最后。
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
  o.merged_into                     -- 新增列必须追加在末尾
FROM voc_opportunity o
LEFT JOIN voc_opportunity_manual m USING (opp_id);

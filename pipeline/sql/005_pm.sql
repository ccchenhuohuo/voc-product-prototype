-- ============================================================
-- 二期看板迁移（二期 PRD §9）。以 voc_admin 执行。
-- ============================================================

-- 1) 裁决留痕：与「不考虑」必填理由同级的决策，必须可写备注
ALTER TABLE voc_proposal ADD COLUMN IF NOT EXISTS decision_note text;
GRANT UPDATE (status, decided_by, decided_at, decision_note) ON voc_proposal TO voc_human;

-- 2) 看板只读角色补充：
--    状态历史（详情页时间线）、提案（裁决页与详情页关联区）、
--    血缘（已裁决提案的「已执行/待执行」标记）、运行日志（指标页抽取量趋势）
GRANT SELECT ON voc_status_log, voc_proposal, voc_opp_lineage, voc_run_log TO voc_reader;

-- 3) 列表搜索：pg_trgm 子串索引（中日英已在 M0 实测）
CREATE INDEX IF NOT EXISTS ix_opp_title_trgm ON voc_opportunity USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_opp_mode_trgm  ON voc_opportunity USING gin (problem_mode gin_trgm_ops);

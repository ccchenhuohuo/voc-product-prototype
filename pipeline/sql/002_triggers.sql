-- ============================================================
-- 触发器：状态即锁的技术强制（PRD v8 §7.5）
-- 两张表分离【不足以】阻止覆盖：锁状态在 manual 表，
-- 被改写的字段在 opportunity 表。必须字段级保护。
-- ============================================================

-- ------------------------------------------------------------
-- 1) locked 条目的语义字段不可被机器改写
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION voc_guard_locked() RETURNS trigger AS $$
DECLARE st text;
BEGIN
  SELECT status INTO st FROM voc_opportunity_manual WHERE opp_id = NEW.opp_id;

  -- 未被人接管（无记录或"考虑中"）=> 机器有完全改写权
  IF st IS NULL OR st = '考虑中' THEN
    RETURN NEW;
  END IF;

  -- locked：语义字段一律保持原值，仅放行统计类白名单
  NEW.title            := OLD.title;
  NEW.problem_mode     := OLD.problem_mode;
  NEW.desc_phenomenon  := OLD.desc_phenomenon;
  NEW.desc_attribution := OLD.desc_attribution;
  NEW.desc_suggestion  := OLD.desc_suggestion;
  NEW.core_tag         := OLD.core_tag;
  NEW.category         := OLD.category;
  NEW.opp_type         := OLD.opp_type;
  NEW.mode_vec         := OLD.mode_vec;
  -- 白名单（保持 NEW 值）：evi_total / evi_ec / evi_social / low_star_rate /
  --   countries / product_names / rep_snippets / rank_score / safety_flag /
  --   safety_evidence_ids / dual_source / weak_evidence / category_set /
  --   last_week / needs_review / backlog / updated_at
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voc_guard_locked ON voc_opportunity;
CREATE TRIGGER trg_voc_guard_locked
  BEFORE UPDATE ON voc_opportunity
  FOR EACH ROW EXECUTE FUNCTION voc_guard_locked();

-- ------------------------------------------------------------
-- 2) 状态变更自动写审计日志 + 「不考虑」必填理由
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION voc_log_status() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NEW;
  END IF;

  IF NEW.status = '不考虑'
     AND (NEW.decision_note IS NULL OR btrim(NEW.decision_note) = '') THEN
    RAISE EXCEPTION
      '置为「不考虑」时 decision_note 必填（墓碑基准与后续复议依赖它）';
  END IF;

  INSERT INTO voc_status_log(opp_id, from_status, to_status, reason, changed_by)
  VALUES (NEW.opp_id,
          CASE WHEN TG_OP = 'UPDATE' THEN OLD.status ELSE NULL END,
          NEW.status,
          NEW.decision_note,
          COALESCE(NEW.updated_by, current_user));

  NEW.updated_at := now();
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voc_log_status ON voc_opportunity_manual;
CREATE TRIGGER trg_voc_log_status
  BEFORE INSERT OR UPDATE ON voc_opportunity_manual
  FOR EACH ROW EXECUTE FUNCTION voc_log_status();

-- ------------------------------------------------------------
-- 3) 安全类条目不得被自动合并 / 墓碑抑制（§10.1）
--    在 lineage 写入时兜底拦截（应用层也会检查）
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION voc_guard_safety_merge() RETURNS trigger AS $$
DECLARE n int;
BEGIN
  IF NEW.op_type = 'MERGE' AND NEW.decided_by = 'machine' THEN
    SELECT count(*) INTO n
    FROM voc_opportunity
    WHERE opp_id = ANY(NEW.parent_ids) AND safety_flag;
    IF n > 0 THEN
      RAISE EXCEPTION
        '安全类机会点（safety_flag=true）不得自动合并，必须走 voc_proposal 人工确认';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voc_guard_safety ON voc_opp_lineage;
CREATE TRIGGER trg_voc_guard_safety
  BEFORE INSERT ON voc_opp_lineage
  FOR EACH ROW EXECUTE FUNCTION voc_guard_safety_merge();

-- ------------------------------------------------------------
-- 4) weak_evidence 由 evi_total 自动派生，避免应用层漏置
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION voc_derive_flags() RETURNS trigger AS $$
BEGIN
  NEW.weak_evidence := COALESCE(NEW.evi_total, 0) <= 2;
  NEW.dual_source   := COALESCE(NEW.evi_ec, 0) > 0 AND COALESCE(NEW.evi_social, 0) > 0;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voc_derive_flags ON voc_opportunity;
CREATE TRIGGER trg_voc_derive_flags
  BEFORE INSERT OR UPDATE ON voc_opportunity
  FOR EACH ROW EXECUTE FUNCTION voc_derive_flags();

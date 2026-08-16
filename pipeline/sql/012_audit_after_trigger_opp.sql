-- ============================================================
-- 把 011 的修法同样应用到机会点人工层
-- 以 voc_admin 执行。
--
-- voc_opportunity_manual 上的 voc_log_status 也是「BEFORE 里写日志」，
-- 新品创新卡改状态走的同样是 INSERT ... ON CONFLICT DO UPDATE，
-- 因此会产生和 011 完全相同的幻影记录。同法拆成 BEFORE 校验 + AFTER 记账。
-- ============================================================

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

  NEW.updated_at := now();
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION voc_audit_opp_status() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NULL;
  END IF;

  INSERT INTO voc_status_log(opp_id, from_status, to_status, reason, changed_by)
  VALUES (NEW.opp_id,
          CASE WHEN TG_OP = 'UPDATE' THEN OLD.status ELSE NULL END,
          NEW.status,
          NEW.decision_note,
          COALESCE(NEW.updated_by, current_user));
  RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voc_audit_opp_status ON voc_opportunity_manual;
CREATE TRIGGER trg_voc_audit_opp_status
  AFTER INSERT OR UPDATE ON voc_opportunity_manual
  FOR EACH ROW EXECUTE FUNCTION voc_audit_opp_status();

COMMENT ON FUNCTION voc_log_status() IS
  'BEFORE：校验「不考虑」必填理由、维护 updated_at。不写日志。';
COMMENT ON FUNCTION voc_audit_opp_status() IS
  'AFTER：写机会点状态流水。放 AFTER 才能避开 ON CONFLICT DO UPDATE 的幻影 INSERT。';

-- 自检：复现应用写法，两次变更只应留两条日志，且无「以不考虑新建」的幻影。
DO $$
DECLARE k text; n int;
BEGIN
  SELECT opp_id INTO k FROM voc_opportunity
   WHERE opp_type = '新品创新' AND merged_into IS NULL LIMIT 1;

  INSERT INTO voc_opportunity_manual(opp_id, status, updated_by)
       VALUES (k, '在跟进', '__selftest__');
  INSERT INTO voc_opportunity_manual(opp_id, status, decision_note, updated_by)
       VALUES (k, '不考虑', '自检', '__selftest__')
  ON CONFLICT (opp_id) DO UPDATE
     SET status = EXCLUDED.status,
         decision_note = EXCLUDED.decision_note,
         updated_by = EXCLUDED.updated_by;

  SELECT count(*) INTO n FROM voc_status_log WHERE changed_by = '__selftest__';
  IF n <> 2 THEN
    RAISE EXCEPTION '两次状态变更应恰好留 2 条日志，实得 % 条（幻影未消除）', n;
  END IF;
  IF EXISTS (SELECT 1 FROM voc_status_log
              WHERE changed_by = '__selftest__' AND from_status IS NULL AND to_status = '不考虑') THEN
    RAISE EXCEPTION '仍存在幻影记录';
  END IF;

  DELETE FROM voc_status_log        WHERE changed_by = '__selftest__';
  DELETE FROM voc_opportunity_manual WHERE updated_by = '__selftest__';
  RAISE NOTICE '自检通过：机会点人工层幻影已消除';
END $$;

SELECT '机会点 AFTER 审计触发器已生效' AS 结果;

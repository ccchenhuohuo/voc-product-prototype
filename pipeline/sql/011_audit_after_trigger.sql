-- ============================================================
-- 修正审计日志的幻影记录
-- 以 voc_admin 执行。
--
-- 缺陷：应用用 INSERT ... ON CONFLICT DO UPDATE 改状态。PostgreSQL 在冲突时
-- 会先触发 BEFORE INSERT（日志里落一条 from_status 为空的"新建"），再触发
-- BEFORE UPDATE（落第二条真实记录）。一次改状态产生两条日志，其中一条描述
-- 的是从未发生过的事——比如"以『不考虑』新建了这张卡"。审计日志是追责与
-- 复议的依据，不能有这种记录。
--
-- 修法：把校验与赋值留在 BEFORE，把写日志挪到 AFTER。ON CONFLICT DO UPDATE
-- 在冲突时不会触发 AFTER INSERT，只触发 AFTER UPDATE，幻影自然消失。
-- ============================================================

-- ① BEFORE：只做校验与字段赋值，不再写日志。
CREATE OR REPLACE FUNCTION voc_log_spu_issue_status() RETURNS trigger AS $$
DECLARE current_evi_count int;
BEGIN
  NEW.updated_at := now();

  IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NEW;
  END IF;

  IF NEW.status = '不考虑'
     AND (NEW.decision_note IS NULL OR btrim(NEW.decision_note) = '') THEN
    RAISE EXCEPTION
      '置为「不考虑」时 decision_note 必填（证据基准与后续复议依赖它）';
  END IF;

  IF NEW.status = '不考虑' THEN
    SELECT i.evi_count::int INTO current_evi_count
      FROM voc_spu_issue i
     WHERE i.spu = NEW.spu AND i.opp_id = NEW.opp_id;
    IF current_evi_count IS NULL THEN
      RAISE EXCEPTION
        '置为「不考虑」时找不到当前 SPU 问题条目，无法记录 baseline_evi_count';
    END IF;
    NEW.baseline_evi_count := current_evi_count;
  END IF;

  -- closed_at 只跟随「已完成」。离开该状态就清空：复活后仍挂着旧闭环时间
  -- 会让「上市后是否还有反馈」的判读凭空多一个假分界；历史留在审计日志里。
  IF NEW.status = '已完成' THEN
    NEW.closed_at := COALESCE(NEW.closed_at, now());
  ELSE
    NEW.closed_at := NULL;
  END IF;

  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- ② AFTER：只写日志。状态没变就不记，避免改备注也刷出一条流水。
CREATE OR REPLACE FUNCTION voc_audit_spu_issue_status() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NULL;
  END IF;

  INSERT INTO voc_spu_issue_log
         (spu, opp_id, from_status, to_status, reason, changed_by)
  VALUES (NEW.spu,
          NEW.opp_id,
          CASE WHEN TG_OP = 'UPDATE' THEN OLD.status ELSE NULL END,
          NEW.status,
          NEW.decision_note,
          COALESCE(NEW.updated_by, current_user));
  RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voc_audit_spu_issue_status ON voc_spu_issue_manual;
CREATE TRIGGER trg_voc_audit_spu_issue_status
  AFTER INSERT OR UPDATE ON voc_spu_issue_manual
  FOR EACH ROW EXECUTE FUNCTION voc_audit_spu_issue_status();

COMMENT ON FUNCTION voc_log_spu_issue_status() IS
  'BEFORE：校验必填理由、捕获 baseline_evi_count、维护 closed_at。不写日志。';
COMMENT ON FUNCTION voc_audit_spu_issue_status() IS
  'AFTER：写状态变更流水。放在 AFTER 才能避开 ON CONFLICT DO UPDATE 的幻影 INSERT。';

-- 自检：一次 upsert 只应留下一条日志。全程回滚，不落任何数据。
DO $$
DECLARE k record; n int;
BEGIN
  SELECT spu, opp_id INTO k FROM voc_spu_issue ORDER BY evi_count DESC LIMIT 1;
  INSERT INTO voc_spu_issue_manual(spu, opp_id, status, updated_by)
       VALUES (k.spu, k.opp_id, '在跟进', '__selftest__');
  -- 复现应用的写法：同一主键再 upsert 一次
  INSERT INTO voc_spu_issue_manual(spu, opp_id, status, decision_note, updated_by)
       VALUES (k.spu, k.opp_id, '不考虑', '自检', '__selftest__')
  ON CONFLICT (spu, opp_id) DO UPDATE
     SET status = EXCLUDED.status,
         decision_note = EXCLUDED.decision_note,
         updated_by = EXCLUDED.updated_by;

  SELECT count(*) INTO n FROM voc_spu_issue_log WHERE changed_by = '__selftest__';
  IF n <> 2 THEN
    RAISE EXCEPTION '两次状态变更应恰好留 2 条日志，实得 % 条（幻影未消除）', n;
  END IF;
  IF EXISTS (SELECT 1 FROM voc_spu_issue_log
              WHERE changed_by = '__selftest__' AND from_status IS NULL AND to_status = '不考虑') THEN
    RAISE EXCEPTION '仍存在幻影记录：以「不考虑」新建，但实际是更新';
  END IF;

  DELETE FROM voc_spu_issue_log     WHERE changed_by = '__selftest__';
  DELETE FROM voc_spu_issue_manual  WHERE updated_by = '__selftest__';
  RAISE NOTICE '自检通过：一次 upsert 只留一条日志，幻影已消除';
END $$;

SELECT 'AFTER 审计触发器已生效' AS 结果;

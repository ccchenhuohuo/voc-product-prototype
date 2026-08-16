-- ============================================================
-- M0 验收：触发器与角色权限必须真的生效
-- 每项失败即 RAISE EXCEPTION，整脚本非零退出
-- 用法（仅验收库，从 pipeline/ 运行）：
--   psql 'postgresql://voc_admin@127.0.0.1:5434/voc_acceptance' \
--     -v ON_ERROR_STOP=1 -f tests/m0_acceptance.sql
-- 前置：已应用 001–004，执行角色具备测试所需 DDL/DML 权限；不要指向生产库。
-- 成功路径由末尾 ROLLBACK 回滚全部测试数据。
-- ============================================================
\set ON_ERROR_STOP on
BEGIN;

CREATE OR REPLACE FUNCTION assert(cond boolean, msg text) RETURNS void AS $$
BEGIN
  IF NOT cond THEN RAISE EXCEPTION 'ASSERT FAILED: %', msg; END IF;
  RAISE NOTICE '  PASS  %', msg;
END $$ LANGUAGE plpgsql;

-- 准备两条机会点
INSERT INTO voc_opportunity(opp_id, opp_type, src_line, core_tag, title,
                            problem_mode, desc_phenomenon, evi_total, evi_ec, evi_social)
VALUES ('T-UNLOCKED','老品迭代','电商','耐用性','原标题U','原模式U','原现象U', 5, 5, 0),
       ('T-LOCKED',  '老品迭代','电商','耐用性','原标题L','原模式L','原现象L', 5, 5, 0);

-- ---------- 1. weak_evidence / dual_source 自动派生 ----------
SELECT assert((SELECT NOT weak_evidence FROM voc_opportunity WHERE opp_id='T-UNLOCKED'),
              '1a evi_total=5 时 weak_evidence 应为 false');
SELECT assert((SELECT NOT dual_source FROM voc_opportunity WHERE opp_id='T-UNLOCKED'),
              '1b 单源时 dual_source 应为 false');
UPDATE voc_opportunity SET evi_total=2, evi_social=1 WHERE opp_id='T-UNLOCKED';
SELECT assert((SELECT weak_evidence AND dual_source FROM voc_opportunity WHERE opp_id='T-UNLOCKED'),
              '1c evi_total=2 且双源时两个派生字段应自动置 true');
UPDATE voc_opportunity SET evi_total=5, evi_social=0 WHERE opp_id='T-UNLOCKED';

-- ---------- 2. 未锁定条目：机器可自由改写 ----------
UPDATE voc_opportunity SET title='新标题U', desc_phenomenon='新现象U' WHERE opp_id='T-UNLOCKED';
SELECT assert((SELECT title='新标题U' FROM voc_opportunity WHERE opp_id='T-UNLOCKED'),
              '2a 未接管条目的标题应可被改写');

-- 显式标为「考虑中」后仍应可改写
INSERT INTO voc_opportunity_manual(opp_id,status,updated_by) VALUES ('T-UNLOCKED','考虑中','tester');
UPDATE voc_opportunity SET title='再改一次U' WHERE opp_id='T-UNLOCKED';
SELECT assert((SELECT title='再改一次U' FROM voc_opportunity WHERE opp_id='T-UNLOCKED'),
              '2b 状态=考虑中时标题仍应可被改写');

-- ---------- 3. 锁定条目：语义字段不可被改写 ----------
INSERT INTO voc_opportunity_manual(opp_id,status,owner,updated_by)
VALUES ('T-LOCKED','项目中','pm-a','tester');
UPDATE voc_opportunity
   SET title='恶意改写', desc_phenomenon='恶意现象', desc_suggestion='恶意建议',
       core_tag='被篡改', problem_mode='被篡改',
       evi_total=99, rank_score=12.5, last_week='2026-W40'
 WHERE opp_id='T-LOCKED';
SELECT assert((SELECT title='原标题L' FROM voc_opportunity WHERE opp_id='T-LOCKED'),
              '3a 锁定条目的 title 必须保持原值');
SELECT assert((SELECT desc_phenomenon='原现象L' FROM voc_opportunity WHERE opp_id='T-LOCKED'),
              '3b 锁定条目的 desc_phenomenon 必须保持原值');
SELECT assert((SELECT core_tag='耐用性' FROM voc_opportunity WHERE opp_id='T-LOCKED'),
              '3c 锁定条目的 core_tag 必须保持原值');
SELECT assert((SELECT problem_mode='原模式L' FROM voc_opportunity WHERE opp_id='T-LOCKED'),
              '3d 锁定条目的 problem_mode 必须保持原值');
SELECT assert((SELECT evi_total=99 FROM voc_opportunity WHERE opp_id='T-LOCKED'),
              '3e 白名单字段 evi_total 应放行');
SELECT assert((SELECT rank_score=12.5 FROM voc_opportunity WHERE opp_id='T-LOCKED'),
              '3f 白名单字段 rank_score 应放行');
SELECT assert((SELECT last_week='2026-W40' FROM voc_opportunity WHERE opp_id='T-LOCKED'),
              '3g 白名单字段 last_week 应放行');

-- ---------- 4. 状态变更自动写审计日志 ----------
SELECT assert((SELECT count(*)>=2 FROM voc_status_log WHERE opp_id IN ('T-LOCKED','T-UNLOCKED')),
              '4a 状态变更应自动写 voc_status_log');
UPDATE voc_opportunity_manual SET status='已完成', updated_by='pm-a' WHERE opp_id='T-LOCKED';
SELECT assert((SELECT count(*)=1 FROM voc_status_log
               WHERE opp_id='T-LOCKED' AND from_status='项目中' AND to_status='已完成'),
              '4b 应记录 from/to 状态');

-- ---------- 5.「不考虑」必填 decision_note ----------
DO $$
BEGIN
  BEGIN
    UPDATE voc_opportunity_manual SET status='不考虑', decision_note=NULL WHERE opp_id='T-UNLOCKED';
    RAISE EXCEPTION 'ASSERT FAILED: 5a 缺 decision_note 时应被拒绝，但通过了';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE '%ASSERT FAILED%' THEN RAISE; END IF;
    RAISE NOTICE '  PASS  5a 缺 decision_note 时置「不考虑」被正确拒绝';
  END;
END $$;
UPDATE voc_opportunity_manual SET status='不考虑', decision_note='成本过高', updated_by='pm-a'
 WHERE opp_id='T-UNLOCKED';
SELECT assert((SELECT status='不考虑' FROM voc_opportunity_manual WHERE opp_id='T-UNLOCKED'),
              '5b 填了理由后应可置「不考虑」');

-- ---------- 6. 安全类条目不得被机器自动合并 ----------
UPDATE voc_opportunity SET safety_flag=true WHERE opp_id='T-LOCKED';
DO $$
BEGIN
  BEGIN
    INSERT INTO voc_opp_lineage(op_type,parent_ids,child_ids,week,decided_by)
    VALUES ('MERGE', ARRAY['T-LOCKED'], ARRAY['T-UNLOCKED'], '2026-W33', 'machine');
    RAISE EXCEPTION 'ASSERT FAILED: 6a 安全类自动合并应被拒绝，但通过了';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE '%ASSERT FAILED%' THEN RAISE; END IF;
    RAISE NOTICE '  PASS  6a 安全类条目的机器自动合并被正确拒绝';
  END;
END $$;
INSERT INTO voc_opp_lineage(op_type,parent_ids,child_ids,week,decided_by)
VALUES ('MERGE', ARRAY['T-LOCKED'], ARRAY['T-UNLOCKED'], '2026-W33', 'pm-a');
SELECT assert((SELECT count(*)=1 FROM voc_opp_lineage WHERE decided_by='pm-a'),
              '6b 人工确认的合并应放行');

-- ---------- 7. 视图可用 ----------
SELECT assert((SELECT count(*)=2 FROM voc_board WHERE opp_id LIKE 'T-%'),
              '7a voc_board 应合并人机两侧');
SELECT assert((SELECT status='已完成' FROM voc_board WHERE opp_id='T-LOCKED'),
              '7b voc_board 应带出人工状态');
SELECT assert((SELECT count(*)>=0 FROM voc_inbox), '7c voc_inbox 可查询');
SELECT assert((SELECT count(*)=1 FROM voc_safety_watch WHERE opp_id='T-LOCKED'),
              '7d voc_safety_watch 应含安全类条目');
SELECT assert((SELECT count(*)=1 FROM voc_weekly_metrics), '7e voc_weekly_metrics 可查询');

ROLLBACK;
\echo '=== M0 触发器与视图验收全部通过（已回滚测试数据）==='

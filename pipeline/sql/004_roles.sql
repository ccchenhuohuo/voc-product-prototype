-- ============================================================
-- 角色矩阵（PRD v8 §10.2）
-- 机器角色写机器表；人工角色写人工表及经授权的提案裁决列
-- 口令由部署脚本从调用进程环境注入，不写在本文件
-- ============================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='voc_writer') THEN
    CREATE ROLE voc_writer LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='voc_human') THEN
    CREATE ROLE voc_human LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='voc_reader') THEN
    CREATE ROLE voc_reader LOGIN;
  END IF;
END $$;

GRANT CONNECT ON DATABASE voc TO voc_writer, voc_human, voc_reader;
GRANT USAGE   ON SCHEMA public TO voc_writer, voc_human, voc_reader;

-- ------------------------------------------------------------
-- voc_writer：手工管线作业。可写事实层与机会点层，manual 只读
-- ------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON
  voc_message, voc_evidence, voc_unclassified_evidence,
  voc_opportunity, voc_opp_evidence, voc_opp_lineage,
  voc_opp_snapshot, voc_proposal, voc_run_log, voc_tag_taxonomy
TO voc_writer;
GRANT SELECT ON voc_opportunity_manual, voc_status_log TO voc_writer;
-- 机器需读 voc_board（release_to_pm、陈旧检测都依赖它）
GRANT SELECT ON voc_board, voc_inbox, voc_safety_watch, voc_weekly_metrics TO voc_writer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO voc_writer;

-- ------------------------------------------------------------
-- voc_human：飞书回写资产 / PM 写入。只能碰人工表与提案裁决
-- ------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON voc_opportunity_manual TO voc_human;
GRANT SELECT, INSERT ON voc_status_log TO voc_human;
GRANT SELECT ON voc_proposal TO voc_human;
GRANT UPDATE (status, decided_by, decided_at) ON voc_proposal TO voc_human;
GRANT SELECT ON voc_board, voc_inbox, voc_safety_watch, voc_weekly_metrics TO voc_human;
GRANT SELECT ON voc_message, voc_evidence, voc_opp_evidence TO voc_human;  -- 下钻
GRANT USAGE, SELECT ON SEQUENCE voc_status_log_log_id_seq TO voc_human;

-- ------------------------------------------------------------
-- voc_reader：PM 只读直连 / BI
-- ------------------------------------------------------------
GRANT SELECT ON voc_board, voc_inbox, voc_safety_watch, voc_weekly_metrics TO voc_reader;
GRANT SELECT ON voc_message, voc_evidence, voc_opp_evidence, voc_opp_snapshot TO voc_reader;
-- 原始机器表：voc_reader 纯只读、无越权顾虑，直连排障需要能摸这几张表
GRANT SELECT ON voc_opportunity, voc_run_log, voc_tag_taxonomy, voc_unclassified_evidence TO voc_reader;

-- ------------------------------------------------------------
-- 显式拒绝：voc_human 不得改机器表；voc_writer 不得改人工表
-- （上面未 GRANT 即为拒绝，此处仅作断言用，便于 M0 验收脚本核对）
-- ------------------------------------------------------------
COMMENT ON TABLE voc_opportunity_manual IS
  '人工层。voc_writer 只读；机器的周度重算永不写入此表。';
COMMENT ON TABLE voc_opportunity IS
  '机器层。voc_human 无写权限；locked 条目的语义字段由 trg_voc_guard_locked 保护。';

-- 020：voc_run_log 补读权限——首页「数据新鲜度与运行健康」区块的依赖
--
-- 背景：看板应用统一以 voc_human 连库（system/app/db.py），010 的授权清单
-- 按当时的页面需求只覆盖了渲染涉及的表。首页新增的 HOME_FRESHNESS 要读
-- voc_run_log（最近运行的状态/token/费用），真库 EXPLAIN 体检报
-- InsufficientPrivilege——离线单测发现不了这一类缺口，2026-08-17 实测抓到。
--
-- 只授 SELECT：运行日志由管道写入（voc_writer），看板与只读角色永远不写它。
BEGIN;

GRANT SELECT ON voc_run_log TO voc_human, voc_reader;

-- 自检：授权后 voc_human 必须能规划该查询。
DO $$
BEGIN
  IF NOT has_table_privilege('voc_human', 'voc_run_log', 'SELECT') THEN
    RAISE EXCEPTION 'voc_human 仍无 voc_run_log 读权限';
  END IF;
  IF NOT has_table_privilege('voc_reader', 'voc_run_log', 'SELECT') THEN
    RAISE EXCEPTION 'voc_reader 仍无 voc_run_log 读权限';
  END IF;
END $$;

COMMIT;

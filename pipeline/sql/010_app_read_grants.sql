-- ============================================================
-- 给看板应用补读权限：voc_human 需要读 voc_opportunity
-- 以 voc_admin 执行。
--
-- 背景：004 建角色时 voc_human 只拿了「人工表 + 证据层」的读权，
-- 因为当时还没有面向 PM 的应用。看板要渲染问题标题、n_eff、
-- scope、opp_type、rank_score、released_at，全部在 voc_opportunity 上，
-- 缺这一张读权会让所有页面 500。
--
-- 只加 SELECT。安全边界是「机器表不可写」，读权不触碰这条边界，
-- 写权仍然只有两张 manual 表——文件末尾有断言守着。
-- ============================================================

GRANT SELECT ON voc_opportunity TO voc_human;

-- 应用当前引用的全部 8 张表，逐张确认 voc_human 可读，缺一即报错。
DO $$
DECLARE missing text;
BEGIN
  SELECT string_agg(t, ', ') INTO missing
    FROM unnest(ARRAY['voc_message','voc_evidence','voc_opp_evidence','voc_opportunity',
                      'voc_spu','voc_spu_issue','voc_spu_issue_manual','voc_opportunity_manual']) AS t
   WHERE NOT has_table_privilege('voc_human', t, 'SELECT');
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION '看板应用缺少读权限：%', missing;
  END IF;
END $$;

-- 写边界不许被这次授权放宽：机器表上 voc_human 一个写权都不能有。
DO $$
DECLARE leaked text;
BEGIN
  SELECT string_agg(t, ', ') INTO leaked
    FROM unnest(ARRAY['voc_message','voc_evidence','voc_opp_evidence','voc_opportunity']) AS t
   WHERE has_table_privilege('voc_human', t, 'INSERT')
      OR has_table_privilege('voc_human', t, 'UPDATE')
      OR has_table_privilege('voc_human', t, 'DELETE');
  IF leaked IS NOT NULL THEN
    RAISE EXCEPTION '写边界被破坏，voc_human 竟可写机器表：%', leaked;
  END IF;
END $$;

SELECT 'voc_human 读权已补齐，写边界完好' AS 结果;

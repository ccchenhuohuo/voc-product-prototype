-- 018：把共性度 n_eff 的取数口径对齐到 SPU 问题层
--
-- 背景：015 解耦分类契约时，把 voc_spu_issue 的 `m.src_line = '电商'` 条件
-- 去掉了——「老品迭代」的语义已改为「任一证据挂 SPU」，社媒挂 SPU 的证据
-- 同样路由到产品卡。但 n_eff/scope 的计算写在 009 定义的
-- voc_refresh_spu_layer() 函数体里，015 没有一并覆盖，至今仍只统计电商。
--
-- 后果（2026-08-17 全量刷新后实测）：
--   * 338 个老品迭代机会点有 SPU 证据，其中只有 280 个含电商证据
--     -> 58 个机会点 scope 恒为空，在战略视图里既无分档也排在末尾；
--   * 另有 112 个机会点两套口径分档不同（单品->多品 23 个、
--     空->品线级 16 个、多品->品线级 16 个……）。
-- 也就是说卡片页会显示某问题挂在多个 SPU 上，战略视图却判它「单品」，
-- 同一份证据在相邻两页给出互相矛盾的结论。
--
-- 本迁移只改口径，不改阈值与 NULL 语义：
--   * 去掉 src_line 限制，与 voc_spu_issue 的分子基础一致；
--   * 仍不套用「成卡 >=2」阈值——009 的原意就是让共性度比卡片层更宽，
--     避免单证据产品被静默丢掉而低估扩散，这一点保持不变；
--   * 仍不按 opp_type 过滤：n_eff 是机会点自身的证据事实，
--     战略视图那侧已自行按 opp_type='老品迭代' 取数。
BEGIN;

CREATE OR REPLACE FUNCTION voc_refresh_spu_layer() RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW public.voc_spu;
  REFRESH MATERIALIZED VIEW public.voc_spu_issue;

  -- 共性度使用未套用「成卡 >=2」阈值的全量挂靠，避免单证据产品被静默
  -- 丢掉而低估扩散；同一消息内重复 SPU 先去重，多 SPU 则各自计一次归属。
  -- 来源口径与 voc_spu_issue 对齐：只要证据挂了 SPU 就计入，不论渠道。
  WITH attached AS (
    SELECT DISTINCT
           oe.opp_id,
           oe.message_id,
           oe.seq,
           NULLIF(btrim(s.spu), '') AS spu
      FROM public.voc_opp_evidence oe
      JOIN public.voc_message m USING (message_id)
     CROSS JOIN LATERAL unnest(m.spu) AS s(spu)
     WHERE cardinality(m.spu) > 0
       AND NULLIF(btrim(s.spu), '') IS NOT NULL
  ), per_spu AS (
    SELECT opp_id, spu, count(*)::numeric AS evi_count
      FROM attached
     GROUP BY opp_id, spu
  ), spread AS (
    SELECT opp_id,
           power(sum(evi_count), 2)
             / NULLIF(sum(evi_count * evi_count), 0) AS n_eff
      FROM per_spu
     GROUP BY opp_id
  ), calculated AS (
    SELECT o.opp_id,
           s.n_eff,
           CASE
             WHEN s.n_eff IS NULL THEN NULL
             WHEN s.n_eff < 1.5 THEN '单品'
             WHEN s.n_eff < 3 THEN '多品'
             ELSE '品线级'
           END AS scope
      FROM public.voc_opportunity o
      LEFT JOIN spread s USING (opp_id)
  )
  UPDATE public.voc_opportunity o
     SET n_eff = c.n_eff,
         scope = c.scope
    FROM calculated c
   WHERE o.opp_id = c.opp_id
     AND (o.n_eff IS DISTINCT FROM c.n_eff
          OR o.scope IS DISTINCT FROM c.scope);
END;
$$;

REVOKE ALL ON FUNCTION voc_refresh_spu_layer() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION voc_refresh_spu_layer() TO voc_writer;

SELECT voc_refresh_spu_layer();

-- 只读自检：三条断言分别守住 NULL 语义、口径一致性和阈值不变。
DO $$
DECLARE
  null_pair_errors bigint;
  card_without_scope bigint;
  issue_threshold_ok boolean;
BEGIN
  SELECT count(*) FILTER (WHERE (n_eff IS NULL) <> (scope IS NULL))
    INTO null_pair_errors FROM voc_opportunity;
  IF null_pair_errors <> 0 THEN
    RAISE EXCEPTION 'n_eff 与 scope 的 NULL 语义脱钩：% 行', null_pair_errors;
  END IF;

  -- 上了 SPU 卡片层却没有共性度分档，正是本迁移要消灭的矛盾态。
  SELECT count(DISTINCT i.opp_id) INTO card_without_scope
    FROM voc_spu_issue i
    JOIN voc_opportunity o USING (opp_id)
   WHERE o.scope IS NULL;
  IF card_without_scope <> 0 THEN
    RAISE EXCEPTION '有 % 个机会点进了 SPU 卡片层却没有 scope', card_without_scope;
  END IF;

  SELECT COALESCE(bool_and(evi_count >= 2), true) INTO issue_threshold_ok
    FROM voc_spu_issue;
  IF NOT issue_threshold_ok THEN
    RAISE EXCEPTION 'voc_spu_issue 的成卡阈值被破坏';
  END IF;
END $$;

COMMIT;

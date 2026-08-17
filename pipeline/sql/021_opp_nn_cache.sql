-- 021：机会点最近邻缓存表——首页「语义相似度分布」的数据源
--
-- 背景：首页原实现在请求时对每个机会点做同生命周期 LATERAL KNN。
-- EXPLAIN 计划合法，但真库实测 103 秒（预算 200ms）：LATERAL 里带
-- classification_state / merged_into / opp_type 过滤，HNSW 索引（ix_opp_vec）
-- 无法命中，退化为逐行全表向量扫描（约 4k × 4k 次 1024 维距离计算）。
-- 属任务书预设的回退场景：结果缓存进表，由管道收尾刷新，页面只读缓存。
--
-- 刷新频率 = 机会点层重建频率（收尾一次刷一次）。页面读到空表时显示
-- 「尚未计算」空状态，不报错。
BEGIN;

CREATE TABLE IF NOT EXISTS voc_opp_nn (
  opp_id         text PRIMARY KEY,
  lifecycle      text NOT NULL,
  title          text,
  neighbor_id    text NOT NULL,
  neighbor_title text,
  distance       double precision NOT NULL,
  refreshed_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE voc_opp_nn IS
  '每个有效机会点在同生命周期内的最近邻（余弦距离）。收尾刷新，首页只读。';

-- 与 voc_refresh_spu_layer 同一权限模式：SECURITY DEFINER 薄函数收窄权限，
-- 机器角色只拿执行权，不拿表所有权。
CREATE OR REPLACE FUNCTION voc_refresh_opp_nn() RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  -- 批量重算是分钟级操作，只应在收尾（机会点层刚重建完）调用。
  -- 全删全插保持幂等；TRUNCATE 不留死元组。
  TRUNCATE public.voc_opp_nn;

  WITH active AS (
    SELECT o.opp_id, o.opp_type AS lifecycle, o.title, o.mode_vec
      FROM public.voc_opportunity o
     WHERE o.classification_state = '确定'
       AND o.merged_into IS NULL
       AND o.mode_vec IS NOT NULL
       AND o.opp_type IN ('老品迭代', '新品创新')
  )
  INSERT INTO public.voc_opp_nn
         (opp_id, lifecycle, title, neighbor_id, neighbor_title, distance)
  SELECT a.opp_id, a.lifecycle, a.title,
         b.opp_id, b.title,
         (b.mode_vec <=> a.mode_vec)::double precision
    FROM active a
   CROSS JOIN LATERAL (
     SELECT x.opp_id, x.title, x.mode_vec
       FROM active x
      WHERE x.lifecycle = a.lifecycle
        AND x.opp_id <> a.opp_id
      ORDER BY x.mode_vec <=> a.mode_vec
      LIMIT 1
   ) b;
END;
$$;

REVOKE ALL ON FUNCTION voc_refresh_opp_nn() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION voc_refresh_opp_nn() TO voc_writer;

GRANT SELECT ON voc_opp_nn TO voc_writer, voc_human, voc_reader;

COMMIT;

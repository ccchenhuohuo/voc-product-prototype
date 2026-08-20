-- 025：SPU 容器扩展到社媒事实/继承挂载，并显式标记是否有电商数据。
-- voc_spu_issue 口径与结构不变。
BEGIN;

DROP MATERIALIZED VIEW IF EXISTS voc_spu;

CREATE MATERIALIZED VIEW voc_spu AS
WITH all_spu AS (
  SELECT DISTINCT NULLIF(btrim(x.spu), '') AS spu
    FROM voc_message m
   CROSS JOIN LATERAL unnest(
     CASE
       WHEN m.src_line = '社媒' THEN
         COALESCE(m.spu, ARRAY[]::text[])
         || COALESCE(m.spu_inherited, ARRAY[]::text[])
       ELSE COALESCE(m.spu, ARRAY[]::text[])
     END
   ) AS x(spu)
   WHERE NULLIF(btrim(x.spu), '') IS NOT NULL
), base_message AS (
  -- 电商聚合完整保留 009 口径：只展开云听事实 spu，不读推断列。
  SELECT DISTINCT
         m.message_id,
         NULLIF(btrim(s.spu), '') AS spu,
         m.product_name,
         m.sku,
         m.category,
         m.prod_line,
         m.product_grade,
         m.launch_period,
         m.star,
         m.publish_time
    FROM voc_message m
   CROSS JOIN LATERAL unnest(m.spu) AS s(spu)
   WHERE m.src_line = '电商'
     AND cardinality(m.spu) > 0
     AND NULLIF(btrim(s.spu), '') IS NOT NULL
), message_agg AS (
  SELECT spu,
         array_agg(DISTINCT product_name ORDER BY product_name)
           FILTER (WHERE product_name IS NOT NULL) AS product_names,
         mode() WITHIN GROUP (ORDER BY category)
           FILTER (WHERE category IS NOT NULL) AS category,
         mode() WITHIN GROUP (ORDER BY prod_line)
           FILTER (WHERE prod_line IS NOT NULL) AS prod_line,
         (array_agg(product_grade ORDER BY
            CASE regexp_replace(product_grade, '级$', '')
              WHEN 'PS' THEN 1 WHEN 'S' THEN 2 WHEN 'A' THEN 3
              WHEN 'B' THEN 4 WHEN 'C' THEN 5 WHEN 'D' THEN 6
              WHEN '其他' THEN 7 ELSE 8
            END,
            product_grade)
          FILTER (WHERE product_grade IS NOT NULL))[1] AS grade,
         min(launch_period) FILTER (WHERE launch_period IS NOT NULL) AS launch_period,
         count(DISTINCT message_id) AS message_count,
         round(avg(star), 2) AS avg_star,
         min(to_char(publish_time, 'IYYY-"W"IW'))
           FILTER (WHERE publish_time IS NOT NULL) AS first_publish_week,
         max(to_char(publish_time, 'IYYY-"W"IW'))
           FILTER (WHERE publish_time IS NOT NULL) AS last_publish_week
    FROM base_message
   GROUP BY spu
), sku_value AS (
  SELECT DISTINCT b.spu, NULLIF(btrim(x.sku), '') AS sku
    FROM base_message b
   CROSS JOIN LATERAL unnest(COALESCE(b.sku, ARRAY[]::text[])) AS x(sku)
   WHERE NULLIF(btrim(x.sku), '') IS NOT NULL
), sku_agg AS (
  SELECT spu, array_agg(sku ORDER BY sku) AS skus
    FROM sku_value
   GROUP BY spu
), message_spu AS (
  SELECT DISTINCT message_id, spu FROM base_message
), evidence_agg AS (
  SELECT ms.spu,
         count(*) FILTER (WHERE e.sentiment = '负面') AS negative_evi_count,
         count(*) FILTER (WHERE e.sentiment = '正面') AS positive_evi_count
    FROM message_spu ms
    JOIN voc_evidence e USING (message_id)
   GROUP BY ms.spu
), ecommerce AS (
  SELECT m.spu,
         m.product_names,
         s.skus,
         m.category,
         m.prod_line,
         m.grade,
         m.launch_period,
         m.message_count,
         COALESCE(e.negative_evi_count, 0) AS negative_evi_count,
         COALESCE(e.positive_evi_count, 0) AS positive_evi_count,
         m.avg_star,
         m.first_publish_week,
         m.last_publish_week
    FROM message_agg m
    LEFT JOIN sku_agg s USING (spu)
    LEFT JOIN evidence_agg e USING (spu)
)
SELECT a.spu,
       e.product_names,
       e.skus,
       e.category,
       e.prod_line,
       e.grade,
       e.launch_period,
       e.message_count,
       e.negative_evi_count,
       e.positive_evi_count,
       e.avg_star,
       e.first_publish_week,
       e.last_publish_week,
       (e.spu IS NOT NULL) AS has_ec
  FROM all_spu a
  LEFT JOIN ecommerce e USING (spu);

CREATE UNIQUE INDEX ux_voc_spu_spu ON voc_spu (spu);

GRANT SELECT ON voc_spu TO voc_writer, voc_human, voc_reader;

COMMENT ON COLUMN voc_spu.has_ec IS
  'true=有电商消息并保留 009 统计口径；false=仅社媒挂载，电商属性均为 NULL。';

COMMIT;

"""VOC 看板的全部 SQL。路由中不得散落查询语句。"""


# 侧栏读数与 base.html 的 nav_counts 键一一对应。「待复议」不是
# 数据库状态，而是「考虑中 + 曾复活」的展示态。
SHELL_COUNTS = """
SELECT
  (SELECT count(DISTINCT i.spu)::int
     FROM voc_spu_issue i
     JOIN voc_opportunity o ON o.opp_id = i.opp_id
    WHERE o.merged_into IS NULL) AS iter,
  (SELECT count(*)::int
    FROM voc_opportunity o
    WHERE o.opp_type = '新品创新'
      AND o.classification_state = '确定'
      AND o.merged_into IS NULL) AS inno,
  (SELECT count(*)::int
    FROM voc_opportunity o
    WHERE o.scope = '品线级'
      AND o.classification_state = '确定'
      AND o.merged_into IS NULL) AS strategy,
  (SELECT count(*)::int FROM voc_spu s) AS search,
  (SELECT count(*)::int
     FROM voc_spu_issue_manual m
     JOIN voc_spu_issue i
       ON i.spu = m.spu AND i.opp_id = m.opp_id
     JOIN voc_opportunity o ON o.opp_id = m.opp_id
    WHERE m.status = '考虑中'
      AND m.revived_at IS NOT NULL
      AND o.merged_into IS NULL) AS revived
"""


# 首页证据漏斗同时返回主漏斗与「未进入产品负面原料」的 Top 8 标签。
# 内容标签分支与 pipeline.voc_analytics.db.generation_pool 保持同一组标签及
# 文本/品牌门槛；它和产品负面分支是两个业务入口，因此这里只比较规模，
# 不把两者误写成集合包含关系。
HOME_EVIDENCE_FUNNEL = """
WITH stage_counts AS (
  SELECT
    (SELECT count(*)::bigint FROM voc_evidence) AS fact_total,
    (SELECT count(*)::bigint
       FROM voc_evidence e
      WHERE e.is_product
        AND e.sentiment = '负面'
        AND NULLIF(btrim(e.snippet), '') IS NOT NULL) AS product_negative,
    (SELECT count(*)::bigint
       FROM voc_evidence e
       JOIN voc_message m ON m.message_id = e.message_id
       JOIN voc_source_policy p ON p.src_line = m.src_line
      WHERE NOT p.requires_spu
        AND COALESCE(cardinality(m.spu), 0) = 0
        AND (
          (COALESCE(m.content_type, ARRAY[]::text[])
             && ARRAY['用户咨询', '其他']::text[]
           AND COALESCE(NULLIF(btrim(e.snippet), ''),
                        NULLIF(btrim(m.content), '')) IS NOT NULL)
          OR
          (COALESCE(m.content_type, ARRAY[]::text[])
             && ARRAY['产品评测', '竞品拉踩']::text[]
           AND COALESCE(NULLIF(btrim(e.snippet), ''),
                        NULLIF(btrim(m.content), '')) IS NOT NULL
           AND COALESCE(cardinality(m.brands), 0) >= 2)
        )) AS innovation_material,
    (SELECT count(*)::bigint
       FROM (
         SELECT DISTINCT oe.message_id, oe.seq
           FROM voc_opp_evidence oe
       ) attached_evidence) AS attached,
    (SELECT count(*)::bigint
       FROM (
         SELECT DISTINCT oe.message_id, oe.seq
           FROM voc_spu_issue i
           JOIN voc_opp_evidence oe ON oe.opp_id = i.opp_id
           JOIN voc_message m ON m.message_id = oe.message_id
          WHERE i.spu = ANY(COALESCE(m.spu, ARRAY[]::text[]))
       ) card_evidence) AS in_spu_cards
), stages AS (
  SELECT v.stage_order, v.stage_key, v.label, v.item_count
    FROM stage_counts s
   CROSS JOIN LATERAL (
     VALUES
       (1, 'fact_total'::text, '事实层证据总数'::text, s.fact_total),
       (2, 'product_negative', '产品负面且有原声片段', s.product_negative),
       (3, 'innovation_material', '需求缺口 / 竞品对标原料', s.innovation_material),
       (4, 'attached', '已挂靠到机会点', s.attached),
       (5, 'spu_cards', '进入 SPU 卡片层', s.in_spu_cards)
   ) v(stage_order, stage_key, label, item_count)
), stage_rates AS (
  SELECT s.*,
         lag(s.item_count) OVER (ORDER BY s.stage_order) AS previous_count
    FROM stages s
), loss_tags AS (
  SELECT COALESCE(NULLIF(btrim(e.tag), ''), '未标注') AS loss_tag,
         count(*)::bigint AS item_count
    FROM voc_evidence e
   WHERE e.sentiment = '负面'
     AND e.is_product IS NOT TRUE
   GROUP BY COALESCE(NULLIF(btrim(e.tag), ''), '未标注')
   ORDER BY item_count DESC, loss_tag
   LIMIT 8
)
SELECT 'stage'::text AS row_type,
       s.stage_order, s.stage_key, s.label,
       s.item_count, s.previous_count,
       round(s.item_count::numeric / NULLIF(s.previous_count, 0) * 100, 1)
         AS retention_pct,
       NULL::text AS loss_tag
  FROM stage_rates s
UNION ALL
SELECT 'loss_tag'::text, NULL::int, NULL::text, NULL::text,
       l.item_count, NULL::bigint, NULL::numeric, l.loss_tag
  FROM loss_tags l
ORDER BY row_type DESC, stage_order NULLS LAST, item_count DESC, loss_tag
"""


# 机会点证据分布使用关系层实际行数，不信任可能漂移的 evi_total。
# 任务书的「6-10 / 10 条以上」在 10 上重叠；这里采用互斥分档并把末档
# 明确为 11 条以上。
HOME_EVIDENCE_PER_OPP = """
WITH lifecycles(lifecycle_order, lifecycle) AS (
  VALUES (1, '老品迭代'::text), (2, '新品创新'::text)
), bins(bucket_order, bucket_key, bucket_label) AS (
  VALUES (1, 'one'::text, '1 条'::text),
         (2, 'two'::text, '2 条'::text),
         (3, 'three_five'::text, '3–5 条'::text),
         (4, 'six_ten'::text, '6–10 条'::text),
         (5, 'eleven_plus'::text, '11 条以上'::text)
), evidence_counts AS (
  SELECT oe.opp_id, count(*)::bigint AS evidence_count
    FROM voc_opp_evidence oe
   GROUP BY oe.opp_id
), active AS (
  SELECT o.opp_type AS lifecycle,
         COALESCE(e.evidence_count, 0)::bigint AS evidence_count
    FROM voc_opportunity o
    LEFT JOIN evidence_counts e ON e.opp_id = o.opp_id
   WHERE o.classification_state = '确定'
     AND o.merged_into IS NULL
     AND o.opp_type IN ('老品迭代', '新品创新')
), bucketed AS (
  SELECT a.lifecycle, a.evidence_count,
         CASE
           WHEN a.evidence_count = 1 THEN 'one'
           WHEN a.evidence_count = 2 THEN 'two'
           WHEN a.evidence_count BETWEEN 3 AND 5 THEN 'three_five'
           WHEN a.evidence_count BETWEEN 6 AND 10 THEN 'six_ten'
           WHEN a.evidence_count > 10 THEN 'eleven_plus'
         END AS bucket_key
    FROM active a
), aggregated AS (
  SELECT b.lifecycle, b.bucket_key,
         count(*)::int AS opportunity_count,
         COALESCE(sum(b.evidence_count), 0)::bigint AS evidence_count
    FROM bucketed b
   WHERE b.bucket_key IS NOT NULL
   GROUP BY b.lifecycle, b.bucket_key
), result AS (
  SELECT l.lifecycle_order, l.lifecycle,
         b.bucket_order, b.bucket_key, b.bucket_label,
         COALESCE(a.opportunity_count, 0)::int AS opportunity_count,
         COALESCE(a.evidence_count, 0)::bigint AS evidence_count
    FROM lifecycles l
   CROSS JOIN bins b
    LEFT JOIN aggregated a
      ON a.lifecycle = l.lifecycle AND a.bucket_key = b.bucket_key
)
SELECT r.*,
       sum(r.opportunity_count) OVER (PARTITION BY r.lifecycle)::int
         AS lifecycle_total,
       round(r.opportunity_count::numeric
             / NULLIF(sum(r.opportunity_count) OVER (
                 PARTITION BY r.lifecycle
               ), 0) * 100, 1) AS opportunity_pct
  FROM result r
 ORDER BY r.lifecycle_order, r.bucket_order
"""


# 001_schema.sql 已提供 ix_opp_vec HNSW(vector_cosine_ops)。两个生命周期拆成
# 带字面量过滤的 LATERAL KNN 分支，让 PostgreSQL 能以该索引逐点取 LIMIT 1，
# 避免 4,000 x 4,000 的两两比较。最相似对在 KNN 结果上去重后全局取 10。
HOME_SIMILARITY = """
-- 最近邻从 voc_opp_nn 缓存表读取（021 迁移），由收尾调用 voc_refresh_opp_nn()
-- 刷新。原实现是请求时逐机会点 LATERAL KNN：EXPLAIN 计划合法，但过滤条件让
-- HNSW 索引失效，真库实测 103 秒（预算 200ms）。缓存为空 = 尚未计算过，
-- bucket 行的 opportunity_count 全为 0，模板按空状态渲染。
WITH active AS NOT MATERIALIZED (
  SELECT o.opp_id, o.opp_type AS lifecycle
    FROM voc_opportunity o
   WHERE o.classification_state = '确定'
     AND o.merged_into IS NULL
     AND o.mode_vec IS NOT NULL
     AND o.opp_type IN ('老品迭代', '新品创新')
), nearest AS (
  SELECT nn.lifecycle, nn.opp_id, nn.title,
         nn.neighbor_id, nn.neighbor_title, nn.distance
    FROM voc_opp_nn nn
    -- 只保留双方仍有效的行：缓存刷新落后于机会点层重建时，
    -- 指向已删/已合并机会点的过期行不得进入统计。
    JOIN active a ON a.opp_id = nn.opp_id
    JOIN active b ON b.opp_id = nn.neighbor_id
), bucket_defs(bucket_order, bucket_key, bucket_label) AS (
  VALUES (1, 'near_synonym'::text, '< 0.05 · 近乎同义'::text),
         (2, 'highly_similar'::text, '0.05–0.10 · 高度相似'::text),
         (3, 'similar'::text, '0.10–0.15 · 相似'::text),
         (4, 'related'::text, '0.15–0.30 · 相关'::text),
         (5, 'unrelated'::text, '≥ 0.30 · 基本无关'::text)
), lifecycle_defs(lifecycle_order, lifecycle) AS (
  VALUES (1, '老品迭代'::text), (2, '新品创新'::text)
), bucketed AS (
  SELECT n.*,
         CASE
           WHEN n.distance < 0.05 THEN 'near_synonym'
           WHEN n.distance < 0.10 THEN 'highly_similar'
           WHEN n.distance < 0.15 THEN 'similar'
           WHEN n.distance < 0.30 THEN 'related'
           ELSE 'unrelated'
         END AS bucket_key
    FROM nearest n
), bucket_counts AS (
  SELECT b.lifecycle, b.bucket_key, count(*)::int AS opportunity_count
    FROM bucketed b
   GROUP BY b.lifecycle, b.bucket_key
), vector_counts AS (
  SELECT a.lifecycle, count(*)::int AS vector_count
    FROM active a
   GROUP BY a.lifecycle
), normalized_pairs AS (
  SELECT n.lifecycle,
         LEAST(n.opp_id, n.neighbor_id) AS opp_id_a,
         CASE WHEN n.opp_id <= n.neighbor_id
              THEN n.title ELSE n.neighbor_title END AS title_a,
         GREATEST(n.opp_id, n.neighbor_id) AS opp_id_b,
         CASE WHEN n.opp_id <= n.neighbor_id
              THEN n.neighbor_title ELSE n.title END AS title_b,
         n.distance
    FROM nearest n
), unique_pairs AS (
  SELECT DISTINCT ON (p.lifecycle, p.opp_id_a, p.opp_id_b)
         p.lifecycle, p.opp_id_a, p.title_a,
         p.opp_id_b, p.title_b, p.distance
    FROM normalized_pairs p
   ORDER BY p.lifecycle, p.opp_id_a, p.opp_id_b, p.distance
), top_pairs AS (
  SELECT p.*,
         row_number() OVER (
           ORDER BY p.distance, p.lifecycle, p.opp_id_a, p.opp_id_b
         )::int AS pair_order
    FROM unique_pairs p
   ORDER BY p.distance, p.lifecycle, p.opp_id_a, p.opp_id_b
   LIMIT 10
), output AS (
  SELECT 'bucket'::text AS row_type,
         l.lifecycle_order, l.lifecycle,
         d.bucket_order, d.bucket_key, d.bucket_label,
         COALESCE(c.opportunity_count, 0)::int AS opportunity_count,
         COALESCE(v.vector_count, 0)::int AS vector_count,
         NULL::text AS opp_id_a, NULL::text AS title_a, NULL::text AS spu_a,
         NULL::text AS opp_id_b, NULL::text AS title_b, NULL::text AS spu_b,
         NULL::double precision AS distance, NULL::int AS pair_order
    FROM lifecycle_defs l
   CROSS JOIN bucket_defs d
    LEFT JOIN bucket_counts c
      ON c.lifecycle = l.lifecycle AND c.bucket_key = d.bucket_key
    LEFT JOIN vector_counts v ON v.lifecycle = l.lifecycle
  UNION ALL
  SELECT 'pair'::text, NULL::int, p.lifecycle,
         NULL::int, NULL::text, NULL::text,
         NULL::int, NULL::int,
         p.opp_id_a, p.title_a, ia.spu,
         p.opp_id_b, p.title_b, ib.spu,
         p.distance, p.pair_order
    FROM top_pairs p
    LEFT JOIN LATERAL (
      SELECT i.spu
        FROM voc_spu_issue i
       WHERE i.opp_id = p.opp_id_a
       ORDER BY i.evi_count DESC, i.spu
       LIMIT 1
    ) ia ON TRUE
    LEFT JOIN LATERAL (
      SELECT i.spu
        FROM voc_spu_issue i
       WHERE i.opp_id = p.opp_id_b
       ORDER BY i.evi_count DESC, i.spu
       LIMIT 1
    ) ib ON TRUE
)
SELECT *
  FROM output
 ORDER BY CASE row_type WHEN 'bucket' THEN 1 ELSE 2 END,
          lifecycle_order NULLS LAST, bucket_order NULLS LAST,
          pair_order NULLS LAST
"""


HOME_ISSUE_STATUS = """
WITH statuses(status_order, status) AS (
  VALUES (1, '考虑中'::text),
         (2, '在跟进'::text),
         (3, '项目中'::text),
         (4, '已完成'::text),
         (5, '不考虑'::text),
         (6, '未表态'::text)
), status_counts AS (
  SELECT CASE WHEN m.opp_id IS NULL THEN '未表态' ELSE m.status END AS status,
         count(*)::int AS issue_count
    FROM voc_spu_issue i
    LEFT JOIN voc_spu_issue_manual m
      ON m.spu = i.spu AND m.opp_id = i.opp_id
   GROUP BY CASE WHEN m.opp_id IS NULL THEN '未表态' ELSE m.status END
), result AS (
  SELECT s.status_order, s.status,
         COALESCE(c.issue_count, 0)::int AS issue_count
    FROM statuses s
    LEFT JOIN status_counts c ON c.status = s.status
)
SELECT r.*,
       sum(r.issue_count) OVER ()::int AS issue_total,
       round(r.issue_count::numeric
             / NULLIF(sum(r.issue_count) OVER (), 0) * 100, 1) AS issue_pct
  FROM result r
 ORDER BY r.status_order
"""


# 生成池条件与 pipeline.voc_analytics.db.generation_pool 保持一致；最终机会点
# 只按生成池证据的真实关系归因到来源/语种，避免拿消息量冒充机会产出。
HOME_COVERAGE = """
WITH message_counts AS (
  SELECT m.src_line,
         COALESCE(NULLIF(btrim(m.lang), ''), '未标注') AS lang,
         count(*)::bigint AS message_count
    FROM voc_message m
   GROUP BY m.src_line, COALESCE(NULLIF(btrim(m.lang), ''), '未标注')
), pool_evidence AS MATERIALIZED (
  SELECT e.message_id, e.seq, m.src_line,
         COALESCE(NULLIF(btrim(m.lang), ''), '未标注') AS lang
    FROM voc_evidence e
    JOIN voc_message m ON m.message_id = e.message_id
    JOIN voc_source_policy p ON p.src_line = m.src_line
   WHERE (
       (e.is_product
        AND e.sentiment = '负面'
        AND NULLIF(btrim(e.snippet), '') IS NOT NULL)
       OR
       (COALESCE(m.content_type, ARRAY[]::text[])
          && ARRAY['用户使用体验']::text[]
        AND e.sentiment = '负面'
        AND NULLIF(btrim(e.snippet), '') IS NOT NULL)
       OR
       (COALESCE(m.content_type, ARRAY[]::text[])
          && ARRAY['产品评测', '竞品拉踩']::text[]
        AND COALESCE(NULLIF(btrim(e.snippet), ''),
                     NULLIF(btrim(m.content), '')) IS NOT NULL
        AND COALESCE(cardinality(m.brands), 0) >= 2)
       OR
       (COALESCE(m.content_type, ARRAY[]::text[])
          && ARRAY['用户咨询', '其他']::text[]
        AND COALESCE(NULLIF(btrim(e.snippet), ''),
                     NULLIF(btrim(m.content), '')) IS NOT NULL)
     )
     AND (NOT p.requires_spu OR COALESCE(cardinality(m.spu), 0) > 0)
), pool_counts AS (
  SELECT p.src_line, p.lang, count(*)::bigint AS evidence_count
    FROM pool_evidence p
   GROUP BY p.src_line, p.lang
), opportunity_counts AS (
  SELECT p.src_line, p.lang,
         count(DISTINCT o.opp_id)::int AS opportunity_count
    FROM pool_evidence p
    JOIN voc_opp_evidence oe
      ON oe.message_id = p.message_id AND oe.seq = p.seq
    JOIN voc_opportunity o ON o.opp_id = oe.opp_id
   WHERE o.classification_state = '确定'
     AND o.merged_into IS NULL
   GROUP BY p.src_line, p.lang
)
SELECT m.src_line, m.lang, m.message_count,
       COALESCE(p.evidence_count, 0)::bigint AS evidence_count,
       COALESCE(o.opportunity_count, 0)::int AS opportunity_count,
       round(COALESCE(o.opportunity_count, 0)::numeric
             / NULLIF(COALESCE(p.evidence_count, 0), 0) * 100, 2)
         AS conversion_pct
  FROM message_counts m
  LEFT JOIN pool_counts p
    ON p.src_line = m.src_line AND p.lang = m.lang
  LEFT JOIN opportunity_counts o
    ON o.src_line = m.src_line AND o.lang = m.lang
 ORDER BY m.src_line, COALESCE(p.evidence_count, 0) DESC, m.lang
"""


HOME_FRESHNESS = """
WITH freshness AS (
  SELECT
    (SELECT max(m.publish_time) FROM voc_message m) AS latest_publish_time,
    (SELECT count(DISTINCT date_trunc('week', m.publish_time))::int
       FROM voc_message m
      WHERE m.publish_time IS NOT NULL) AS coverage_weeks,
    (SELECT count(*)::int FROM voc_spu_issue) AS spu_issue_total,
    (SELECT count(*)::int
       FROM voc_spu_issue i
       LEFT JOIN voc_opportunity o ON o.opp_id = i.opp_id
      WHERE o.opp_id IS NULL OR o.merged_into IS NOT NULL) AS dangling_count
), recent_runs AS (
  SELECT r.run_id, r.stage, r.status, r.started_at, r.finished_at,
         r.llm_calls, r.llm_tokens,
         r.metrics -> 'cost' ->> 'cny' AS cost_cny,
         row_number() OVER (
           ORDER BY r.started_at DESC NULLS LAST,
                    r.finished_at DESC NULLS LAST, r.run_id DESC
         )::int AS run_order
    FROM voc_run_log r
   ORDER BY r.started_at DESC NULLS LAST,
            r.finished_at DESC NULLS LAST, r.run_id DESC
   LIMIT 5
), output AS (
  SELECT 'freshness'::text AS row_type,
         f.latest_publish_time, f.coverage_weeks,
         f.spu_issue_total, f.dangling_count,
         round(f.dangling_count::numeric
               / NULLIF(f.spu_issue_total, 0) * 100, 1) AS dangling_pct,
         NULL::text AS run_id, NULL::text AS stage, NULL::text AS status,
         NULL::timestamptz AS started_at, NULL::timestamptz AS finished_at,
         NULL::int AS llm_calls, NULL::bigint AS llm_tokens,
         NULL::text AS cost_cny, NULL::int AS run_order
    FROM freshness f
  UNION ALL
  SELECT 'run'::text, NULL::timestamptz, NULL::int,
         NULL::int, NULL::int, NULL::numeric,
         r.run_id, r.stage, r.status, r.started_at, r.finished_at,
         r.llm_calls, r.llm_tokens, r.cost_cny, r.run_order
    FROM recent_runs r
)
SELECT *
  FROM output
 ORDER BY CASE row_type WHEN 'freshness' THEN 1 ELSE 2 END,
          run_order NULLS FIRST
"""


# 两张 SPU 表共用同一套业务排序。定级先去掉可选的「级」后缀，
# 再按 PS › S › A › B › C › D › 其他排序，不能依赖字母序。
def _spu_order(alias: str) -> str:
    grade_order = f"""CASE regexp_replace(COALESCE({alias}.grade, ''), '级$', '')
            WHEN 'PS' THEN 1
            WHEN 'S' THEN 2
            WHEN 'A' THEN 3
            WHEN 'B' THEN 4
            WHEN 'C' THEN 5
            WHEN 'D' THEN 6
            ELSE 7
          END"""
    # ELSE 7 不能省：库里有 5 个 SPU 定级就是「其他」。缺了它这些行落成 NULL，
    # 而升降两个分支都写了 NULLS LAST，结果是不论点升序还是降序它们都垫底。
    return f"""
 ORDER BY CASE WHEN ordering.sort_key = 'grade'
                        AND ordering.sort_dir = 'asc'
                    THEN {grade_order} ELSE NULL END ASC NULLS LAST,
          CASE WHEN ordering.sort_key = 'grade'
                        AND ordering.sort_dir = 'desc'
                    THEN {grade_order} ELSE NULL END DESC NULLS LAST,
          CASE WHEN ordering.sort_key = 'ratio'
                        AND ordering.sort_dir = 'asc'
                    THEN {alias}.wilson_score ELSE NULL END ASC NULLS LAST,
          CASE WHEN ordering.sort_key = 'ratio'
                        AND ordering.sort_dir = 'desc'
                    THEN {alias}.wilson_score ELSE NULL END DESC NULLS LAST,
          CASE WHEN ordering.sort_key = 'evidence'
                        AND ordering.sort_dir = 'asc'
                    THEN {alias}.negative_evi_count ELSE NULL END ASC NULLS LAST,
          CASE WHEN ordering.sort_key = 'evidence'
                        AND ordering.sort_dir = 'desc'
                    THEN {alias}.negative_evi_count ELSE NULL END DESC NULLS LAST,
          CASE WHEN ordering.sort_key = 'star'
                        AND ordering.sort_dir = 'asc'
                    THEN {alias}.avg_star ELSE NULL END ASC NULLS LAST,
          CASE WHEN ordering.sort_key = 'star'
                        AND ordering.sort_dir = 'desc'
                    THEN {alias}.avg_star ELSE NULL END DESC NULLS LAST,
          CASE WHEN ordering.sort_key = 'issues'
                        AND ordering.sort_dir = 'asc'
                    THEN {alias}.open_issue_count ELSE NULL END ASC NULLS LAST,
          CASE WHEN ordering.sort_key = 'issues'
                        AND ordering.sort_dir = 'asc'
                    THEN {alias}.issue_count ELSE NULL END ASC NULLS LAST,
          CASE WHEN ordering.sort_key = 'issues'
                        AND ordering.sort_dir = 'desc'
                    THEN {alias}.open_issue_count ELSE NULL END DESC NULLS LAST,
          CASE WHEN ordering.sort_key = 'issues'
                        AND ordering.sort_dir = 'desc'
                    THEN {alias}.issue_count ELSE NULL END DESC NULLS LAST,
          {alias}.wilson_score DESC, {alias}.spu
"""


def _wilson_ctes() -> str:
    """给 results CTE 增加 95% Wilson 置信下界，供数据库侧排序。"""
    return """
), sample_sizes AS (
  SELECT r.*,
         (COALESCE(r.negative_evi_count, 0)
          + COALESCE(r.positive_evi_count, 0))::numeric AS sample_size
    FROM results r
), scored AS (
  SELECT r.*,
         CASE
           WHEN r.sample_size = 0 THEN 0::numeric
           ELSE (
             r.negative_ratio
             + 3.8416 / (2 * r.sample_size)
             - 1.96 * sqrt((
                 r.negative_ratio * (1 - r.negative_ratio)
                 + 3.8416 / (4 * r.sample_size)
               ) / r.sample_size)
           ) / (1 + 3.8416 / r.sample_size)
         END AS wilson_score
    FROM sample_sizes r
"""


# 老品迭代队列只收录当前有问题条目的 SPU。分子是非终态条目数，
# 分母是当前 voc_spu_issue 条目数；没有人工记录默认「考虑中」。
def _board_spus(*, revived_only: bool = False) -> str:
    revived_cte = """
revived_spus AS (
  SELECT DISTINCT m.spu
    FROM voc_spu_issue_manual m
    LEFT JOIN voc_spu_issue i
      ON i.spu = m.spu AND i.opp_id = m.opp_id
    LEFT JOIN voc_opportunity o ON o.opp_id = m.opp_id
   WHERE m.status = '考虑中'
     AND m.revived_at IS NOT NULL
     AND i.spu IS NOT NULL
     AND o.opp_id IS NOT NULL
     AND o.merged_into IS NULL
),
""" if revived_only else ""
    revived_join = (
        "  JOIN revived_spus v ON v.spu = c.spu\n" if revived_only else ""
    )
    return """
WITH """ + revived_cte + """issue_state AS (
  SELECT i.spu, i.opp_id,
         COALESCE(m.status, '考虑中') AS status
    FROM voc_spu_issue i
    LEFT JOIN voc_spu_issue_manual m
      ON m.spu = i.spu AND m.opp_id = i.opp_id
), issue_counts AS (
  SELECT x.spu,
         count(*)::int AS issue_count,
         count(*) FILTER (
           WHERE x.status NOT IN ('已完成', '不考虑')
         )::int AS open_issue_count
    FROM issue_state x
   GROUP BY x.spu
), recent AS (
  SELECT x.spu, count(*)::int AS recent_evi_count
    FROM issue_state x
    JOIN voc_opp_evidence oe ON oe.opp_id = x.opp_id
    JOIN voc_message msg ON msg.message_id = oe.message_id
   WHERE x.spu = ANY(COALESCE(msg.spu, ARRAY[]::text[]))
     AND msg.publish_time >= now() - interval '60 days'
   GROUP BY x.spu
), results AS (
  SELECT c.spu, s.product_names, s.skus, s.category, s.prod_line, s.grade,
       s.launch_period, s.message_count, s.negative_evi_count,
       s.positive_evi_count, s.avg_star,
       COALESCE(
         s.negative_evi_count::numeric
           / NULLIF(s.negative_evi_count + s.positive_evi_count, 0),
         0::numeric
       ) AS negative_ratio,
       c.open_issue_count, c.issue_count,
       COALESCE(r.recent_evi_count, 0)::int AS recent_evi_count
  FROM issue_counts c
""" + revived_join + """\
  LEFT JOIN voc_spu s ON s.spu = c.spu
  LEFT JOIN recent r ON r.spu = c.spu

""" + _wilson_ctes() + """)
SELECT r.*
  FROM scored r
 CROSS JOIN (
   SELECT %s::text AS sort_key, %s::text AS sort_dir
 ) ordering
""" + _spu_order("r")


BOARD_SPUS = _board_spus()
BOARD_SPUS_REVIVED = _board_spus(revived_only=True)


# 看板的问题行与详情页使用同一批原子字段。taxonomy 从当前
# evidence 的结构化字段聚合，不读已废弃的旧层级列。
BOARD_ISSUES = """
WITH keys AS (
  SELECT i.spu, i.opp_id
    FROM voc_spu_issue i
    JOIN voc_opportunity o ON o.opp_id = i.opp_id
   WHERE o.merged_into IS NULL
), evidence_agg AS (
  SELECT k.spu, k.opp_id,
         count(*)::int AS actual_evi_count,
         count(*) FILTER (
           WHERE msg.publish_time >= now() - interval '60 days'
         )::int AS recent_evi_count,
         mode() WITHIN GROUP (ORDER BY e.tax_stage)
           FILTER (WHERE e.tax_stage IS NOT NULL) AS tax_stage,
         mode() WITHIN GROUP (ORDER BY e.tax_domain)
           FILTER (WHERE e.tax_domain IS NOT NULL) AS tax_domain,
         mode() WITHIN GROUP (ORDER BY e.tax_sub)
           FILTER (WHERE e.tax_sub IS NOT NULL) AS tax_sub,
         mode() WITHIN GROUP (ORDER BY e.tax_leaf)
           FILTER (WHERE e.tax_leaf IS NOT NULL) AS tax_leaf
    FROM keys k
    JOIN voc_opp_evidence oe ON oe.opp_id = k.opp_id
    JOIN voc_message msg ON msg.message_id = oe.message_id
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
   WHERE k.spu = ANY(COALESCE(msg.spu, ARRAY[]::text[]))
   GROUP BY k.spu, k.opp_id
)
SELECT k.spu, k.opp_id,
       COALESCE(i.evi_count::int, a.actual_evi_count,
                m.baseline_evi_count, 0)::int AS evi_count,
       i.tax_path,
       (i.opp_id IS NULL) AS historical,
       o.title, o.problem_mode, o.n_eff, o.scope,
       COALESCE(m.status, '考虑中') AS status,
       m.owner, m.decision_note, m.baseline_evi_count,
       m.target_release, m.release_date, m.closed_at,
       m.revived_at, m.revive_reason,
       COALESCE(a.recent_evi_count, 0)::int AS recent_evi_count,
       a.tax_stage, a.tax_domain, a.tax_sub, a.tax_leaf
  FROM keys k
  JOIN voc_opportunity o ON o.opp_id = k.opp_id
  LEFT JOIN voc_spu_issue i
    ON i.spu = k.spu AND i.opp_id = k.opp_id
  LEFT JOIN voc_spu_issue_manual m
    ON m.spu = k.spu AND m.opp_id = k.opp_id
  LEFT JOIN evidence_agg a
    ON a.spu = k.spu AND a.opp_id = k.opp_id
 ORDER BY k.spu,
          CASE
            WHEN COALESCE(m.status, '考虑中') IN ('已完成', '不考虑') THEN 2
            WHEN COALESCE(m.status, '考虑中') = '考虑中'
                 AND m.revived_at IS NOT NULL THEN 0
            ELSE 1
          END,
          COALESCE(i.evi_count::int, a.actual_evi_count,
                   m.baseline_evi_count, 0) DESC,
          k.opp_id
"""


# 互动、标签覆盖与品牌都从机会点证据实时聚合。品牌单独拆分为
# CTE，避免 unnest(brands) 把 interactions 与覆盖计数成倍放大。
BOARD_INNOVATIONS = """
WITH evidence_agg AS (
  SELECT oe.opp_id,
         count(*)::int AS attached_count,
         count(*) FILTER (WHERE e.tax_path IS NOT NULL)::int AS tagged_count,
         COALESCE(sum(COALESCE(msg.interactions, 0)), 0)::bigint AS interactions
    FROM voc_opp_evidence oe
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
    JOIN voc_message msg ON msg.message_id = oe.message_id
   GROUP BY oe.opp_id
), brand_agg AS (
  SELECT oe.opp_id,
         array_agg(DISTINCT x.brand ORDER BY x.brand) AS brands
    FROM voc_opp_evidence oe
    JOIN voc_message msg ON msg.message_id = oe.message_id
    JOIN LATERAL (
      SELECT NULLIF(btrim(raw.brand), '') AS brand
        FROM unnest(COALESCE(msg.brands, ARRAY[]::text[])) AS raw(brand)
    ) x ON TRUE
   WHERE x.brand IS NOT NULL
   GROUP BY oe.opp_id
)
SELECT o.opp_id, o.title, o.channel, o.prod_line, o.core_tag,
       o.desc_phenomenon, o.desc_attribution, o.desc_suggestion,
       o.evi_total, o.evi_social, o.rank_score,
       o.first_week, o.last_week, o.released_at,
       COALESCE(m.status, '考虑中') AS status,
       m.owner, m.decision_note, m.target_release, m.release_date,
       COALESCE(a.interactions, 0)::bigint AS interactions,
       COALESCE(b.brands, ARRAY[]::text[]) AS brands,
       COALESCE(a.tagged_count, 0)::int AS tagged_count,
       COALESCE(a.attached_count, 0)::int AS attached_count
  FROM voc_opportunity o
  LEFT JOIN voc_opportunity_manual m ON m.opp_id = o.opp_id
  LEFT JOIN evidence_agg a ON a.opp_id = o.opp_id
  LEFT JOIN brand_agg b ON b.opp_id = o.opp_id
 WHERE o.opp_type = '新品创新'
   AND o.classification_state = '确定'
   AND o.merged_into IS NULL
 ORDER BY o.rank_score DESC NULLS LAST,
          COALESCE(a.interactions, 0) DESC,
          o.opp_id
"""


def _search_spus(tag_condition: str = "") -> str:
    return """
WITH issue_counts AS (
  SELECT i.spu,
         count(*)::int AS issue_count,
         count(*) FILTER (
           WHERE COALESCE(m.status, '考虑中') NOT IN ('已完成', '不考虑')
         )::int AS open_issue_count,
         bool_or(
           COALESCE(m.status, '考虑中') = '考虑中'
           AND m.revived_at IS NOT NULL
         ) AS has_revived_issue
    FROM voc_spu_issue i
    LEFT JOIN voc_spu_issue_manual m
      ON m.spu = i.spu AND m.opp_id = i.opp_id
   GROUP BY i.spu
), scale AS (
  SELECT COALESCE(max(s.negative_evi_count), 0)::bigint
           AS max_negative_evi_count
    FROM voc_spu s
), results AS (
  SELECT s.spu, s.product_names, s.skus, s.category, s.prod_line, s.grade,
         s.launch_period, s.message_count, s.negative_evi_count,
         s.positive_evi_count, s.avg_star,
         COALESCE(
           s.negative_evi_count::numeric
             / NULLIF(s.negative_evi_count + s.positive_evi_count, 0),
           0::numeric
         ) AS negative_ratio,
         COALESCE(c.open_issue_count, 0)::int AS open_issue_count,
         COALESCE(c.issue_count, 0)::int AS issue_count,
         COALESCE(c.has_revived_issue, false) AS has_revived_issue
    FROM voc_spu s
    LEFT JOIN issue_counts c ON c.spu = s.spu
   WHERE (%s = ''
          OR s.spu ILIKE %s
          OR EXISTS (
            SELECT 1
              FROM unnest(COALESCE(s.product_names, ARRAY[]::text[])) AS pn(name)
             WHERE pn.name ILIKE %s
          )
          OR EXISTS (
            SELECT 1
              FROM unnest(COALESCE(s.skus, ARRAY[]::text[])) AS sk(code)
             WHERE sk.code ILIKE %s
          ))
     AND (%s = '' OR COALESCE(s.category, '未分类') = %s)
""" + tag_condition + """

""" + _wilson_ctes() + """)
SELECT r.*, x.max_negative_evi_count
  FROM scored r
  JOIN scale x ON TRUE
 CROSS JOIN (
   SELECT %s::text AS sort_key, %s::text AS sort_dir
 ) ordering
""" + _spu_order("r")


# 未选择标签时必须走不含 EXISTS 的查询；把标签条件写成
# 「参数为空 OR EXISTS (...)」会让 14.8 万行 evidence 无意义地参与计划。
SEARCH_SPUS = _search_spus()
SEARCH_SPUS_BY_DOMAIN = _search_spus("""
     AND EXISTS (
       SELECT 1
         FROM voc_message msg
         JOIN voc_evidence e ON e.message_id = msg.message_id
        WHERE s.spu = ANY(COALESCE(msg.spu, ARRAY[]::text[]))
          AND e.sentiment = '负面'
          AND e.tax_domain = %s
     )
""")
SEARCH_SPUS_BY_SUB = _search_spus("""
     AND EXISTS (
       SELECT 1
         FROM voc_message msg
         JOIN voc_evidence e ON e.message_id = msg.message_id
        WHERE s.spu = ANY(COALESCE(msg.spu, ARRAY[]::text[]))
          AND e.sentiment = '负面'
          AND e.tax_domain = %s
          AND e.tax_sub = %s
     )
""")
SEARCH_SPUS_BY_LEAF = _search_spus("""
     AND EXISTS (
       SELECT 1
         FROM voc_message msg
         JOIN voc_evidence e ON e.message_id = msg.message_id
        WHERE s.spu = ANY(COALESCE(msg.spu, ARRAY[]::text[]))
          AND e.sentiment = '负面'
          AND e.tax_domain = %s
          AND e.tax_sub = %s
          AND e.tax_leaf = %s
     )
""")


# 标签总量很小，一次聚合出三级选项比逐项查询更稳定；candidates 先应用
# 关键词与品类，因此计数反映当前这两个「其他筛选」，而不是退化成全库计数。
SEARCH_TAG_FACETS = """
WITH candidates AS (
  SELECT s.spu
    FROM voc_spu s
   WHERE (%s = ''
          OR s.spu ILIKE %s
          OR EXISTS (
            SELECT 1
              FROM unnest(COALESCE(s.product_names, ARRAY[]::text[])) AS pn(name)
             WHERE pn.name ILIKE %s
          )
          OR EXISTS (
            SELECT 1
              FROM unnest(COALESCE(s.skus, ARRAY[]::text[])) AS sk(code)
             WHERE sk.code ILIKE %s
          ))
     AND (%s = '' OR COALESCE(s.category, '未分类') = %s)
), tagged AS (
  SELECT DISTINCT c.spu, e.tax_domain, e.tax_sub, e.tax_leaf
    FROM candidates c
    JOIN voc_message msg
      ON c.spu = ANY(COALESCE(msg.spu, ARRAY[]::text[]))
    JOIN voc_evidence e ON e.message_id = msg.message_id
   WHERE e.sentiment = '负面'
     AND NULLIF(btrim(e.tax_domain), '') IS NOT NULL
)
SELECT x.level, x.domain, x.sub, x.leaf, x.count
  FROM (
    SELECT 1 AS level_order, 'domain'::text AS level,
           t.tax_domain AS domain, NULL::text AS sub, NULL::text AS leaf,
           count(DISTINCT t.spu)::int AS count
      FROM tagged t
     GROUP BY t.tax_domain
    UNION ALL
    SELECT 2 AS level_order, 'sub'::text AS level,
           t.tax_domain AS domain, t.tax_sub AS sub, NULL::text AS leaf,
           count(DISTINCT t.spu)::int AS count
      FROM tagged t
     WHERE NULLIF(btrim(t.tax_sub), '') IS NOT NULL
     GROUP BY t.tax_domain, t.tax_sub
    UNION ALL
    SELECT 3 AS level_order, 'leaf'::text AS level,
           t.tax_domain AS domain, t.tax_sub AS sub, t.tax_leaf AS leaf,
           count(DISTINCT t.spu)::int AS count
      FROM tagged t
     WHERE NULLIF(btrim(t.tax_sub), '') IS NOT NULL
       AND NULLIF(btrim(t.tax_leaf), '') IS NOT NULL
     GROUP BY t.tax_domain, t.tax_sub, t.tax_leaf
  ) x
 ORDER BY x.level_order, x.count DESC, x.domain, x.sub, x.leaf
"""


# 品类 facet 是全量 SPU 的稳定导航读数，不随搜索词改变。
SEARCH_FACETS = """
SELECT 'category'::text AS facet, '__all__'::text AS value,
       count(*)::int AS count
  FROM voc_spu s
UNION ALL
SELECT 'category'::text AS facet,
       COALESCE(s.category, '未分类') AS value,
       count(*)::int AS count
  FROM voc_spu s
 GROUP BY COALESCE(s.category, '未分类')
 ORDER BY facet, value
"""


SPU_DETAIL = """
WITH medians AS (
  SELECT percentile_cont(0.5) WITHIN GROUP (
           ORDER BY (
             s.negative_evi_count::numeric
               / NULLIF(s.negative_evi_count + s.positive_evi_count, 0)
           )::double precision
         ) FILTER (
           WHERE s.negative_evi_count + s.positive_evi_count > 0
         ) AS median_negative_ratio,
         percentile_cont(0.5) WITHIN GROUP (
           ORDER BY s.avg_star::double precision
         ) FILTER (WHERE s.avg_star IS NOT NULL) AS median_avg_star
    FROM voc_spu s
), issue_counts AS (
  SELECT i.spu,
         count(*)::int AS issue_count,
         count(*) FILTER (
           WHERE COALESCE(m.status, '考虑中') NOT IN ('已完成', '不考虑')
         )::int AS open_issue_count
    FROM voc_spu_issue i
    LEFT JOIN voc_spu_issue_manual m
      ON m.spu = i.spu AND m.opp_id = i.opp_id
   GROUP BY i.spu
)
SELECT s.*,
       COALESCE(
         s.negative_evi_count::numeric
           / NULLIF(s.negative_evi_count + s.positive_evi_count, 0),
         0::numeric
       ) AS negative_ratio,
       COALESCE(c.open_issue_count, 0)::int AS open_issue_count,
       COALESCE(c.issue_count, 0)::int AS issue_count,
       md.median_negative_ratio, md.median_avg_star
  FROM voc_spu s
  JOIN medians md ON TRUE
  LEFT JOIN issue_counts c ON c.spu = s.spu
 WHERE s.spu = %s
"""


SPU_ISSUES = """
WITH keys AS (
  SELECT i.spu, i.opp_id
    FROM voc_spu_issue i
    JOIN voc_opportunity o ON o.opp_id = i.opp_id
   WHERE i.spu = %s AND o.merged_into IS NULL
  UNION
  SELECT m.spu, m.opp_id
    FROM voc_spu_issue_manual m
    JOIN voc_opportunity o ON o.opp_id = m.opp_id
   WHERE m.spu = %s
     AND o.classification_state = '确定'
     AND o.opp_type = '老品迭代'
), evidence_agg AS (
  SELECT k.spu, k.opp_id,
         count(*)::int AS actual_evi_count,
         count(*) FILTER (
           WHERE msg.publish_time >= now() - interval '60 days'
         )::int AS recent_evi_count,
         mode() WITHIN GROUP (ORDER BY e.tax_stage)
           FILTER (WHERE e.tax_stage IS NOT NULL) AS tax_stage,
         mode() WITHIN GROUP (ORDER BY e.tax_domain)
           FILTER (WHERE e.tax_domain IS NOT NULL) AS tax_domain,
         mode() WITHIN GROUP (ORDER BY e.tax_sub)
           FILTER (WHERE e.tax_sub IS NOT NULL) AS tax_sub,
         mode() WITHIN GROUP (ORDER BY e.tax_leaf)
           FILTER (WHERE e.tax_leaf IS NOT NULL) AS tax_leaf
    FROM keys k
    JOIN voc_opp_evidence oe ON oe.opp_id = k.opp_id
    JOIN voc_message msg ON msg.message_id = oe.message_id
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
   WHERE k.spu = ANY(COALESCE(msg.spu, ARRAY[]::text[]))
   GROUP BY k.spu, k.opp_id
)
SELECT k.spu, k.opp_id,
       COALESCE(i.evi_count::int, a.actual_evi_count,
                m.baseline_evi_count, 0)::int AS evi_count,
       i.tax_path,
       (i.opp_id IS NULL) AS historical,
       o.title, o.problem_mode, o.n_eff, o.scope,
       COALESCE(m.status, '考虑中') AS status,
       m.owner, m.decision_note, m.baseline_evi_count,
       m.target_release, m.release_date, m.closed_at,
       m.revived_at, m.revive_reason,
       COALESCE(a.recent_evi_count, 0)::int AS recent_evi_count,
       a.tax_stage, a.tax_domain, a.tax_sub, a.tax_leaf
  FROM keys k
  JOIN voc_opportunity o ON o.opp_id = k.opp_id
  LEFT JOIN voc_spu_issue i
    ON i.spu = k.spu AND i.opp_id = k.opp_id
  LEFT JOIN voc_spu_issue_manual m
    ON m.spu = k.spu AND m.opp_id = k.opp_id
  LEFT JOIN evidence_agg a
    ON a.spu = k.spu AND a.opp_id = k.opp_id
 ORDER BY CASE
            WHEN COALESCE(m.status, '考虑中') IN ('已完成', '不考虑') THEN 2
            WHEN COALESCE(m.status, '考虑中') = '考虑中'
                 AND m.revived_at IS NOT NULL THEN 0
            ELSE 1
          END,
          COALESCE(i.evi_count::int, a.actual_evi_count,
                   m.baseline_evi_count, 0) DESC,
          k.opp_id
"""


ISSUE_DETAIL = """
WITH key AS (
  SELECT %s::text AS spu, %s::text AS opp_id
), evidence_agg AS (
  SELECT k.spu, k.opp_id,
         count(*)::int AS actual_evi_count,
         count(*) FILTER (
           WHERE msg.publish_time >= now() - interval '60 days'
         )::int AS recent_evi_count,
         avg(msg.star) FILTER (WHERE msg.star IS NOT NULL) AS voice_avg_star,
         mode() WITHIN GROUP (ORDER BY e.tax_stage)
           FILTER (WHERE e.tax_stage IS NOT NULL) AS tax_stage,
         mode() WITHIN GROUP (ORDER BY e.tax_domain)
           FILTER (WHERE e.tax_domain IS NOT NULL) AS tax_domain,
         mode() WITHIN GROUP (ORDER BY e.tax_sub)
           FILTER (WHERE e.tax_sub IS NOT NULL) AS tax_sub,
         mode() WITHIN GROUP (ORDER BY e.tax_leaf)
           FILTER (WHERE e.tax_leaf IS NOT NULL) AS tax_leaf
    FROM key k
    JOIN voc_opp_evidence oe ON oe.opp_id = k.opp_id
    JOIN voc_message msg ON msg.message_id = oe.message_id
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
   WHERE k.spu = ANY(COALESCE(msg.spu, ARRAY[]::text[]))
   GROUP BY k.spu, k.opp_id
)
SELECT k.spu, k.opp_id,
       COALESCE(i.evi_count::int, a.actual_evi_count,
                m.baseline_evi_count, 0)::int AS evi_count,
       i.tax_path,
       (i.opp_id IS NULL AND m.opp_id IS NOT NULL) AS historical,
       o.title, o.problem_mode, o.n_eff, o.scope,
       COALESCE(m.status, '考虑中') AS status,
       m.owner, m.decision_note, m.baseline_evi_count,
       m.target_release, m.release_date, m.closed_at,
       m.revived_at, m.revive_reason,
       s.product_names, s.skus, s.category, s.prod_line,
       s.grade, s.launch_period,
       COALESCE(a.recent_evi_count, 0)::int AS recent_evi_count,
       a.voice_avg_star,
       a.tax_stage, a.tax_domain, a.tax_sub, a.tax_leaf
  FROM key k
  JOIN voc_opportunity o ON o.opp_id = k.opp_id
  LEFT JOIN voc_spu_issue i
    ON i.spu = k.spu AND i.opp_id = k.opp_id
  LEFT JOIN voc_spu_issue_manual m
    ON m.spu = k.spu AND m.opp_id = k.opp_id
  LEFT JOIN voc_spu s ON s.spu = k.spu
  LEFT JOIN evidence_agg a
    ON a.spu = k.spu AND a.opp_id = k.opp_id
 WHERE (i.opp_id IS NOT NULL OR m.opp_id IS NOT NULL)
   AND o.classification_state = '确定'
   AND o.opp_type = '老品迭代'
"""


ISSUE_VOICES = """
SELECT oe.message_id, oe.seq,
       COALESCE(NULLIF(e.snippet, ''), msg.content) AS voice_text,
       CASE
         WHEN msg.content_zh IS DISTINCT FROM msg.content THEN msg.content_zh
       END AS translation,
       e.tax_stage, e.tax_domain, e.tax_sub, e.tax_leaf,
       msg.platform, msg.publish_time, msg.star, msg.country, msg.lang,
       msg.interactions, msg.url
  FROM voc_opp_evidence oe
  JOIN voc_evidence e
    ON e.message_id = oe.message_id AND e.seq = oe.seq
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE oe.opp_id = %s
   AND %s = ANY(COALESCE(msg.spu, ARRAY[]::text[]))
 ORDER BY msg.publish_time DESC NULLS LAST, oe.message_id, oe.seq
"""


INNOVATION_DETAIL = """
WITH evidence_agg AS (
  SELECT oe.opp_id,
         count(*)::int AS attached_count,
         count(*) FILTER (WHERE e.tax_path IS NOT NULL)::int AS tagged_count,
         COALESCE(sum(COALESCE(msg.interactions, 0)), 0)::bigint AS interactions
    FROM voc_opp_evidence oe
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
    JOIN voc_message msg ON msg.message_id = oe.message_id
   GROUP BY oe.opp_id
), brand_agg AS (
  SELECT oe.opp_id,
         array_agg(DISTINCT x.brand ORDER BY x.brand) AS brands
    FROM voc_opp_evidence oe
    JOIN voc_message msg ON msg.message_id = oe.message_id
    JOIN LATERAL (
      SELECT NULLIF(btrim(raw.brand), '') AS brand
        FROM unnest(COALESCE(msg.brands, ARRAY[]::text[])) AS raw(brand)
    ) x ON TRUE
   WHERE x.brand IS NOT NULL
   GROUP BY oe.opp_id
)
SELECT o.opp_id, o.opp_type, o.channel, o.prod_line, o.core_tag, o.title,
       o.problem_mode, o.desc_phenomenon, o.desc_attribution,
       o.desc_suggestion, o.evi_total, o.evi_social, o.rank_score,
       o.countries, o.rep_snippets, o.first_week, o.last_week,
       o.released_at, o.safety_flag,
       COALESCE(m.status, '考虑中') AS status,
       m.owner, m.note, m.decision_note, m.target_release, m.release_date,
       COALESCE(a.interactions, 0)::bigint AS interactions,
       COALESCE(b.brands, ARRAY[]::text[]) AS brands,
       COALESCE(a.tagged_count, 0)::int AS tagged_count,
       COALESCE(a.attached_count, 0)::int AS attached_count
  FROM voc_opportunity o
  LEFT JOIN voc_opportunity_manual m ON m.opp_id = o.opp_id
  LEFT JOIN evidence_agg a ON a.opp_id = o.opp_id
  LEFT JOIN brand_agg b ON b.opp_id = o.opp_id
 WHERE o.opp_id = %s
   AND o.opp_type = '新品创新'
   AND o.classification_state = '确定'
   AND o.merged_into IS NULL
"""


# 原型只展示三条「灵感来源」，按互动与发布时间取最有代表性的原声。
INNOVATION_EVIDENCE = """
SELECT oe.message_id, oe.seq,
       COALESCE(NULLIF(e.snippet, ''), NULLIF(msg.content_zh, ''), msg.content)
         AS voice_text,
       e.snippet, e.tax_stage, e.tax_domain, e.tax_sub, e.tax_leaf,
       msg.platform, msg.publish_time, msg.interactions,
       msg.brands, msg.content, msg.content_zh
  FROM voc_opp_evidence oe
  JOIN voc_evidence e
    ON e.message_id = oe.message_id AND e.seq = oe.seq
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE oe.opp_id = %s
 ORDER BY msg.interactions DESC NULLS LAST,
          msg.publish_time DESC NULLS LAST,
          oe.message_id, oe.seq
 LIMIT 3
"""


STRATEGY_OPPORTUNITIES = """
WITH spread AS (
  SELECT i.opp_id, count(DISTINCT i.spu)::int AS spu_count
    FROM voc_spu_issue i
   GROUP BY i.opp_id
)
SELECT o.opp_id, o.title, o.prod_line, o.core_tag, o.n_eff, o.scope,
       o.evi_total, o.released_at,
       COALESCE(s.spu_count, 0)::int AS spu_count
  FROM voc_opportunity o
  LEFT JOIN spread s ON s.opp_id = o.opp_id
 WHERE o.merged_into IS NULL
   AND o.classification_state = '确定'
   AND o.scope IN ('品线级', '多品', '单品')
 ORDER BY o.n_eff DESC NULLS LAST, o.evi_total DESC, o.opp_id
"""


# 状态机校验、baseline/closed_at 维护与审计全由数据库触发器负责。
UPDATE_ISSUE_STATUS = """
INSERT INTO voc_spu_issue_manual
       (spu, opp_id, status, decision_note, target_release, release_date, updated_by)
VALUES (%s, %s, %s, NULLIF(%s, ''), NULLIF(%s, ''), %s, %s)
ON CONFLICT (spu, opp_id) DO UPDATE SET
       status = EXCLUDED.status,
       decision_note = CASE WHEN EXCLUDED.status = '不考虑'
                            THEN EXCLUDED.decision_note
                            ELSE voc_spu_issue_manual.decision_note END,
       target_release = COALESCE(
         EXCLUDED.target_release, voc_spu_issue_manual.target_release
       ),
       release_date = COALESCE(
         EXCLUDED.release_date, voc_spu_issue_manual.release_date
       ),
       updated_by = EXCLUDED.updated_by
"""


UPDATE_INNOVATION_STATUS = """
INSERT INTO voc_opportunity_manual
       (opp_id, status, decision_note, target_release, release_date, updated_by)
VALUES (%s, %s, NULLIF(%s, ''), NULLIF(%s, ''), %s, %s)
ON CONFLICT (opp_id) DO UPDATE SET
       status = EXCLUDED.status,
       decision_note = CASE WHEN EXCLUDED.status = '不考虑'
                            THEN EXCLUDED.decision_note
                            ELSE voc_opportunity_manual.decision_note END,
       target_release = COALESCE(
         EXCLUDED.target_release, voc_opportunity_manual.target_release
       ),
       release_date = COALESCE(
         EXCLUDED.release_date, voc_opportunity_manual.release_date
       ),
       updated_by = EXCLUDED.updated_by
"""


REVIVE_DISMISSED = """
SELECT m.spu, m.opp_id, m.baseline_evi_count,
       i.evi_count AS current_evi_count,
       m.target_release, m.release_date
  FROM voc_spu_issue_manual m
  JOIN voc_spu_issue i ON i.spu = m.spu AND i.opp_id = m.opp_id
 WHERE m.status = '不考虑'
   AND m.baseline_evi_count IS NOT NULL
   AND i.evi_count >= 3 * m.baseline_evi_count
"""


REVIVE_COMPLETED = """
SELECT m.spu, m.opp_id, m.baseline_evi_count,
       i.evi_count AS current_evi_count, m.target_release, m.release_date,
       count(*)::int AS new_feedback_count
  FROM voc_spu_issue_manual m
  LEFT JOIN voc_spu_issue i
    ON i.spu = m.spu AND i.opp_id = m.opp_id
  JOIN voc_opportunity o ON o.opp_id = m.opp_id
  JOIN voc_opp_evidence oe ON oe.opp_id = m.opp_id
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE m.status = '已完成'
   AND o.classification_state = '确定'
   AND o.opp_type = '老品迭代'
   AND m.release_date IS NOT NULL
   AND m.spu = ANY(COALESCE(msg.spu, ARRAY[]::text[]))
   AND msg.publish_time::date > m.release_date
 GROUP BY m.spu, m.opp_id, m.baseline_evi_count, i.evi_count,
          m.target_release, m.release_date
HAVING count(*) > 0
"""


APPLY_REVIVAL = """
UPDATE voc_spu_issue_manual
   SET status = '考虑中', revived_at = now(), revive_reason = %s, updated_by = %s
 WHERE spu = %s AND opp_id = %s
"""

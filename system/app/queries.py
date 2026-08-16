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
      AND o.merged_into IS NULL) AS inno,
  (SELECT count(*)::int
     FROM voc_opportunity o
    WHERE o.scope = '品线级'
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
   WHERE m.spu = %s
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
 WHERE i.opp_id IS NOT NULL OR m.opp_id IS NOT NULL
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
  JOIN voc_opp_evidence oe ON oe.opp_id = m.opp_id
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE m.status = '已完成'
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

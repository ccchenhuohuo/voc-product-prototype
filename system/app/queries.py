"""VOC 看板的全部 SQL。路由中不得散落查询语句。"""


def _assignment_snapshot_ctes() -> str:
    """当前已发布 v3 问题层对应的消息级冻结归属。"""
    return """
latest_assignment_run AS MATERIALIZED (
  SELECT split_part(r.run_id, ':', 1) AS run_id
    FROM voc_run_log r
   WHERE r.stage = 'finalize' AND r.status = 'success'
     -- shadow 期间兼容视图仍指向 v2，不能提前把 v3 原声混进产品页。
     AND position(
           'voc_spu_issue_v3'
           IN pg_get_viewdef('voc_spu_issue'::regclass, true)
         ) > 0
     AND EXISTS (
       SELECT 1
         FROM voc_run_log snapshot_log
        WHERE snapshot_log.run_id =
              split_part(r.run_id, ':', 1) || ':assign_snapshot'
          AND snapshot_log.stage = 'assign_snapshot'
          AND snapshot_log.status = 'success'
     )
   ORDER BY r.finished_at DESC NULLS LAST, r.run_id DESC
   LIMIT 1
), snapshot_message_spu AS MATERIALIZED (
  SELECT DISTINCT s.message_id, s.assigned_spu AS spu
    FROM voc_assign_snapshot s
    JOIN latest_assignment_run r ON r.run_id = s.run_id
)
"""


# 侧栏读数与 base.html 的 nav_counts 键一一对应。老品迭代双数分别是
# voc_spu 全量产品数，以及至少有一张现存问题卡的 SPU 数。
SHELL_COUNTS = """
SELECT
  (SELECT count(*)::int FROM voc_spu s) AS products,
  (SELECT count(DISTINCT i.spu)::int
     FROM voc_spu_issue i
     JOIN voc_opportunity o ON o.opp_id = i.opp_id
    WHERE o.classification_state = '确定'
      AND o.merged_into IS NULL) AS iter,
  (SELECT count(*)::int
    FROM voc_opportunity o
    WHERE o.opp_id LIKE 'OPP2-%'
      AND o.opp_type = '新品创新'
      AND o.classification_state = '确定'
      AND o.merged_into IS NULL) AS inno,
  (SELECT count(*)::int
    FROM voc_opportunity o
    WHERE o.opp_id LIKE 'OPP2-%'
      AND o.scope_source IS DISTINCT FROM 'v3-未计算'
      AND o.scope = '品线级'
      AND o.classification_state = '确定'
      AND o.merged_into IS NULL) AS strategy,
  (SELECT count(DISTINCT m.spu)::int
     FROM voc_spu_issue_manual m
     JOIN voc_spu_issue i
       ON i.spu = m.spu AND i.opp_id = m.opp_id
     JOIN voc_opportunity o ON o.opp_id = m.opp_id
    WHERE m.status = '考虑中'
      AND m.revived_at IS NOT NULL
      AND o.merged_into IS NULL) AS revived
"""


# 首页来源卡只读事实层与 022 探针缓存。客服在事实层没有合法 src_line，
# 因此通过来源定义 LEFT JOIN 得到真实的 0；缓存缺行时 matched_count 保持 NULL，
# 让页面显示「未探测」，不能把未知伪装成 0。
HOME2_SOURCES = """
WITH source_defs(source_order, source_key, source_label, src_line,
                 query_task_type) AS (
  VALUES (1, 'ec'::text, '电商评论'::text, '电商'::text, 'COMMENT'::text),
         (2, 'social', '社交媒体', '社媒', 'SOCIAL'),
         (3, 'service', '客服会话', '客服', 'SERVICE')
), message_counts AS (
  SELECT m.src_line, count(*)::bigint AS used_count
    FROM voc_message m
   GROUP BY m.src_line
)
SELECT d.source_order, d.source_key, d.source_label, d.src_line,
       d.query_task_type, COALESCE(m.used_count, 0)::bigint AS used_count,
       p.matched_count AS available_count,
       p.window_start, p.window_end, p.probed_at
  FROM source_defs d
  LEFT JOIN message_counts m ON m.src_line = d.src_line
  LEFT JOIN voc_source_probe p ON p.query_task_type = d.query_task_type
 ORDER BY d.source_order
"""


# 社媒 content_type 是多值数组。先按任务书给定优先级把每条消息压成唯一标签，
# 再把关系层压成「每条消息是否进入各生命周期」的布尔值；这样同一消息有多条
# 证据或挂到多个同生命周期机会点时仍只计一次，各标签总和严格等于社媒消息数。
HOME2_FLOW_SOCIAL = """
WITH label_defs(label_order, label) AS (
  VALUES (1, '产品种草广告'::text),
         (2, '用户咨询'),
         (3, '用户使用体验'),
         (4, '产品评测'),
         (5, '竞品拉踩'),
         (6, '其他'),
         (7, '无标签'),
         (8, '二手转让')
), assigned AS MATERIALIZED (
  SELECT m.message_id,
         CASE
           WHEN COALESCE(m.content_type, ARRAY[]::text[])
                  && ARRAY['用户咨询']::text[] THEN '用户咨询'
           WHEN COALESCE(m.content_type, ARRAY[]::text[])
                  && ARRAY['用户使用体验']::text[] THEN '用户使用体验'
           WHEN COALESCE(m.content_type, ARRAY[]::text[])
                  && ARRAY['产品评测']::text[] THEN '产品评测'
           WHEN COALESCE(m.content_type, ARRAY[]::text[])
                  && ARRAY['竞品拉踩']::text[] THEN '竞品拉踩'
           WHEN COALESCE(m.content_type, ARRAY[]::text[])
                  && ARRAY['其他']::text[] THEN '其他'
           WHEN COALESCE(m.content_type, ARRAY[]::text[])
                  && ARRAY['二手转让']::text[] THEN '二手转让'
           WHEN COALESCE(m.content_type, ARRAY[]::text[])
                  && ARRAY['产品种草广告']::text[] THEN '产品种草广告'
           ELSE '无标签'
         END AS label
    FROM voc_message m
   WHERE m.src_line = '社媒'
), lifecycle_by_message AS MATERIALIZED (
  SELECT x.message_id,
         bool_or(o.opp_type = '老品迭代') AS enters_iter,
         bool_or(o.opp_type = '新品创新') AS enters_inno
    FROM (
      SELECT DISTINCT oe.message_id, oe.opp_id
        FROM voc_opp_evidence oe
    ) x
    JOIN voc_opportunity o ON o.opp_id = x.opp_id
   WHERE o.classification_state = '确定'
     AND o.merged_into IS NULL
     AND o.opp_id LIKE 'OPP2-%'
     AND o.opp_type IN ('老品迭代', '新品创新')
   GROUP BY x.message_id
), aggregated AS (
  SELECT a.label, count(*)::bigint AS message_count,
         count(*) FILTER (WHERE COALESCE(l.enters_iter, false))::bigint
           AS iter_count,
         count(*) FILTER (WHERE COALESCE(l.enters_inno, false))::bigint
           AS inno_count
    FROM assigned a
    LEFT JOIN lifecycle_by_message l ON l.message_id = a.message_id
   GROUP BY a.label
), class_count AS (
  SELECT count(DISTINCT tag)::int AS label_class_count
    FROM voc_message m
   CROSS JOIN LATERAL unnest(COALESCE(m.content_type, ARRAY[]::text[])) tag
   WHERE m.src_line = '社媒'
     AND tag IN ('用户咨询', '用户使用体验', '产品评测', '竞品拉踩',
                 '其他', '二手转让', '产品种草广告')
)
SELECT d.label_order, d.label,
       COALESCE(a.message_count, 0)::bigint AS message_count,
       COALESCE(a.iter_count, 0)::bigint AS iter_count,
       COALESCE(a.inno_count, 0)::bigint AS inno_count,
       sum(COALESCE(a.message_count, 0)) OVER ()::bigint AS source_total,
       c.label_class_count
  FROM label_defs d
  LEFT JOIN aggregated a ON a.label = d.label
 CROSS JOIN class_count c
 ORDER BY d.label_order
"""


# 电商没有 content_type。按每条消息 seq 最小的证据标签唯一归因，再按消息量
# 动态取 Top 6；未进 Top 6 的非空标签合并为「其余 N 类」，没有证据标签的消息
# 独立归入「无标签」。关系层同样先压成消息级布尔值，避免多证据重复计流。
HOME2_FLOW_EC = """
WITH primary_evidence AS MATERIALIZED (
  SELECT DISTINCT ON (e.message_id)
         e.message_id, COALESCE(NULLIF(btrim(e.tag), ''), '无标签') AS label
    FROM voc_evidence e
   ORDER BY e.message_id, e.seq
), assigned AS MATERIALIZED (
  SELECT m.message_id, COALESCE(p.label, '无标签') AS label
    FROM voc_message m
    LEFT JOIN primary_evidence p ON p.message_id = m.message_id
   WHERE m.src_line = '电商'
), lifecycle_by_message AS MATERIALIZED (
  SELECT x.message_id,
         bool_or(o.opp_type = '老品迭代') AS enters_iter,
         bool_or(o.opp_type = '新品创新') AS enters_inno
    FROM (
      SELECT DISTINCT oe.message_id, oe.opp_id
        FROM voc_opp_evidence oe
    ) x
    JOIN voc_opportunity o ON o.opp_id = x.opp_id
   WHERE o.classification_state = '确定'
     AND o.merged_into IS NULL
     AND o.opp_id LIKE 'OPP2-%'
     AND o.opp_type IN ('老品迭代', '新品创新')
   GROUP BY x.message_id
), tag_counts AS (
  SELECT a.label, count(*)::bigint AS message_count,
         count(*) FILTER (WHERE COALESCE(l.enters_iter, false))::bigint
           AS iter_count,
         count(*) FILTER (WHERE COALESCE(l.enters_inno, false))::bigint
           AS inno_count
    FROM assigned a
    LEFT JOIN lifecycle_by_message l ON l.message_id = a.message_id
   GROUP BY a.label
), ranked AS (
  SELECT t.*,
         row_number() OVER (
           ORDER BY t.message_count DESC, t.label
         )::int AS tag_rank
    FROM tag_counts t
   WHERE t.label <> '无标签'
), class_summary AS (
  SELECT (SELECT count(DISTINCT NULLIF(btrim(e.tag), ''))::int
            FROM voc_evidence e
            JOIN voc_message m ON m.message_id = e.message_id
           WHERE m.src_line = '电商'
             AND NULLIF(btrim(e.tag), '') IS NOT NULL) AS label_class_count,
         count(*) FILTER (WHERE r.tag_rank > 6)::int AS remaining_class_count
    FROM ranked r
), buckets AS (
  SELECT r.tag_rank AS bucket_order, 'top'::text AS bucket_key, r.label,
         r.message_count, r.iter_count, r.inno_count
    FROM ranked r
   WHERE r.tag_rank <= 6
  UNION ALL
  SELECT 7, 'other', '其余 ' || c.remaining_class_count::text || ' 类',
         COALESCE(sum(r.message_count), 0)::bigint,
         COALESCE(sum(r.iter_count), 0)::bigint,
         COALESCE(sum(r.inno_count), 0)::bigint
    FROM class_summary c
    JOIN ranked r ON r.tag_rank > 6
   GROUP BY c.remaining_class_count
  UNION ALL
  SELECT 8, 'untagged', '无标签',
         COALESCE(t.message_count, 0)::bigint,
         COALESCE(t.iter_count, 0)::bigint,
         COALESCE(t.inno_count, 0)::bigint
    FROM (SELECT 1) seed
    LEFT JOIN tag_counts t ON t.label = '无标签'
)
SELECT b.bucket_order, b.bucket_key, b.label,
       b.message_count, b.iter_count, b.inno_count,
       sum(b.message_count) OVER ()::bigint AS source_total,
       c.label_class_count, c.remaining_class_count
  FROM buckets b
 CROSS JOIN class_summary c
 ORDER BY b.bucket_order
"""


# 两条生命周期共用五状态，但锚点不同：老品直接数 SPU×问题行，新品直接数
# 新品机会点。人工表缺行时用 COALESCE(..., '考虑中')，与业务页既有默认口径
# 一致。机器放行态另作正交的全库统计，不与人工状态互相扣减。
HOME2_STATUS = """
WITH statuses(status_order, status) AS (
  VALUES (1, '考虑中'::text),
         (2, '在跟进'),
         (3, '项目中'),
         (4, '已完成'),
         (5, '不考虑')
), iter_counts AS (
  SELECT COALESCE(m.status, '考虑中') AS status,
         count(*)::bigint AS item_count
    FROM voc_spu_issue i
    LEFT JOIN voc_spu_issue_manual m
      ON m.spu = i.spu AND m.opp_id = i.opp_id
   GROUP BY COALESCE(m.status, '考虑中')
), inno_counts AS (
  SELECT COALESCE(m.status, '考虑中') AS status,
         count(*)::bigint AS item_count
    FROM voc_opportunity o
    LEFT JOIN voc_opportunity_manual m ON m.opp_id = o.opp_id
   WHERE o.opp_id LIKE 'OPP2-%'
     AND o.opp_type = '新品创新'
   GROUP BY COALESCE(m.status, '考虑中')
), lifecycle_rows AS (
  SELECT 1 AS lifecycle_order, '老品迭代'::text AS lifecycle,
         '锚定 SPU × 问题'::text AS anchor,
         s.status_order, s.status, COALESCE(i.item_count, 0)::bigint AS item_count
    FROM statuses s
    LEFT JOIN iter_counts i ON i.status = s.status
  UNION ALL
  SELECT 2, '新品创新', '锚定机会点',
         s.status_order, s.status, COALESCE(i.item_count, 0)::bigint
    FROM statuses s
    LEFT JOIN inno_counts i ON i.status = s.status
), machine_rows AS (
  SELECT v.machine_order, v.machine_label, v.item_count
    FROM (
      SELECT count(*) FILTER (WHERE NOT COALESCE(o.backlog, true))::bigint
               AS released_count,
             count(*) FILTER (WHERE COALESCE(o.needs_review, false))::bigint
               AS review_count,
             count(*) FILTER (WHERE COALESCE(o.safety_flag, false))::bigint
               AS safety_count,
             count(*) FILTER (WHERE COALESCE(o.backlog, true))::bigint
               AS unreleased_count
        FROM voc_opportunity o
       WHERE o.opp_id LIKE 'OPP2-%'
    ) c
   CROSS JOIN LATERAL (
     VALUES (1, '已放行'::text, c.released_count),
            (2, '待复核', c.review_count),
            (3, '安全通道', c.safety_count),
            (4, '未放行', c.unreleased_count)
   ) v(machine_order, machine_label, item_count)
)
SELECT 'lifecycle'::text AS row_type,
       l.lifecycle_order, l.lifecycle, l.anchor,
       l.status_order, l.status, l.item_count,
       NULL::int AS machine_order, NULL::text AS machine_label
  FROM lifecycle_rows l
UNION ALL
SELECT 'machine', NULL::int, NULL::text, NULL::text,
       NULL::int, NULL::text, m.item_count,
       m.machine_order, m.machine_label
  FROM machine_rows m
ORDER BY row_type DESC, lifecycle_order NULLS LAST,
         status_order NULLS LAST, machine_order NULLS LAST
"""


# 新鲜度把 voc_spu_issue 与 voc_opp_nn 两处缓存一起纳入悬空分母：前者的
# opp_id、后者的两端任一已删/已合并，都算一条悬空缓存行。最近一次带
# reconciliation 的 run 同时提供页面抬头的 run/week 与账本闭合状态。
HOME2_FRESHNESS = """
WITH issue_health AS (
  SELECT count(*)::bigint AS issue_total,
         count(DISTINCT i.spu)::bigint AS spu_with_issue_count,
         count(*) FILTER (
           WHERE o.opp_id IS NULL OR o.merged_into IS NOT NULL
         )::bigint AS issue_dangling
    FROM voc_spu_issue i
    LEFT JOIN voc_opportunity o ON o.opp_id = i.opp_id
), nn_health AS (
  SELECT count(*)::bigint AS nn_total,
         count(*) FILTER (
           WHERE a.opp_id IS NULL OR a.merged_into IS NOT NULL
              OR b.opp_id IS NULL OR b.merged_into IS NOT NULL
         )::bigint AS nn_dangling
    FROM voc_opp_nn n
    LEFT JOIN voc_opportunity a ON a.opp_id = n.opp_id
    LEFT JOIN voc_opportunity b ON b.opp_id = n.neighbor_id
), latest_run AS (
  SELECT split_part(r.run_id, ':', 1) AS generation_id,
         r.week AS generation_week,
         COALESCE((r.metrics -> 'reconciliation' ->> 'complete')::boolean,
                  false) AS ledger_complete,
         -- PostgreSQL 没有 jsonb_object_length；数对象键要走 jsonb_object_keys。
         (SELECT count(*)::int
            FROM jsonb_object_keys(
              COALESCE(r.metrics -> 'reconciliation', '{}'::jsonb))) AS ledger_item_count
    FROM voc_run_log r
   WHERE r.metrics ? 'reconciliation'
   ORDER BY r.finished_at DESC NULLS LAST,
            r.started_at DESC NULLS LAST, r.run_id DESC
   LIMIT 1
), summary AS (
  SELECT (SELECT max(m.publish_time) FROM voc_message m) AS latest_publish_time,
         (SELECT count(DISTINCT to_char(m.publish_time, 'IYYY-"W"IW'))::int
            FROM voc_message m
           WHERE m.publish_time IS NOT NULL) AS coverage_weeks,
         (SELECT count(*)::bigint FROM voc_spu) AS spu_count,
         i.issue_total AS spu_issue_total,
         i.spu_with_issue_count,
         (i.issue_dangling + n.nn_dangling)::bigint AS dangling_count,
         (i.issue_total + n.nn_total)::bigint AS dangling_total,
         i.issue_dangling, n.nn_dangling
    FROM issue_health i
   CROSS JOIN nn_health n
)
SELECT s.*,
       round(s.dangling_count::numeric
             / NULLIF(s.dangling_total, 0) * 100, 1) AS dangling_pct,
       r.generation_id, r.generation_week,
       r.ledger_complete, r.ledger_item_count
  FROM summary s
  LEFT JOIN latest_run r ON true
"""


# 周柱固定输出以库内最新发布时间所在周为终点的连续 26 个 ISO 周；
# generate_series 补齐断周，模板只画 viewmodel 已算好的矩形，不在浏览器补数据。
HOME2_WEEKLY = """
WITH bounds AS (
  SELECT COALESCE(date_trunc('week', max(m.publish_time)),
                  date_trunc('week', now())) AS end_week
    FROM voc_message m
), weeks AS (
  SELECT generate_series(b.end_week - interval '25 weeks', b.end_week,
                         interval '1 week') AS week_start
    FROM bounds b
), counts AS (
  SELECT date_trunc('week', m.publish_time) AS week_start,
         count(*) FILTER (WHERE m.src_line = '社媒')::bigint AS social_count,
         count(*) FILTER (WHERE m.src_line = '电商')::bigint AS ec_count
    FROM voc_message m
   WHERE m.publish_time IS NOT NULL
   GROUP BY date_trunc('week', m.publish_time)
)
SELECT row_number() OVER (ORDER BY w.week_start)::int AS week_order,
       to_char(w.week_start, 'IYYY-"W"IW') AS week_label,
       w.week_start,
       COALESCE(c.social_count, 0)::bigint AS social_count,
       COALESCE(c.ec_count, 0)::bigint AS ec_count
  FROM weeks w
  LEFT JOIN counts c ON c.week_start = w.week_start
 ORDER BY w.week_start
"""


# 分布端点一次只查白名单校验后的一个「源 × 维度」。先按归一后的非空值聚合，
# 再动态取 Top 8，其余合并；电商×语种和客服的业务空态由 viewmodel 明确输出，
# 即使上游把整列回填成同一个「未标注」也不画误导性的 100% 环。
HOME2_DIST = """
WITH selected(src, dim) AS (
  VALUES (%s::text, %s::text)
), grouped AS (
  SELECT s.src, s.dim,
         CASE s.dim
           WHEN '语种' THEN COALESCE(NULLIF(btrim(m.lang), ''), '未标注')
           ELSE COALESCE(NULLIF(btrim(m.platform), ''), '未标注')
         END AS label,
         count(*)::bigint AS item_count
    FROM selected s
    JOIN voc_message m
      ON m.src_line = CASE s.src WHEN '社媒' THEN '社媒'
                                      WHEN '电商' THEN '电商'
                                      ELSE '客服' END
   GROUP BY s.src, s.dim,
            CASE s.dim
              WHEN '语种' THEN COALESCE(NULLIF(btrim(m.lang), ''), '未标注')
              ELSE COALESCE(NULLIF(btrim(m.platform), ''), '未标注')
            END
), ranked AS (
  SELECT g.*,
         row_number() OVER (
           ORDER BY g.item_count DESC, g.label
         )::int AS item_rank
    FROM grouped g
), output AS (
  SELECT r.src, r.dim, r.item_rank AS item_order,
         r.label, r.item_count
    FROM ranked r
   WHERE r.item_rank <= 8
  UNION ALL
  SELECT r.src, r.dim, 9, '其余', sum(r.item_count)::bigint
    FROM ranked r
   WHERE r.item_rank > 8
   GROUP BY r.src, r.dim
)
SELECT o.*,
       sum(o.item_count) OVER ()::bigint AS distribution_total
  FROM output o
 ORDER BY o.item_order
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


# /iter 始终以扩口径 voc_spu 为实体全集；status_filter 只切预设视野，
# 不再维护一套独立的“产品检索”查询。问题分子是非终态条目数，分母是
# 当前 voc_spu_issue 条目数；没有人工记录默认「考虑中」。原声计数只纳入
# social gate 判为诉求缺口/产品缺陷且尚未进入任何机会点的消息。
def _board_spus() -> str:
    return """
WITH filters AS (
  SELECT %s::text AS status_filter,
         %s::text AS keyword,
         %s::text AS spu_pattern,
         %s::text AS name_pattern,
         %s::text AS sku_pattern,
         %s::text AS category
), """ + _assignment_snapshot_ctes() + """, issue_state AS (
  SELECT i.spu, i.opp_id,
         COALESCE(m.status, '考虑中') AS status,
         m.revived_at
    FROM voc_spu_issue i
    LEFT JOIN voc_spu_issue_manual m
      ON m.spu = i.spu AND m.opp_id = i.opp_id
), issue_counts AS (
  SELECT x.spu,
         count(*)::int AS issue_count,
         count(*) FILTER (
           WHERE x.status NOT IN ('已完成', '不考虑')
         )::int AS open_issue_count,
         bool_or(x.revived_at IS NOT NULL) AS has_revived_issue
    FROM issue_state x
   GROUP BY x.spu
), recent AS (
  SELECT x.spu, count(*)::int AS recent_evi_count
    FROM issue_state x
    JOIN voc_opp_evidence oe
      ON oe.opp_id = x.opp_id AND oe.assigned_spu = x.spu
    JOIN voc_message msg ON msg.message_id = oe.message_id
   WHERE msg.publish_time >= now() - interval '60 days'
   GROUP BY x.spu
), raw_voice_counts AS (
  SELECT x.spu, count(DISTINCT x.message_id)::int AS raw_voice_count
    FROM snapshot_message_spu x
    JOIN voc_message msg ON msg.message_id = x.message_id
    JOIN voc_social_gate g ON g.message_id = msg.message_id
   WHERE g.cls IN ('诉求缺口', '产品缺陷')
     AND g.prompt_ver = (SELECT prompt_ver FROM voc_social_gate
                          ORDER BY judged_at DESC LIMIT 1)
     -- 只排除【已被本 SPU 的可见问题卡承载】的消息，不是「进过任何机会点」。
     -- 后者会让证据散在多个 SPU、每个都不足成卡阈值的机会点两头消失：
     -- 机会点列表里有，产品页 issue=0 且 raw=0。实测这类机会点 584/875。
     AND NOT EXISTS (
       SELECT 1
         FROM voc_opp_evidence oe
         JOIN voc_spu_issue i
           ON i.opp_id = oe.opp_id AND i.spu = x.spu
        WHERE oe.message_id = x.message_id
          AND oe.assigned_spu = x.spu
     )
   GROUP BY x.spu
), category_counts AS (
  SELECT '__all__'::text AS value, count(*)::int AS count
    FROM voc_spu s
  UNION ALL
  SELECT COALESCE(s.category, '未分类'), count(*)::int
    FROM voc_spu s
   GROUP BY COALESCE(s.category, '未分类')
), filter_meta AS (
  SELECT COALESCE(
           (SELECT jsonb_object_agg(c.value, c.count) FROM category_counts c),
           '{}'::jsonb
         ) AS category_facets
), scale AS (
  SELECT COALESCE(max(s.negative_evi_count), 0)::bigint
           AS max_negative_evi_count
    FROM voc_spu s
), results AS (
  SELECT s.spu, s.product_names, s.skus, s.category, s.prod_line, s.grade,
         s.launch_period, s.has_ec, s.message_count, s.negative_evi_count,
         s.positive_evi_count, s.avg_star,
         CASE WHEN s.has_ec THEN COALESCE(
           s.negative_evi_count::numeric
             / NULLIF(s.negative_evi_count + s.positive_evi_count, 0),
           0::numeric
         ) END AS negative_ratio,
         COALESCE(c.open_issue_count, 0)::int AS open_issue_count,
         COALESCE(c.issue_count, 0)::int AS issue_count,
         COALESCE(c.has_revived_issue, false) AS has_revived_issue,
         COALESCE(r.recent_evi_count, 0)::int AS recent_evi_count,
         COALESCE(v.raw_voice_count, 0)::int AS raw_voice_count
    FROM voc_spu s
   CROSS JOIN filters f
  LEFT JOIN issue_counts c ON c.spu = s.spu
  LEFT JOIN recent r ON r.spu = c.spu
  LEFT JOIN raw_voice_counts v ON v.spu = s.spu
   WHERE (f.keyword = ''
          OR s.spu ILIKE f.spu_pattern
          OR EXISTS (
            SELECT 1
              FROM unnest(COALESCE(s.product_names, ARRAY[]::text[])) AS pn(name)
             WHERE pn.name ILIKE f.name_pattern
          )
          OR EXISTS (
            SELECT 1
              FROM unnest(COALESCE(s.skus, ARRAY[]::text[])) AS sk(code)
             WHERE sk.code ILIKE f.sku_pattern
          ))
     AND (f.category = '' OR COALESCE(s.category, '未分类') = f.category)
     AND CASE f.status_filter
           WHEN 'all' THEN true
           WHEN 'revived' THEN COALESCE(c.has_revived_issue, false)
           ELSE COALESCE(c.open_issue_count, 0) > 0
         END

""" + _wilson_ctes() + """)
SELECT r.*, x.max_negative_evi_count,
       meta.category_facets
  FROM filter_meta meta
  LEFT JOIN scored r ON TRUE
  JOIN scale x ON TRUE
 CROSS JOIN (
   SELECT %s::text AS sort_key, %s::text AS sort_dir
 ) ordering
""" + _spu_order("r")


BOARD_SPUS = _board_spus()
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
    JOIN voc_opp_evidence oe
      ON oe.opp_id = k.opp_id AND oe.assigned_spu = k.spu
    JOIN voc_message msg ON msg.message_id = oe.message_id
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
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


# 新品页只有需求缺口一种业务性质，v3 的 core_tag 固定为空，列表按机会点
# 标题与证据数组织。互动、标签覆盖与品牌都从机会点证据实时聚合；品牌单独
# 拆分为 CTE，避免 unnest(brands) 把 interactions 与覆盖计数成倍放大。
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
SELECT o.opp_id, o.title, o.prod_line, o.core_tag,
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
 WHERE o.opp_id LIKE 'OPP2-%'
   AND o.opp_type = '新品创新'
   AND o.classification_state = '确定'
   AND o.merged_into IS NULL
 ORDER BY o.evi_total DESC NULLS LAST,
          o.rank_score DESC NULLS LAST,
          COALESCE(a.interactions, 0) DESC,
          o.opp_id
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
       CASE WHEN s.has_ec THEN COALESCE(
         s.negative_evi_count::numeric
           / NULLIF(s.negative_evi_count + s.positive_evi_count, 0),
         0::numeric
       ) END AS negative_ratio,
       COALESCE(c.open_issue_count, 0)::int AS open_issue_count,
       COALESCE(c.issue_count, 0)::int AS issue_count,
       md.median_negative_ratio, md.median_avg_star
  FROM voc_spu s
  JOIN medians md ON TRUE
  LEFT JOIN issue_counts c ON c.spu = s.spu
 WHERE s.spu = %s
"""


# SPU 原声完整列表与列表页计数严格读当前已发布的冻结归属。
SPU_RAW_VOICES = """
WITH target AS (
  SELECT %s::text AS spu
), """ + _assignment_snapshot_ctes() + """
SELECT msg.message_id, g.cls, NULLIF(btrim(g.claim), '') AS claim,
       COALESCE(NULLIF(btrim(msg.content_zh), ''),
                NULLIF(btrim(msg.content), ''), '—') AS content,
       msg.platform, msg.publish_time, msg.url,
       msg.message_group_id, msg.message_type,
       msg.author_name, msg.message_title,
       g.confidence, g.votes, g.prompt_ver, g.judged_at
  FROM snapshot_message_spu a
  JOIN voc_message msg ON msg.message_id = a.message_id
  JOIN voc_social_gate g ON g.message_id = msg.message_id
  JOIN target t ON t.spu = a.spu
 WHERE g.cls IN ('诉求缺口', '产品缺陷')
   AND g.prompt_ver = (SELECT prompt_ver FROM voc_social_gate
                        ORDER BY judged_at DESC LIMIT 1)
   -- 口径同上：只排除已被本 SPU 可见问题卡承载的消息。
   AND NOT EXISTS (
     SELECT 1
       FROM voc_opp_evidence oe
       JOIN voc_spu_issue i
         ON i.opp_id = oe.opp_id AND i.spu = t.spu
      WHERE oe.message_id = msg.message_id
        AND oe.assigned_spu = t.spu
   )
 ORDER BY msg.publish_time DESC NULLS LAST, msg.message_id
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
     AND o.opp_id LIKE 'OPP2-%'
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
    JOIN voc_opp_evidence oe
      ON oe.opp_id = k.opp_id AND oe.assigned_spu = k.spu
    JOIN voc_message msg ON msg.message_id = oe.message_id
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
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
    JOIN voc_opp_evidence oe
      ON oe.opp_id = k.opp_id AND oe.assigned_spu = k.spu
    JOIN voc_message msg ON msg.message_id = oe.message_id
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
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
       -- btrim 与下方 full_content 的空判保持一致：全空白片段必须回落正文，
       -- 否则页面渲染一行空白且拿不到展开（片段"真值"、full_content 为空）。
       COALESCE(NULLIF(btrim(e.snippet), ''), msg.content) AS voice_text,
       CASE
         WHEN msg.content_zh IS DISTINCT FROM msg.content THEN msg.content_zh
       END AS translation,
       -- 片段是按标签切出的单句；正句显示片段，完整原声折叠展开。
       -- 仅当正句确为片段（非整段回落）且原文另有内容时才下发，避免重复。
       CASE
         WHEN NULLIF(btrim(e.snippet), '') IS NOT NULL
              AND btrim(msg.content) IS DISTINCT FROM btrim(e.snippet)
         THEN msg.content
       END AS full_content,
       e.tax_stage, e.tax_domain, e.tax_sub, e.tax_leaf,
       msg.platform, msg.publish_time, msg.star, msg.country, msg.lang,
       msg.interactions, msg.url
  FROM voc_opp_evidence oe
  JOIN voc_evidence e
    ON e.message_id = oe.message_id AND e.seq = oe.seq
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE oe.opp_id = %s
   AND oe.assigned_spu = %s
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
SELECT o.opp_id, o.opp_type, o.prod_line, o.core_tag, o.title,
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
   AND o.opp_id LIKE 'OPP2-%'
   AND o.opp_type = '新品创新'
   AND o.classification_state = '确定'
   AND o.merged_into IS NULL
"""


# 原型只展示三条「灵感来源」，按互动与发布时间取最有代表性的原声。
INNOVATION_EVIDENCE = """
SELECT oe.message_id, oe.seq,
       COALESCE(NULLIF(btrim(e.snippet), ''), NULLIF(btrim(msg.content_zh), ''),
                msg.content) AS voice_text,
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
       o.scope_source,
       o.evi_total, o.released_at,
       COALESCE(s.spu_count, 0)::int AS spu_count
  FROM voc_opportunity o
  LEFT JOIN spread s ON s.opp_id = o.opp_id
 WHERE o.merged_into IS NULL
   AND o.opp_id LIKE 'OPP2-%'
   AND o.classification_state = '确定'
   AND o.scope_source IS DISTINCT FROM 'v3-未计算'
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
   AND o.opp_id LIKE 'OPP2-%'
   AND o.classification_state = '确定'
   AND o.opp_type = '老品迭代'
   AND m.release_date IS NOT NULL
   AND oe.assigned_spu = m.spu
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


# ---------------------------------------------------------------------------
# VOC MCP v1 read-only queries. The target database must still pass the
# startup schema gate before any of these business queries are used.
# ---------------------------------------------------------------------------

MCP_SCHEMA_OBJECTS = """
WITH relation_requirements(object_name, tier, capability) AS (
  VALUES
    ('public.voc_message', 'core', NULL::text),
    ('public.voc_evidence', 'core', NULL),
    ('public.voc_opportunity', 'core', NULL),
    ('public.voc_opp_evidence', 'core', NULL),
    ('public.voc_spu', 'core', NULL),
    ('public.voc_spu_issue', 'core', NULL),
    ('public.voc_spu_issue_manual', 'core', NULL),
    ('public.voc_opportunity_manual', 'core', NULL),
    ('public.voc_run_log', 'core', NULL),
    ('public.voc_tag_taxonomy', 'core', NULL),
    ('public.voc_board', 'core', NULL),
    ('public.voc_opp_nn', 'core', NULL),
    ('public.voc_source_probe', 'core', NULL),
    ('public.voc_assign_snapshot', 'core', NULL),
    ('public.voc_source_policy', 'optional', 'source_attribution'),
    ('public.voc_social_gate', 'optional', 'unclaimed_social')
), column_requirements(table_name, column_name, tier, capability) AS (
  VALUES
    ('voc_message', 'message_id', 'core', NULL::text),
    ('voc_message', 'src_line', 'core', NULL),
    ('voc_message', 'publish_time', 'core', NULL),
    ('voc_message', 'content', 'core', NULL),
    ('voc_message', 'content_zh', 'core', NULL),
    ('voc_message', 'retention_until', 'core', NULL),
    ('voc_evidence', 'seq', 'core', NULL),
    ('voc_evidence', 'snippet', 'core', NULL),
    ('voc_evidence', 'sentiment', 'core', NULL),
    ('voc_opportunity', 'mode_vec', 'core', NULL),
    ('voc_opportunity', 'classification_state', 'core', NULL),
    ('voc_opportunity', 'classify_rule', 'core', NULL),
    ('voc_opportunity', 'merged_into', 'core', NULL),
    ('voc_opportunity', 'rank_score', 'core', NULL),
    ('voc_opportunity', 'scope_source', 'core', NULL),
    ('voc_opp_evidence', 'assigned_spu', 'core', NULL),
    ('voc_opp_evidence', 'assignment_source', 'core', NULL),
    ('voc_opp_evidence', 'assign_run_id', 'core', NULL),
    ('voc_assign_snapshot', 'run_id', 'core', NULL),
    ('voc_assign_snapshot', 'assigned_spu', 'core', NULL),
    ('voc_assign_snapshot', 'source', 'core', NULL),
    ('voc_opportunity', 'source_lines', 'optional', 'source_attribution'),
    ('voc_opportunity', 'evi_by_source', 'optional', 'source_attribution'),
    ('voc_message', 'message_group_id', 'optional', 'social_threads'),
    ('voc_message', 'message_type', 'optional', 'social_threads'),
    ('voc_message', 'parent_id', 'optional', 'social_threads'),
    ('voc_message', 'message_title', 'optional', 'social_threads'),
    ('voc_spu_issue', 'msg_count', 'optional', 'spu_msg_count'),
    ('voc_message', 'msg_sentiment', 'optional', 'msg_sentiment')
), index_requirements(object_name, tier, capability) AS (
  VALUES ('public.ix_opp_vec', 'core', NULL::text)
), extension_requirements(object_name, tier, capability) AS (
  VALUES ('vector', 'core', NULL::text), ('pg_trgm', 'core', NULL::text)
)
SELECT 'relation:' || r.object_name AS object_name,
       r.tier, r.capability,
       (to_regclass(r.object_name) IS NOT NULL
        AND has_table_privilege(
          current_user, to_regclass(r.object_name), 'SELECT'
        )) AS present
  FROM relation_requirements r
UNION ALL
SELECT 'column:public.' || c.table_name || '.' || c.column_name,
       c.tier, c.capability,
       EXISTS (
         SELECT 1
           FROM information_schema.columns x
          WHERE x.table_schema = 'public'
            AND x.table_name = c.table_name
            AND x.column_name = c.column_name
       )
       AND has_column_privilege(
         current_user, to_regclass('public.' || c.table_name), c.column_name, 'SELECT'
       )
  FROM column_requirements c
UNION ALL
SELECT 'index:' || i.object_name, i.tier, i.capability,
       (to_regclass(i.object_name) IS NOT NULL)
  FROM index_requirements i
UNION ALL
SELECT 'extension:' || e.object_name, e.tier, e.capability,
       EXISTS (SELECT 1 FROM pg_extension x WHERE x.extname = e.object_name)
  FROM extension_requirements e
ORDER BY object_name
"""


MCP_SCHEMA_CHANNEL = """
SELECT EXISTS (
  SELECT 1
    FROM information_schema.columns
   WHERE table_schema = 'public'
     AND table_name = 'voc_opportunity'
     AND column_name = 'channel'
) AS present
"""


# Candidate definition from PRD v0.3 C2. Acceptance must revise this query after
# testing it against the real run ledger: 验收时按真库 run_log 实测修订。
MCP_DATA_AS_OF = """
SELECT r.run_id, r.week, r.finished_at
  FROM voc_run_log r
 WHERE r.stage = 'finalize'
   AND r.status = 'success'
   AND r.finished_at IS NOT NULL
   AND COALESCE((r.metrics #>> '{reconciliation,complete}')::boolean, false)
 ORDER BY r.finished_at DESC, r.run_id DESC
 LIMIT 1
"""


MCP_SOURCE_VALUES = """
SELECT p.src_line
  FROM voc_source_policy p
 ORDER BY p.src_line
"""


MCP_STATUS_CONSTRAINTS = """
SELECT CASE c.conrelid
         WHEN 'voc_opportunity_manual'::regclass THEN 'opportunity'
         WHEN 'voc_spu_issue_manual'::regclass THEN 'spu_issue'
       END AS status_scope,
       pg_get_constraintdef(c.oid) AS constraint_def
  FROM pg_constraint c
 WHERE c.contype = 'c'
   AND c.conrelid IN (
     'voc_opportunity_manual'::regclass,
     'voc_spu_issue_manual'::regclass
   )
   AND position('status' IN lower(pg_get_constraintdef(c.oid))) > 0
 ORDER BY status_scope, c.conname
"""


MCP_KNN_ITERATIVE_CONFIG = """
SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true) AS configured
"""


MCP_FIND_SEMANTIC_OPPORTUNITIES = """
WITH args AS (
  SELECT %s::vector AS query_vec,
         %s::int AS candidate_limit,
         %s::boolean AS exclude_generic,
         %s::text[] AS generic_modes,
         %s::text AS opp_type,
         %s::text AS src_line,
         %s::text AS category,
         %s::text AS spu,
         %s::text AS week_from,
         %s::text AS week_to,
         %s::text AS status,
         %s::text AS status_scope,
         %s::int AS top_k,
         %s::boolean AS iterative
), settings AS MATERIALIZED (
  SELECT CASE WHEN (SELECT iterative FROM args)
              THEN set_config('hnsw.iterative_scan', 'relaxed_order', true)
              ELSE NULL END AS configured
), knn AS MATERIALIZED (
  SELECT o.opp_id, o.opp_type, o.src_line, o.prod_line, o.category,
         o.core_tag, o.title, o.problem_mode, o.rank_score, o.evi_total,
         o.first_week, o.last_week, o.n_eff, o.scope,
         (o.mode_vec <=> a.query_vec)::double precision AS cosine_distance
    FROM voc_opportunity o
   CROSS JOIN args a
   CROSS JOIN settings h
   WHERE o.classification_state = '确定'
     AND o.merged_into IS NULL
     AND o.opp_id LIKE 'OPP2-%'
     AND o.mode_vec IS NOT NULL
     AND (
       NOT a.exclude_generic
       OR NOT (
         COALESCE(o.problem_mode, '') = ANY(a.generic_modes)
         OR rtrim(COALESCE(o.problem_mode, ''), '类') = ANY(a.generic_modes)
       )
     )
   ORDER BY o.mode_vec <=> a.query_vec
   LIMIT (SELECT candidate_limit FROM args)
), filtered AS (
  SELECT k.*,
         CASE WHEN a.status = '' THEN NULL
              WHEN a.status_scope = 'opportunity' THEN a.status
              ELSE a.status END AS status,
         CASE WHEN a.status_scope <> '' THEN a.status_scope
              WHEN k.opp_type = '新品创新' THEN 'opportunity'
              ELSE 'spu_issue' END AS status_at,
         CASE WHEN COALESCE(NULLIF(a.status_scope, ''),
                            CASE WHEN k.opp_type = '新品创新'
                                 THEN 'opportunity' ELSE 'spu_issue' END)
                   = 'spu_issue'
              THEN NULLIF(a.spu, '') END AS status_spu
    FROM knn k
   CROSS JOIN args a
   WHERE (a.opp_type = '' OR k.opp_type = a.opp_type)
     AND (a.src_line = '' OR k.src_line = a.src_line)
     AND (a.category = '' OR k.category = a.category)
     AND (a.week_from = '' OR k.last_week >= a.week_from)
     AND (a.week_to = '' OR k.first_week <= a.week_to)
     AND (a.spu = '' OR EXISTS (
       SELECT 1 FROM voc_spu_issue i
        WHERE i.opp_id = k.opp_id AND i.spu = a.spu
     ))
     AND (
       a.status = ''
       OR (a.status_scope = 'opportunity' AND k.opp_type = '新品创新'
           AND COALESCE((SELECT m.status FROM voc_opportunity_manual m
                          WHERE m.opp_id = k.opp_id), '考虑中') = a.status)
       OR (a.status_scope = 'spu_issue' AND k.opp_type = '老品迭代' AND EXISTS (
         SELECT 1
           FROM voc_spu_issue i
           LEFT JOIN voc_spu_issue_manual m
             ON m.spu = i.spu AND m.opp_id = i.opp_id
          WHERE i.opp_id = k.opp_id
            AND (a.spu = '' OR i.spu = a.spu)
            AND COALESCE(m.status, '考虑中') = a.status
       ))
     )
)
SELECT f.*, count(*) OVER ()::int AS filtered_count
  FROM filtered f
 ORDER BY f.cosine_distance, f.opp_id
 LIMIT (SELECT top_k FROM args)
"""


MCP_FIND_SEMANTIC_STATS = """
WITH args AS (
  SELECT %s::boolean AS exclude_generic,
         %s::text[] AS generic_modes,
         %s::text AS opp_type,
         %s::text AS src_line,
         %s::text AS category,
         %s::text AS spu,
         %s::text AS week_from,
         %s::text AS week_to,
         %s::text AS status,
         %s::text AS status_scope
), eligible AS (
  SELECT o.*
    FROM voc_opportunity o
   CROSS JOIN args a
   WHERE o.classification_state = '确定'
     AND o.merged_into IS NULL
     AND o.opp_id LIKE 'OPP2-%'
     AND (a.opp_type = '' OR o.opp_type = a.opp_type)
     AND (a.src_line = '' OR o.src_line = a.src_line)
     AND (a.category = '' OR o.category = a.category)
     AND (a.week_from = '' OR o.last_week >= a.week_from)
     AND (a.week_to = '' OR o.first_week <= a.week_to)
     AND (a.spu = '' OR EXISTS (
       SELECT 1 FROM voc_spu_issue i
        WHERE i.opp_id = o.opp_id AND i.spu = a.spu
     ))
     AND (
       a.status = ''
       OR (a.status_scope = 'opportunity' AND o.opp_type = '新品创新'
           AND COALESCE((SELECT m.status FROM voc_opportunity_manual m
                          WHERE m.opp_id = o.opp_id), '考虑中') = a.status)
       OR (a.status_scope = 'spu_issue' AND o.opp_type = '老品迭代' AND EXISTS (
         SELECT 1
           FROM voc_spu_issue i
           LEFT JOIN voc_spu_issue_manual m
             ON m.spu = i.spu AND m.opp_id = i.opp_id
          WHERE i.opp_id = o.opp_id
            AND (a.spu = '' OR i.spu = a.spu)
            AND COALESCE(m.status, '考虑中') = a.status
       ))
     )
)
SELECT count(*) FILTER (WHERE e.mode_vec IS NULL)::int AS excluded_no_vector,
       count(*) FILTER (
         WHERE a.exclude_generic
           AND (COALESCE(e.problem_mode, '') = ANY(a.generic_modes)
                OR rtrim(COALESCE(e.problem_mode, ''), '类') = ANY(a.generic_modes))
       )::int AS excluded_generic_mode,
       count(*) FILTER (
         WHERE e.mode_vec IS NOT NULL
           AND (NOT a.exclude_generic
                OR NOT (COALESCE(e.problem_mode, '') = ANY(a.generic_modes)
                        OR rtrim(COALESCE(e.problem_mode, ''), '类') = ANY(a.generic_modes)))
       )::int AS eligible_vector
  FROM eligible e
 CROSS JOIN args a
 GROUP BY a.exclude_generic
"""


MCP_FIND_KEYWORD_OPPORTUNITIES = """
WITH args AS (
  SELECT %s::text AS query,
         %s::text AS pattern,
         %s::text AS opp_type,
         %s::text AS src_line,
         %s::text AS category,
         %s::text AS spu,
         %s::text AS week_from,
         %s::text AS week_to,
         %s::text AS status,
         %s::text AS status_scope,
         %s::int AS top_k
)
SELECT o.opp_id, o.opp_type, o.src_line, o.prod_line, o.category,
       o.core_tag, o.title, o.problem_mode, o.rank_score, o.evi_total,
       o.first_week, o.last_week, o.n_eff, o.scope,
       ARRAY_REMOVE(ARRAY[
         CASE WHEN o.title ILIKE a.pattern ESCAPE E'\\' THEN 'title' END,
         CASE WHEN o.problem_mode ILIKE a.pattern ESCAPE E'\\' THEN 'problem_mode' END,
         CASE WHEN o.core_tag ILIKE a.pattern ESCAPE E'\\' THEN 'core_tag' END
       ], NULL) AS matched_fields,
       GREATEST(
         similarity(COALESCE(o.title, ''), a.query),
         similarity(COALESCE(o.problem_mode, ''), a.query),
         similarity(COALESCE(o.core_tag, ''), a.query)
       )::double precision AS keyword_score,
       CASE WHEN a.status_scope <> '' THEN a.status_scope
            WHEN o.opp_type = '新品创新' THEN 'opportunity'
            ELSE 'spu_issue' END AS status_at,
       CASE WHEN COALESCE(NULLIF(a.status_scope, ''),
                          CASE WHEN o.opp_type = '新品创新'
                               THEN 'opportunity' ELSE 'spu_issue' END)
                 = 'spu_issue'
            THEN NULLIF(a.spu, '') END AS status_spu,
       count(*) OVER ()::int AS available_total
  FROM voc_opportunity o
 CROSS JOIN args a
 WHERE o.classification_state = '确定'
   AND o.merged_into IS NULL
   AND o.opp_id LIKE 'OPP2-%'
   AND (o.title ILIKE a.pattern ESCAPE E'\\'
        OR o.problem_mode ILIKE a.pattern ESCAPE E'\\'
        OR o.core_tag ILIKE a.pattern ESCAPE E'\\')
   AND (a.opp_type = '' OR o.opp_type = a.opp_type)
   AND (a.src_line = '' OR o.src_line = a.src_line)
   AND (a.category = '' OR o.category = a.category)
   AND (a.week_from = '' OR o.last_week >= a.week_from)
   AND (a.week_to = '' OR o.first_week <= a.week_to)
   AND (a.spu = '' OR EXISTS (
     SELECT 1 FROM voc_spu_issue i
      WHERE i.opp_id = o.opp_id AND i.spu = a.spu
   ))
   AND (
     a.status = ''
     OR (a.status_scope = 'opportunity' AND o.opp_type = '新品创新'
         AND COALESCE((SELECT m.status FROM voc_opportunity_manual m
                        WHERE m.opp_id = o.opp_id), '考虑中') = a.status)
     OR (a.status_scope = 'spu_issue' AND o.opp_type = '老品迭代' AND EXISTS (
       SELECT 1
         FROM voc_spu_issue i
         LEFT JOIN voc_spu_issue_manual m
           ON m.spu = i.spu AND m.opp_id = i.opp_id
        WHERE i.opp_id = o.opp_id
          AND (a.spu = '' OR i.spu = a.spu)
          AND COALESCE(m.status, '考虑中') = a.status
     ))
   )
 ORDER BY (lower(COALESCE(o.title, '')) = lower(a.query)) DESC,
          keyword_score DESC, o.rank_score DESC NULLS LAST, o.opp_id
LIMIT (SELECT top_k FROM args)
"""


MCP_FIND_KEYWORD_SPUS = """
WITH args AS (
  SELECT %s::text AS query,
         %s::text AS pattern,
         %s::text AS opp_type,
         %s::text AS src_line,
         %s::text AS category,
         %s::text AS spu,
         %s::text AS week_from,
         %s::text AS week_to,
         %s::text AS status,
         %s::text AS status_scope,
         %s::int AS top_k
), matched AS (
  SELECT s.spu, s.product_names, s.skus, s.category, s.prod_line, s.grade,
         s.launch_period, s.has_ec, s.message_count, s.negative_evi_count,
         s.positive_evi_count, s.avg_star,
         ARRAY_REMOVE(ARRAY[
           CASE WHEN s.spu ILIKE a.pattern ESCAPE E'\\' THEN 'spu' END,
           CASE WHEN EXISTS (
             SELECT 1 FROM unnest(COALESCE(s.product_names, ARRAY[]::text[])) n(value)
              WHERE n.value ILIKE a.pattern ESCAPE E'\\'
           ) THEN 'product_names' END,
           CASE WHEN EXISTS (
             SELECT 1 FROM unnest(COALESCE(s.skus, ARRAY[]::text[])) k(value)
              WHERE k.value ILIKE a.pattern ESCAPE E'\\'
           ) THEN 'skus' END
         ], NULL) AS matched_fields,
         GREATEST(
           similarity(COALESCE(s.spu, ''), a.query),
           COALESCE((SELECT max(similarity(n.value, a.query))
                       FROM unnest(COALESCE(s.product_names, ARRAY[]::text[])) n(value)), 0),
           COALESCE((SELECT max(similarity(k.value, a.query))
                       FROM unnest(COALESCE(s.skus, ARRAY[]::text[])) k(value)), 0)
         )::double precision AS keyword_score,
         'spu_issue'::text AS status_at, s.spu AS status_spu
    FROM voc_spu s
   CROSS JOIN args a
   WHERE (s.spu ILIKE a.pattern ESCAPE E'\\'
          OR EXISTS (
            SELECT 1 FROM unnest(COALESCE(s.product_names, ARRAY[]::text[])) n(value)
             WHERE n.value ILIKE a.pattern ESCAPE E'\\'
          )
          OR EXISTS (
            SELECT 1 FROM unnest(COALESCE(s.skus, ARRAY[]::text[])) k(value)
             WHERE k.value ILIKE a.pattern ESCAPE E'\\'
          ))
     AND (a.category = '' OR COALESCE(s.category, '未分类') = a.category)
     AND (a.spu = '' OR s.spu = a.spu)
     AND (
       (a.opp_type = '' AND a.src_line = '' AND a.week_from = ''
        AND a.week_to = '' AND a.status = '')
       OR EXISTS (
         SELECT 1
           FROM voc_spu_issue i
           JOIN voc_opportunity o ON o.opp_id = i.opp_id
           LEFT JOIN voc_spu_issue_manual sm
             ON sm.spu = i.spu AND sm.opp_id = i.opp_id
           LEFT JOIN voc_opportunity_manual om ON om.opp_id = i.opp_id
          WHERE i.spu = s.spu
            AND o.classification_state = '确定'
            AND o.merged_into IS NULL
            AND o.opp_id LIKE 'OPP2-%'
            AND (a.opp_type = '' OR o.opp_type = a.opp_type)
            AND (a.src_line = '' OR o.src_line = a.src_line)
            AND (a.week_from = '' OR o.last_week >= a.week_from)
            AND (a.week_to = '' OR o.first_week <= a.week_to)
            AND (a.status = ''
                 OR (a.status_scope = 'spu_issue' AND o.opp_type = '老品迭代'
                     AND COALESCE(sm.status, '考虑中') = a.status)
                 OR (a.status_scope = 'opportunity' AND o.opp_type = '新品创新'
                     AND COALESCE(om.status, '考虑中') = a.status))
       )
     )
)
SELECT m.*, count(*) OVER ()::int AS available_total
  FROM matched m
 CROSS JOIN args a
 ORDER BY (lower(m.spu) = lower(a.query)) DESC,
          m.keyword_score DESC, m.message_count DESC, m.spu
 LIMIT (SELECT top_k FROM args)
"""


MCP_FIND_KEYWORD_VOICES = """
WITH args AS (
  SELECT %s::text AS query,
         %s::text AS pattern,
         %s::text AS opp_type,
         %s::text AS src_line,
         %s::text AS category,
         %s::text AS spu,
         %s::text AS week_from,
         %s::text AS week_to,
         %s::text AS status,
         %s::text AS status_scope,
         %s::int AS top_k
)
SELECT oe.opp_id AS via_opp_id, oe.message_id, oe.seq, oe.attach_week,
       msg.src_line, msg.platform, msg.publish_time, msg.country, msg.lang,
       msg.url, msg.content AS content_raw,
       msg.content_zh, e.snippet, e.snippet_raw, e.sentiment,
       similarity(COALESCE(e.snippet, ''), a.query)::double precision
         AS keyword_score,
       ARRAY['snippet']::text[] AS matched_fields,
       CASE WHEN a.status_scope <> '' THEN a.status_scope
            WHEN o.opp_type = '新品创新' THEN 'opportunity'
            ELSE 'spu_issue' END AS status_at,
       CASE WHEN COALESCE(NULLIF(a.status_scope, ''),
                          CASE WHEN o.opp_type = '新品创新'
                               THEN 'opportunity' ELSE 'spu_issue' END)
                 = 'spu_issue'
            THEN NULLIF(a.spu, '') END AS status_spu
  FROM voc_opp_evidence oe
  JOIN voc_evidence e
    ON e.message_id = oe.message_id AND e.seq = oe.seq
  JOIN voc_message msg ON msg.message_id = oe.message_id
  JOIN voc_opportunity o ON o.opp_id = oe.opp_id
 CROSS JOIN args a
 WHERE e.snippet ILIKE a.pattern ESCAPE E'\\'
   AND o.classification_state = '确定'
   AND o.merged_into IS NULL
   AND o.opp_id LIKE 'OPP2-%'
   AND (a.opp_type = '' OR o.opp_type = a.opp_type)
   AND (msg.retention_until IS NULL OR msg.retention_until >= current_date)
   AND (a.src_line = '' OR msg.src_line = a.src_line)
   AND (a.category = '' OR o.category = a.category)
   AND (a.spu = '' OR oe.assigned_spu = a.spu)
   AND (a.week_from = '' OR to_char(msg.publish_time AT TIME ZONE 'Asia/Shanghai', 'IYYY-"W"IW') >= a.week_from)
   AND (a.week_to = '' OR to_char(msg.publish_time AT TIME ZONE 'Asia/Shanghai', 'IYYY-"W"IW') <= a.week_to)
   AND (
     a.status = ''
     OR (a.status_scope = 'opportunity' AND o.opp_type = '新品创新'
         AND COALESCE((SELECT m.status FROM voc_opportunity_manual m
                        WHERE m.opp_id = o.opp_id), '考虑中') = a.status)
     OR (a.status_scope = 'spu_issue' AND o.opp_type = '老品迭代' AND EXISTS (
       SELECT 1
         FROM voc_spu_issue i
         LEFT JOIN voc_spu_issue_manual m
           ON m.spu = i.spu AND m.opp_id = i.opp_id
        WHERE i.opp_id = o.opp_id
          AND (a.spu = '' OR i.spu = a.spu)
          AND COALESCE(m.status, '考虑中') = a.status
     ))
   )
 ORDER BY keyword_score DESC, msg.publish_time DESC NULLS LAST,
          oe.message_id, oe.seq, oe.opp_id
 LIMIT (SELECT top_k FROM args)
"""


MCP_FIND_KEYWORD_VOICE_STATS = """
WITH args AS (
  SELECT %s::text AS pattern,
         %s::text AS opp_type,
         %s::text AS src_line,
         %s::text AS category,
         %s::text AS spu,
         %s::text AS week_from,
         %s::text AS week_to,
         %s::text AS status,
         %s::text AS status_scope
)
SELECT count(*) FILTER (
         WHERE msg.retention_until < current_date
       )::int AS excluded_retention,
       count(*) FILTER (
         WHERE msg.retention_until IS NULL OR msg.retention_until >= current_date
       )::int AS available
  FROM voc_opp_evidence oe
  JOIN voc_evidence e
    ON e.message_id = oe.message_id AND e.seq = oe.seq
  JOIN voc_message msg ON msg.message_id = oe.message_id
  JOIN voc_opportunity o ON o.opp_id = oe.opp_id
 CROSS JOIN args a
 WHERE e.snippet ILIKE a.pattern ESCAPE E'\\'
   AND o.classification_state = '确定'
   AND o.merged_into IS NULL
   AND o.opp_id LIKE 'OPP2-%'
   AND (a.opp_type = '' OR o.opp_type = a.opp_type)
   AND (a.src_line = '' OR msg.src_line = a.src_line)
   AND (a.category = '' OR o.category = a.category)
   AND (a.spu = '' OR oe.assigned_spu = a.spu)
   AND (a.week_from = '' OR to_char(msg.publish_time AT TIME ZONE 'Asia/Shanghai', 'IYYY-"W"IW') >= a.week_from)
   AND (a.week_to = '' OR to_char(msg.publish_time AT TIME ZONE 'Asia/Shanghai', 'IYYY-"W"IW') <= a.week_to)
   AND (
     a.status = ''
     OR (a.status_scope = 'opportunity' AND o.opp_type = '新品创新'
         AND COALESCE((SELECT m.status FROM voc_opportunity_manual m
                        WHERE m.opp_id = o.opp_id), '考虑中') = a.status)
     OR (a.status_scope = 'spu_issue' AND o.opp_type = '老品迭代' AND EXISTS (
       SELECT 1
         FROM voc_spu_issue i
         LEFT JOIN voc_spu_issue_manual m
           ON m.spu = i.spu AND m.opp_id = i.opp_id
        WHERE i.opp_id = o.opp_id
          AND (a.spu = '' OR i.spu = a.spu)
          AND COALESCE(m.status, '考虑中') = a.status
     ))
   )
"""


MCP_FIND_EXPAND_SPUS = """
WITH args AS (
  SELECT %s::text[] AS opp_ids,
         %s::text AS category,
         %s::text AS spu,
         %s::text AS status,
         %s::text AS status_scope,
         %s::int AS top_k
)
SELECT i.opp_id AS via_opp_id, i.spu, i.evi_count,
       s.product_names, s.skus, s.category, s.prod_line, s.grade,
       s.launch_period, s.has_ec,
       COALESCE(m.status, '考虑中') AS status,
       'spu_issue'::text AS status_at, i.spu AS status_spu,
       count(*) OVER ()::int AS available_total
  FROM voc_spu_issue i
  JOIN voc_spu s ON s.spu = i.spu
  LEFT JOIN voc_spu_issue_manual m
    ON m.spu = i.spu AND m.opp_id = i.opp_id
 CROSS JOIN args a
 WHERE i.opp_id = ANY(a.opp_ids)
   AND (a.category = '' OR s.category = a.category)
   AND (a.spu = '' OR i.spu = a.spu)
   AND (a.status = '' OR a.status_scope = 'opportunity'
        OR (a.status_scope = 'spu_issue'
            AND COALESCE(m.status, '考虑中') = a.status))
 ORDER BY array_position(a.opp_ids, i.opp_id), i.evi_count DESC, i.spu
 LIMIT (SELECT top_k FROM args)
"""


MCP_FIND_EXPAND_VOICES = """
WITH args AS (
  SELECT %s::text[] AS opp_ids,
         %s::text AS src_line,
         %s::text AS spu,
         %s::text AS week_from,
         %s::text AS week_to,
         %s::int AS top_k
)
SELECT oe.opp_id AS via_opp_id, oe.message_id, oe.seq, oe.attach_week,
       msg.src_line, msg.platform, msg.publish_time, msg.country, msg.lang,
       msg.url, msg.content AS content_raw, msg.content_zh,
       e.snippet, e.snippet_raw, e.sentiment
  FROM voc_opp_evidence oe
  JOIN voc_evidence e
    ON e.message_id = oe.message_id AND e.seq = oe.seq
  JOIN voc_message msg ON msg.message_id = oe.message_id
 CROSS JOIN args a
 WHERE oe.opp_id = ANY(a.opp_ids)
   AND (msg.retention_until IS NULL OR msg.retention_until >= current_date)
   AND (a.src_line = '' OR msg.src_line = a.src_line)
   AND (a.spu = '' OR oe.assigned_spu = a.spu)
   AND (a.week_from = '' OR to_char(msg.publish_time AT TIME ZONE 'Asia/Shanghai', 'IYYY-"W"IW') >= a.week_from)
   AND (a.week_to = '' OR to_char(msg.publish_time AT TIME ZONE 'Asia/Shanghai', 'IYYY-"W"IW') <= a.week_to)
 ORDER BY array_position(a.opp_ids, oe.opp_id),
          (e.sentiment = '负面') DESC,
          msg.publish_time DESC NULLS LAST, oe.message_id, oe.seq, oe.opp_id
 LIMIT (SELECT top_k FROM args)
"""


MCP_FIND_EXPAND_VOICE_STATS = """
WITH args AS (
  SELECT %s::text[] AS opp_ids,
         %s::text AS src_line,
         %s::text AS spu,
         %s::text AS week_from,
         %s::text AS week_to
)
SELECT count(*) FILTER (WHERE msg.retention_until < current_date)::int
         AS excluded_retention,
       count(*) FILTER (
         WHERE msg.retention_until IS NULL OR msg.retention_until >= current_date
       )::int AS available
  FROM voc_opp_evidence oe
  JOIN voc_message msg ON msg.message_id = oe.message_id
 CROSS JOIN args a
 WHERE oe.opp_id = ANY(a.opp_ids)
   AND (a.src_line = '' OR msg.src_line = a.src_line)
   AND (a.spu = '' OR oe.assigned_spu = a.spu)
   AND (a.week_from = '' OR to_char(msg.publish_time AT TIME ZONE 'Asia/Shanghai', 'IYYY-"W"IW') >= a.week_from)
   AND (a.week_to = '' OR to_char(msg.publish_time AT TIME ZONE 'Asia/Shanghai', 'IYYY-"W"IW') <= a.week_to)
"""


MCP_OPPORTUNITY_CORE = """
SELECT o.opp_id, o.opp_type, o.src_line, o.prod_line, o.category,
       o.category_set, o.core_tag, o.problem_mode, o.title,
       o.desc_phenomenon, o.desc_attribution, o.desc_suggestion,
       o.classification_state, o.classify_rule, o.rank_score, o.safety_flag,
       o.countries, o.product_names, o.rep_snippets,
       o.evi_total, o.evi_ec, o.evi_social, o.low_star_rate,
       o.dual_source, o.weak_evidence, o.n_eff, o.scope,
       o.first_week, o.last_week, o.released_at,
       CASE WHEN o.opp_type = '新品创新'
            THEN COALESCE(m.status, '考虑中') END AS opportunity_status,
       CASE WHEN o.opp_type = '新品创新' THEN m.owner END
         AS opportunity_owner,
       CASE WHEN o.opp_type = '新品创新' THEN m.decision_note END
         AS decision_note,
       CASE WHEN o.opp_type = '新品创新' THEN m.target_release END
         AS target_release,
       CASE WHEN o.opp_type = '新品创新' THEN m.release_date END AS release_date,
       CASE WHEN o.opp_type = '新品创新' THEN m.updated_at END
         AS status_updated_at
  FROM voc_opportunity o
  LEFT JOIN voc_opportunity_manual m ON m.opp_id = o.opp_id
 WHERE o.opp_id = %s
   AND o.classification_state = '确定'
   AND o.merged_into IS NULL
"""


MCP_OPPORTUNITY_SOURCE_DETAIL = """
SELECT o.opp_id, o.source_lines, o.evi_by_source
  FROM voc_opportunity o
 WHERE o.opp_id = %s
"""


MCP_OPPORTUNITY_STATUSES = """
WITH keys AS (
  SELECT i.spu, i.opp_id
    FROM voc_spu_issue i
   WHERE i.opp_id = %s
  UNION
  SELECT m.spu, m.opp_id
    FROM voc_spu_issue_manual m
   WHERE m.opp_id = %s
)
SELECT k.spu, k.opp_id, COALESCE(m.status, '考虑中') AS status,
       m.owner, m.decision_note, m.target_release, m.release_date,
       m.updated_at AS status_updated_at
  FROM keys k
  LEFT JOIN voc_spu_issue_manual m
    ON m.spu = k.spu AND m.opp_id = k.opp_id
 ORDER BY k.spu
"""


MCP_SPU_ISSUE_MSG_COUNTS = """
SELECT i.opp_id, i.msg_count
  FROM voc_spu_issue i
 WHERE i.spu = %s
 ORDER BY i.opp_id
"""


MCP_LIST_SPUS_DEGRADED = """
WITH issue_counts AS (
  SELECT i.spu,
         count(*)::int AS issue_count,
         count(*) FILTER (
           WHERE COALESCE(m.status, '考虑中') NOT IN ('已完成', '不考虑')
         )::int AS open_issue_count,
         bool_or(m.revived_at IS NOT NULL) AS has_revived_issue
    FROM voc_spu_issue i
    LEFT JOIN voc_spu_issue_manual m
      ON m.spu = i.spu AND m.opp_id = i.opp_id
   GROUP BY i.spu
)
SELECT s.spu, s.product_names, s.skus, s.category, s.prod_line, s.grade,
       s.launch_period, s.has_ec, s.message_count, s.negative_evi_count,
       s.positive_evi_count, s.avg_star,
       CASE WHEN s.has_ec THEN COALESCE(
         s.negative_evi_count::numeric
           / NULLIF(s.negative_evi_count + s.positive_evi_count, 0),
         0::numeric
       ) END AS negative_ratio,
       COALESCE(c.open_issue_count, 0)::int AS open_issue_count,
       COALESCE(c.issue_count, 0)::int AS issue_count,
       COALESCE(c.has_revived_issue, false) AS has_revived_issue
  FROM voc_spu s
  LEFT JOIN issue_counts c ON c.spu = s.spu
 ORDER BY s.spu
"""


MCP_VOICES_OPPORTUNITY = """
SELECT oe.opp_id, oe.message_id, oe.seq, oe.attach_week,
       msg.src_line, msg.platform, msg.publish_time, msg.country, msg.lang,
       msg.url, msg.content AS content_raw, msg.content_zh,
       msg.star, msg.interactions,
       e.snippet, e.snippet_raw, e.sentiment,
       count(*) OVER ()::int AS available_total
  FROM voc_opp_evidence oe
  JOIN voc_evidence e
    ON e.message_id = oe.message_id AND e.seq = oe.seq
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE oe.opp_id = %s
   AND (msg.retention_until IS NULL OR msg.retention_until >= current_date)
 ORDER BY (e.sentiment = '负面') DESC,
          msg.publish_time DESC NULLS LAST, oe.message_id, oe.seq
 OFFSET %s LIMIT %s
"""


MCP_VOICES_OPPORTUNITY_COUNTS = """
SELECT count(*) FILTER (WHERE msg.retention_until < current_date)::int
         AS excluded_retention,
       count(*) FILTER (
         WHERE msg.retention_until IS NULL OR msg.retention_until >= current_date
       )::int AS available
  FROM voc_opp_evidence oe
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE oe.opp_id = %s
"""


MCP_VOICE_THREAD_FIELDS = """
SELECT msg.message_id, msg.message_group_id, msg.message_type,
       msg.parent_id, msg.message_title
  FROM voc_message msg
 WHERE msg.message_id = ANY(%s::text[])
 ORDER BY msg.message_id
"""


MCP_VOICE_MSG_SENTIMENTS = """
SELECT msg.message_id, msg.msg_sentiment
  FROM voc_message msg
 WHERE msg.message_id = ANY(%s::text[])
 ORDER BY msg.message_id
"""


MCP_VOICES_UNCLAIMED = """
WITH target AS (SELECT %s::text AS spu),
""" + _assignment_snapshot_ctes() + """
SELECT msg.message_id, NULL::int AS seq, msg.src_line, msg.platform,
       msg.publish_time, msg.country, msg.lang, msg.url,
       msg.content AS content_raw, msg.content_zh, msg.star, msg.interactions,
       NULL::text AS snippet, NULL::text AS snippet_raw,
       NULL::text AS sentiment,
       g.cls, g.claim,
       g.confidence, g.votes, g.prompt_ver, g.judged_at,
       count(*) OVER ()::int AS available_total
  FROM snapshot_message_spu a
  JOIN voc_message msg ON msg.message_id = a.message_id
  JOIN voc_social_gate g ON g.message_id = msg.message_id
  JOIN target t ON t.spu = a.spu
 WHERE g.cls IN ('诉求缺口', '产品缺陷')
   AND g.prompt_ver = (
     SELECT prompt_ver FROM voc_social_gate
      ORDER BY judged_at DESC, message_id LIMIT 1
   )
   AND (msg.retention_until IS NULL OR msg.retention_until >= current_date)
   AND NOT EXISTS (
     SELECT 1
       FROM voc_opp_evidence oe
       JOIN voc_spu_issue i
         ON i.opp_id = oe.opp_id AND i.spu = t.spu
      WHERE oe.message_id = msg.message_id
        AND oe.assigned_spu = t.spu
   )
 ORDER BY msg.publish_time DESC NULLS LAST, msg.message_id
 OFFSET %s LIMIT %s
"""


MCP_VOICES_UNCLAIMED_INHERITED = """
WITH target AS (SELECT %s::text AS spu),
""" + _assignment_snapshot_ctes() + """
SELECT msg.message_id, NULL::int AS seq, msg.src_line, msg.platform,
       msg.publish_time, msg.country, msg.lang, msg.url,
       msg.content AS content_raw, msg.content_zh, msg.star, msg.interactions,
       NULL::text AS snippet, NULL::text AS snippet_raw,
       NULL::text AS sentiment,
       g.cls, g.claim,
       g.confidence, g.votes, g.prompt_ver, g.judged_at,
       count(*) OVER ()::int AS available_total
  FROM snapshot_message_spu a
  JOIN voc_message msg ON msg.message_id = a.message_id
  JOIN voc_social_gate g ON g.message_id = msg.message_id
  JOIN target t ON t.spu = a.spu
 WHERE g.cls IN ('诉求缺口', '产品缺陷')
   AND g.prompt_ver = (
     SELECT prompt_ver FROM voc_social_gate
      ORDER BY judged_at DESC, message_id LIMIT 1
   )
   AND (msg.retention_until IS NULL OR msg.retention_until >= current_date)
   AND NOT EXISTS (
     SELECT 1
       FROM voc_opp_evidence oe
       JOIN voc_spu_issue i
         ON i.opp_id = oe.opp_id AND i.spu = t.spu
      WHERE oe.message_id = msg.message_id
        AND oe.assigned_spu = t.spu
   )
 ORDER BY msg.publish_time DESC NULLS LAST, msg.message_id
 OFFSET %s LIMIT %s
"""


MCP_VOICES_UNCLAIMED_COUNTS = """
WITH target AS (SELECT %s::text AS spu),
""" + _assignment_snapshot_ctes() + """, eligible AS (
  SELECT msg.retention_until
    FROM snapshot_message_spu a
    JOIN voc_message msg ON msg.message_id = a.message_id
    JOIN voc_social_gate g ON g.message_id = msg.message_id
    JOIN target t ON t.spu = a.spu
   WHERE g.cls IN ('诉求缺口', '产品缺陷')
     AND g.prompt_ver = (
       SELECT prompt_ver FROM voc_social_gate
        ORDER BY judged_at DESC, message_id LIMIT 1
     )
     AND NOT EXISTS (
       SELECT 1
         FROM voc_opp_evidence oe
         JOIN voc_spu_issue i
           ON i.opp_id = oe.opp_id AND i.spu = t.spu
        WHERE oe.message_id = msg.message_id
          AND oe.assigned_spu = t.spu
     )
)
SELECT count(*) FILTER (WHERE retention_until < current_date)::int
         AS excluded_retention,
       count(*) FILTER (
         WHERE retention_until IS NULL OR retention_until >= current_date
       )::int AS available
  FROM eligible
"""


MCP_VOICES_UNCLAIMED_COUNTS_INHERITED = """
WITH target AS (SELECT %s::text AS spu),
""" + _assignment_snapshot_ctes() + """, eligible AS (
  SELECT msg.retention_until
    FROM snapshot_message_spu a
    JOIN voc_message msg ON msg.message_id = a.message_id
    JOIN voc_social_gate g ON g.message_id = msg.message_id
    JOIN target t ON t.spu = a.spu
   WHERE g.cls IN ('诉求缺口', '产品缺陷')
     AND g.prompt_ver = (
       SELECT prompt_ver FROM voc_social_gate
        ORDER BY judged_at DESC, message_id LIMIT 1
     )
     AND NOT EXISTS (
       SELECT 1
         FROM voc_opp_evidence oe
         JOIN voc_spu_issue i
           ON i.opp_id = oe.opp_id AND i.spu = t.spu
        WHERE oe.message_id = msg.message_id
          AND oe.assigned_spu = t.spu
     )
)
SELECT count(*) FILTER (WHERE retention_until < current_date)::int
         AS excluded_retention,
       count(*) FILTER (
         WHERE retention_until IS NULL OR retention_until >= current_date
       )::int AS available
  FROM eligible
"""


MCP_BUNDLE_SPU_VOICES_BASE = """
SELECT oe.opp_id, oe.message_id, oe.seq, oe.attach_week,
       msg.src_line, msg.platform, msg.publish_time, msg.country, msg.lang,
       msg.url, msg.content AS content_raw, msg.content_zh,
       e.snippet, e.snippet_raw, e.sentiment
  FROM voc_spu_issue i
  JOIN voc_opp_evidence oe ON oe.opp_id = i.opp_id
  JOIN voc_evidence e
    ON e.message_id = oe.message_id AND e.seq = oe.seq
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE i.spu = %s
   AND oe.assigned_spu = i.spu
   AND (msg.retention_until IS NULL OR msg.retention_until >= current_date)
 ORDER BY (e.sentiment = '负面') DESC,
          msg.publish_time DESC NULLS LAST, oe.message_id, oe.seq, oe.opp_id
 LIMIT %s
"""


MCP_BUNDLE_SPU_VOICES_INHERITED = """
SELECT oe.opp_id, oe.message_id, oe.seq, oe.attach_week,
       msg.src_line, msg.platform, msg.publish_time, msg.country, msg.lang,
       msg.url, msg.content AS content_raw, msg.content_zh,
       e.snippet, e.snippet_raw, e.sentiment
  FROM voc_spu_issue i
  JOIN voc_opp_evidence oe ON oe.opp_id = i.opp_id
  JOIN voc_evidence e
    ON e.message_id = oe.message_id AND e.seq = oe.seq
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE i.spu = %s
   AND oe.assigned_spu = i.spu
   AND (msg.retention_until IS NULL OR msg.retention_until >= current_date)
 ORDER BY (e.sentiment = '负面') DESC,
          msg.publish_time DESC NULLS LAST, oe.message_id, oe.seq, oe.opp_id
 LIMIT %s
"""


MCP_BUNDLE_SPU_VOICE_COUNTS_BASE = """
SELECT count(*) FILTER (
         WHERE msg.retention_until IS NULL OR msg.retention_until >= current_date
       )::int AS available,
       count(*) FILTER (WHERE msg.retention_until < current_date)::int
         AS excluded_retention
  FROM voc_spu_issue i
  JOIN voc_opp_evidence oe ON oe.opp_id = i.opp_id
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE i.spu = %s
   AND oe.assigned_spu = i.spu
"""


MCP_BUNDLE_SPU_VOICE_COUNTS_INHERITED = """
SELECT count(*) FILTER (
         WHERE msg.retention_until IS NULL OR msg.retention_until >= current_date
       )::int AS available,
       count(*) FILTER (WHERE msg.retention_until < current_date)::int
         AS excluded_retention
  FROM voc_spu_issue i
  JOIN voc_opp_evidence oe ON oe.opp_id = i.opp_id
  JOIN voc_message msg ON msg.message_id = oe.message_id
 WHERE i.spu = %s
   AND oe.assigned_spu = i.spu
"""


MCP_TAXONOMY = """
WITH latest AS (
  SELECT max(t.week) AS week FROM voc_tag_taxonomy t
)
SELECT t.tag, t.prod_line, t.tax_path, t.tax_l1, t.is_product, t.is_scene,
       t.week
  FROM voc_tag_taxonomy t
  JOIN latest x ON x.week = t.week
 WHERE COALESCE(t.tax_path, '') LIKE %s ESCAPE E'\\'
 ORDER BY t.prod_line, t.tax_path NULLS LAST, t.tag
"""

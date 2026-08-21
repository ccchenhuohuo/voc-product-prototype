-- 032：SPU 问题层双物化对象 + 原名兼容视图。
--
-- voc_spu 容器保持 025 定义与刷新方式不变。问题层分成 v2/v3 两个 MV，
-- 原对象名改为普通视图且初始指向 v2；切换由验收环节单独执行。
BEGIN;

SET LOCAL search_path = public, pg_temp;

DO $guard$
BEGIN
  IF current_user <> 'voc_admin' THEN
    RAISE EXCEPTION '032 必须以 voc_admin 执行，当前为 %', current_user;
  END IF;
END
$guard$;

LOCK TABLE public.voc_opportunity, public.voc_opp_evidence,
                  public.voc_message, public.voc_evidence
  IN SHARE ROW EXCLUSIVE MODE;

-- 允许迁移事务失败后重跑：按当前 relkind 删除兼容名，再重建两个派生对象。
DO $drop_compat$
DECLARE
  object_kind "char";
BEGIN
  SELECT c.relkind INTO object_kind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relname = 'voc_spu_issue';
  IF object_kind = 'm' THEN
    EXECUTE 'DROP MATERIALIZED VIEW public.voc_spu_issue';
  ELSIF object_kind = 'v' THEN
    EXECUTE 'DROP VIEW public.voc_spu_issue';
  ELSIF object_kind IS NOT NULL THEN
    RAISE EXCEPTION 'voc_spu_issue 对象类型异常：%', object_kind;
  END IF;
END
$drop_compat$;

DROP MATERIALIZED VIEW IF EXISTS public.voc_spu_issue_v2;
DROP MATERIALIZED VIEW IF EXISTS public.voc_spu_issue_v3;

-- v2：026 的消息数组展开定义，增加 OPP-* 命名空间边界，避免 shadow 刷新
-- 把 OPP2-* 行混进仍指向 v2 的兼容读路径。
CREATE MATERIALIZED VIEW public.voc_spu_issue_v2 AS
WITH attached AS (
  SELECT DISTINCT
         NULLIF(btrim(s.spu), '') AS spu,
         oe.opp_id,
         oe.message_id,
         oe.seq,
         oe.attach_week,
         e.tax_path
    FROM public.voc_opp_evidence oe
    JOIN public.voc_opportunity o ON o.opp_id = oe.opp_id
    JOIN public.voc_message m ON m.message_id = oe.message_id
    JOIN public.voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
   CROSS JOIN LATERAL unnest(
          COALESCE(m.spu, ARRAY[]::text[])
       || COALESCE(m.spu_inherited, ARRAY[]::text[])
        ) AS s(spu)
   WHERE o.opp_id LIKE 'OPP-%'
     AND o.opp_type = '老品迭代'
     AND o.classification_state = '确定'
     AND NULLIF(btrim(s.spu), '') IS NOT NULL
)
SELECT spu,
       opp_id,
       count(*) AS evi_count,
       count(DISTINCT message_id)::int AS msg_count,
       mode() WITHIN GROUP (ORDER BY tax_path)
         FILTER (WHERE tax_path IS NOT NULL) AS tax_path,
       min(attach_week) AS first_attach_week,
       max(attach_week) AS last_attach_week
  FROM attached
 GROUP BY spu, opp_id
HAVING count(DISTINCT message_id) >= 2;

-- v3：关系行自身携带冻结归属，禁止再从消息数组二次展开。
CREATE MATERIALIZED VIEW public.voc_spu_issue_v3 AS
WITH attached AS (
  SELECT DISTINCT
         NULLIF(btrim(oe.assigned_spu), '') AS spu,
         oe.opp_id,
         oe.message_id,
         oe.seq,
         oe.attach_week,
         e.tax_path
    FROM public.voc_opp_evidence oe
    JOIN public.voc_opportunity o ON o.opp_id = oe.opp_id
    JOIN public.voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
   WHERE o.opp_id LIKE 'OPP2-%'
     AND o.opp_type = '老品迭代'
     AND o.classification_state = '确定'
     AND oe.assigned_spu IS NOT NULL
     AND oe.assigned_spu = o.core_tag
)
SELECT spu,
       opp_id,
       count(*) AS evi_count,
       count(DISTINCT message_id)::int AS msg_count,
       mode() WITHIN GROUP (ORDER BY tax_path)
         FILTER (WHERE tax_path IS NOT NULL) AS tax_path,
       min(attach_week) AS first_attach_week,
       max(attach_week) AS last_attach_week
  FROM attached
 GROUP BY spu, opp_id
HAVING count(DISTINCT message_id) >= 2;

CREATE UNIQUE INDEX ux_voc_spu_issue_v2_key
  ON public.voc_spu_issue_v2 (spu, opp_id);
CREATE INDEX ix_voc_spu_issue_v2_opp
  ON public.voc_spu_issue_v2 (opp_id);
CREATE UNIQUE INDEX ux_voc_spu_issue_v3_key
  ON public.voc_spu_issue_v3 (spu, opp_id);
CREATE INDEX ix_voc_spu_issue_v3_opp
  ON public.voc_spu_issue_v3 (opp_id);

-- 初始兼容读路径固定指向 v2；本迁移绝不代替验收方切换。
CREATE VIEW public.voc_spu_issue AS
SELECT spu, opp_id, evi_count, msg_count, tax_path,
       first_attach_week, last_attach_week
  FROM public.voc_spu_issue_v2;

GRANT SELECT ON public.voc_spu_issue_v2, public.voc_spu_issue_v3,
                public.voc_spu_issue
  TO voc_reader, voc_human, voc_writer;

CREATE OR REPLACE FUNCTION public.voc_refresh_spu_layer() RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  bad_v2 bigint;
  bad_v3 bigint;
BEGIN
  -- voc_spu 是独立容器 MV，保持原样刷新。
  REFRESH MATERIALIZED VIEW public.voc_spu;
  REFRESH MATERIALIZED VIEW public.voc_spu_issue_v2;
  REFRESH MATERIALIZED VIEW public.voc_spu_issue_v3;

  SELECT count(*) INTO bad_v2
    FROM public.voc_spu_issue_v2 WHERE msg_count < 2;
  SELECT count(*) INTO bad_v3
    FROM public.voc_spu_issue_v3 WHERE msg_count < 2;
  IF bad_v2 <> 0 OR bad_v3 <> 0 THEN
    RAISE EXCEPTION '032 刷新自检失败：v2=% / v3=% 条 msg_count<2',
                    bad_v2, bad_v3;
  END IF;

  -- 战略管道本次不做：v3 行明确清空旧 scope，不参与 v2 共性度回填。
  UPDATE public.voc_opportunity
     SET n_eff = NULL, scope = NULL
   WHERE scope_source = 'v3-未计算'
     AND (n_eff IS NOT NULL OR scope IS NOT NULL);

  -- v2 历史行维持 026 的线程封顶口径，供兼容视图灰度期继续读取。
  WITH attached AS (
    SELECT DISTINCT
           oe.opp_id,
           NULLIF(btrim(s.spu), '') AS spu,
           COALESCE(m.message_group_id, oe.message_id) AS thread
      FROM public.voc_opp_evidence oe
      JOIN public.voc_opportunity o ON o.opp_id = oe.opp_id
      JOIN public.voc_message m ON m.message_id = oe.message_id
     CROSS JOIN LATERAL unnest(
            COALESCE(m.spu, ARRAY[]::text[])
         || COALESCE(m.spu_inherited, ARRAY[]::text[])
          ) AS s(spu)
     WHERE o.opp_id LIKE 'OPP-%'
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
     WHERE o.opp_id LIKE 'OPP-%'
       AND o.scope_source IS DISTINCT FROM 'v3-未计算'
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

REVOKE ALL ON FUNCTION public.voc_refresh_spu_layer() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.voc_refresh_spu_layer() TO voc_writer;

-- 021 的旧实现会 TRUNCATE 整张最近邻缓存并扫描所有生命周期。shadow 期间
-- 必须保留 OPP-* 的 v2 缓存，只重建本代 OPP2-* 行，且候选邻居也不得跨代。
CREATE OR REPLACE FUNCTION public.voc_refresh_opp_nn() RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  DELETE FROM public.voc_opp_nn
   WHERE opp_id LIKE 'OPP2-%';

  WITH active AS (
    SELECT o.opp_id, o.opp_type AS lifecycle, o.title, o.mode_vec
      FROM public.voc_opportunity o
     WHERE o.opp_id LIKE 'OPP2-%'
       AND o.classification_state = '确定'
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

REVOKE ALL ON FUNCTION public.voc_refresh_opp_nn() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.voc_refresh_opp_nn() TO voc_writer;

-- 首次建立两个 MV 后统一刷新；兼容视图只读，不参与 REFRESH。
SELECT public.voc_refresh_spu_layer();

DO $selfcheck$
DECLARE
  v2_signature text[];
  v3_signature text[];
  compat_signature text[];
  bad_assignment bigint;
BEGIN
  SELECT array_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod)
                   ORDER BY a.attnum)
    INTO v2_signature
    FROM pg_attribute a
   WHERE a.attrelid = 'public.voc_spu_issue_v2'::regclass
     AND a.attnum > 0 AND NOT a.attisdropped;
  SELECT array_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod)
                   ORDER BY a.attnum)
    INTO v3_signature
    FROM pg_attribute a
   WHERE a.attrelid = 'public.voc_spu_issue_v3'::regclass
     AND a.attnum > 0 AND NOT a.attisdropped;
  SELECT array_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod)
                   ORDER BY a.attnum)
    INTO compat_signature
    FROM pg_attribute a
   WHERE a.attrelid = 'public.voc_spu_issue'::regclass
     AND a.attnum > 0 AND NOT a.attisdropped;

  IF v2_signature IS DISTINCT FROM v3_signature
     OR v2_signature IS DISTINCT FROM compat_signature THEN
    RAISE EXCEPTION '032 列契约不一致：v2=% / v3=% / view=%',
                    v2_signature, v3_signature, compat_signature;
  END IF;

  SELECT count(*) INTO bad_assignment
    FROM public.voc_opp_evidence oe
    JOIN public.voc_opportunity o ON o.opp_id = oe.opp_id
   WHERE o.opp_id LIKE 'OPP2-%'
     AND o.opp_type = '老品迭代'
     AND (oe.assigned_spu IS NULL
          OR oe.assignment_source IS NULL
          OR oe.assign_run_id IS NULL
          OR oe.assigned_spu IS DISTINCT FROM o.core_tag);
  IF bad_assignment <> 0 THEN
    RAISE EXCEPTION '032 归属自检失败：v3 老品关系 % 行不完整', bad_assignment;
  END IF;
END
$selfcheck$;

COMMIT;

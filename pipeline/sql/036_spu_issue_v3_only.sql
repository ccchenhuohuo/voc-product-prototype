-- 036：SPU 问题层单代化，取代 032 的双物化结构。
--
-- 032 的前提是「v2 数据在 OPP-* 命名空间、v3 在 OPP2-*，两代并存需各自物化」。
-- 2026-08-21 核实生产库：OPP-* 零张卡，全部 1,970 张 v2 卡都在 OPP2-* 下，
-- 且其关系行 assigned_spu 全为 NULL。故 032 的 _v2（过滤 OPP-*）恒为空，
-- 其尾部「OPP2-* 老品关系归属完整」自检对当时的库必然抛异常——它对着一个
-- 不存在的状态设计，无法应用。
--
-- 业主 2026-08-21 决议：v3 全量刷新后 v2 架构直接废弃，不做并存灰度、不做回滚。
-- 因此本迁移只保留 v3 一份物化视图，voc_spu_issue 改为指向它的普通视图。
-- 前置：机会点层已清空（本迁移的归属自检依赖这一点）。
BEGIN;

SET LOCAL search_path = public, pg_temp;

DO $guard$
BEGIN
  IF current_user <> 'voc_admin' THEN
    RAISE EXCEPTION '036 必须以 voc_admin 执行，当前为 %', current_user;
  END IF;
END
$guard$;

LOCK TABLE public.voc_opportunity, public.voc_opp_evidence, public.voc_evidence
  IN SHARE ROW EXCLUSIVE MODE;

-- 可重跑：按当前 relkind 拆掉兼容名，再重建。
DO $drop_compat$
DECLARE
  object_kind "char";
BEGIN
  SELECT c.relkind INTO object_kind
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
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

-- v3 定义：归属来自关系行冻结的 assigned_spu，不再展开消息数组。
-- 列契约与 026 完全一致，前端与战略层无需改查询。
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
   WHERE o.opp_type = '老品迭代'
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

CREATE UNIQUE INDEX ux_voc_spu_issue_v3_key ON public.voc_spu_issue_v3 (spu, opp_id);
CREATE INDEX ix_voc_spu_issue_v3_opp ON public.voc_spu_issue_v3 (opp_id);

-- 兼容名：前端、战略层与既有 SQL 一律读这个名字。
CREATE VIEW public.voc_spu_issue AS SELECT * FROM public.voc_spu_issue_v3;

COMMENT ON VIEW public.voc_spu_issue IS
  '兼容名，指向 voc_spu_issue_v3（036 起单代化，v2 已废弃）。';

-- 刷新函数：只剩 v3 一份；voc_spu 容器保持 025 的独立刷新。
CREATE OR REPLACE FUNCTION public.voc_refresh_spu_layer() RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  bad_v3 bigint;
BEGIN
  REFRESH MATERIALIZED VIEW public.voc_spu;
  REFRESH MATERIALIZED VIEW public.voc_spu_issue_v3;

  SELECT count(*) INTO bad_v3
    FROM public.voc_spu_issue_v3 WHERE msg_count < 2;
  IF bad_v3 <> 0 THEN
    RAISE EXCEPTION '036 刷新自检失败：% 条 msg_count<2', bad_v3;
  END IF;
END
$$;

GRANT SELECT ON public.voc_spu_issue_v3 TO voc_reader, voc_human, voc_writer;
GRANT SELECT ON public.voc_spu_issue    TO voc_reader, voc_human, voc_writer;

-- 归属自检：老品关系行必须携带完整且与 core_tag 一致的冻结归属。
-- 机会点层已清空时该计数为 0；日后重跑本迁移即成为真正的契约检查。
DO $selfcheck$
DECLARE
  bad_assignment bigint;
BEGIN
  SELECT count(*) INTO bad_assignment
    FROM public.voc_opp_evidence oe
    JOIN public.voc_opportunity o ON o.opp_id = oe.opp_id
   WHERE o.opp_type = '老品迭代'
     AND (oe.assigned_spu IS NULL
          OR oe.assignment_source IS NULL
          OR oe.assign_run_id IS NULL
          OR oe.assigned_spu IS DISTINCT FROM o.core_tag);
  IF bad_assignment <> 0 THEN
    RAISE EXCEPTION '036 归属自检失败：老品关系 % 行归属不完整', bad_assignment;
  END IF;
END
$selfcheck$;

COMMIT;

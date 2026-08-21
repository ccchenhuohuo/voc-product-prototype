-- 030：统一 Python / SQL 的 SPU 判定口径，并重分类存量机会点。
--
-- 015 中的 has_spu 是迁移语句内部的一次性 CTE 列，不是可替换对象；历史
-- 迁移保持不动。本迁移先建立唯一函数，再用同一函数重算当前存量数据。
BEGIN;

SET LOCAL search_path = public, pg_temp;

DO $guard$
BEGIN
  IF current_user <> 'voc_admin' THEN
    RAISE EXCEPTION '030 必须以 voc_admin 执行，当前为 %', current_user;
  END IF;
END
$guard$;

CREATE OR REPLACE FUNCTION voc_has_spu(p_message_id text)
RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT EXISTS (
    SELECT 1 FROM voc_message m
     WHERE m.message_id = p_message_id
       AND COALESCE(cardinality(m.spu), 0)
         + COALESCE(cardinality(m.spu_inherited), 0) > 0)
$$;

COMMENT ON FUNCTION voc_has_spu(text) IS
  '消息是否具有事实或根帖继承 SPU；分类、自检与快照准备的唯一 SQL 口径。';

LOCK TABLE public.voc_opportunity, public.voc_opp_evidence,
                  public.voc_message, public.voc_source_policy
  IN SHARE ROW EXCLUSIVE MODE;

WITH evidence_summary AS (
  SELECT o.opp_id,
         count(oe.opp_id)::int AS evidence_count,
         COALESCE(bool_or(public.voc_has_spu(oe.message_id))
                    FILTER (WHERE oe.message_id IS NOT NULL), false) AS has_spu,
         COALESCE(bool_or(NOT p.requires_spu)
                    FILTER (WHERE oe.message_id IS NOT NULL), false)
           AS has_non_spu_source
    FROM public.voc_opportunity o
    LEFT JOIN public.voc_opp_evidence oe ON oe.opp_id = o.opp_id
    LEFT JOIN public.voc_message m ON m.message_id = oe.message_id
    LEFT JOIN public.voc_source_policy p ON p.src_line = m.src_line
   GROUP BY o.opp_id
), classified AS (
  SELECT opp_id,
         evidence_count,
         CASE
           WHEN evidence_count = 0 THEN NULL
           WHEN has_spu THEN '老品迭代'
           WHEN has_non_spu_source THEN '新品创新'
           ELSE NULL
         END AS opp_type,
         CASE
           WHEN evidence_count = 0 THEN '无效'
           WHEN has_spu OR has_non_spu_source THEN '确定'
           ELSE '无效'
         END AS classification_state,
         CASE
           WHEN evidence_count = 0 THEN 'R0'
           WHEN has_spu THEN 'R1'
           WHEN has_non_spu_source THEN 'R2'
           ELSE 'R3'
         END AS classify_rule
    FROM evidence_summary
)
UPDATE public.voc_opportunity o
   SET evi_total = c.evidence_count,
       opp_type = c.opp_type,
       classification_state = c.classification_state,
       classify_rule = c.classify_rule
  FROM classified c
 WHERE c.opp_id = o.opp_id
   AND (o.evi_total IS DISTINCT FROM c.evidence_count
        OR o.opp_type IS DISTINCT FROM c.opp_type
        OR o.classification_state IS DISTINCT FROM c.classification_state
        OR o.classify_rule IS DISTINCT FROM c.classify_rule);

-- 自检一：函数必须与 Python _has_spu 的「事实数组 ∪ 继承数组非空」定义等价。
DO $selfcheck$
DECLARE
  opposite_rows bigint;
BEGIN
  SELECT count(*) INTO opposite_rows
    FROM public.voc_message m
   WHERE public.voc_has_spu(m.message_id) IS DISTINCT FROM
         (COALESCE(cardinality(m.spu), 0)
          + COALESCE(cardinality(m.spu_inherited), 0) > 0);
  IF opposite_rows <> 0 THEN
    RAISE EXCEPTION '030 自检失败：Python/SQL SPU 判定相反 % 行', opposite_rows;
  END IF;
END
$selfcheck$;

-- 自检二：重分类结果不得仍与上述统一口径相反；非零即回滚，不把一条仅供
-- 人眼查看的计数当作保护。
DO $classification_check$
DECLARE
  classification_mismatch_rows bigint;
BEGIN
  WITH expected AS (
    SELECT o.opp_id,
           count(oe.opp_id)::int AS evidence_count,
           COALESCE(bool_or(public.voc_has_spu(oe.message_id))
                      FILTER (WHERE oe.message_id IS NOT NULL), false) AS has_spu,
           COALESCE(bool_or(NOT p.requires_spu)
                      FILTER (WHERE oe.message_id IS NOT NULL), false)
             AS has_non_spu_source
      FROM public.voc_opportunity o
      LEFT JOIN public.voc_opp_evidence oe ON oe.opp_id = o.opp_id
      LEFT JOIN public.voc_message m ON m.message_id = oe.message_id
      LEFT JOIN public.voc_source_policy p ON p.src_line = m.src_line
     GROUP BY o.opp_id
  )
  SELECT count(*) INTO classification_mismatch_rows
    FROM expected e
    JOIN public.voc_opportunity o USING (opp_id)
   WHERE o.classify_rule IS DISTINCT FROM CASE
           WHEN e.evidence_count = 0 THEN 'R0'
           WHEN e.has_spu THEN 'R1'
           WHEN e.has_non_spu_source THEN 'R2'
           ELSE 'R3'
         END;

  IF classification_mismatch_rows <> 0 THEN
    RAISE EXCEPTION '030 自检失败：重分类后仍有 % 行口径相反',
                    classification_mismatch_rows;
  END IF;
END
$classification_check$;

REVOKE ALL ON FUNCTION voc_has_spu(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION voc_has_spu(text) TO voc_writer, voc_human, voc_reader;

COMMIT;

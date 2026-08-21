-- 033：单写者、幂等的 v3 G5 归属快照准备与收尾指纹校验。
BEGIN;

SET LOCAL search_path = public, pg_temp;

DO $guard$
BEGIN
  IF current_user <> 'voc_admin' THEN
    RAISE EXCEPTION '033 必须以 voc_admin 执行，当前为 %', current_user;
  END IF;
END
$guard$;

CREATE OR REPLACE FUNCTION public.voc_prepare_assign_snapshot(p_run_id text)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  existing_rows bigint;
  log_row_exists boolean;
  has_snapshot_log boolean;
  expected_rows bigint;
  expected_facts bigint;
  expected_fingerprint text;
  row_count bigint;
  distinct_fact_count bigint;
  content_fingerprint text;
BEGIN
  IF NULLIF(btrim(p_run_id), '') IS NULL THEN
    RAISE EXCEPTION 'p_run_id 不得为空';
  END IF;

  -- 同一 run_id 的所有准备者先串行；第二个写者进入后只读既有结果。
  PERFORM pg_advisory_xact_lock(hashtext(p_run_id));

  SELECT count(*) INTO existing_rows
    FROM public.voc_assign_snapshot s
   WHERE s.run_id = p_run_id;

  SELECT count(*) > 0,
         COALESCE(bool_or(r.metrics ? 'assign_snapshot'), false)
    INTO log_row_exists, has_snapshot_log
    FROM public.voc_run_log r
   WHERE r.run_id = p_run_id || ':assign_snapshot';

  -- 幂等重试不能重写原指纹，否则被篡改的快照会在第二次 prepare 时取得一份
  -- 新基线而逃过 finalize。已有指纹时只验证并直接返回；即使快照为空也适用。
  IF has_snapshot_log THEN
    SELECT (r.metrics -> 'assign_snapshot' ->> 'row_count')::bigint,
           (r.metrics -> 'assign_snapshot' ->> 'distinct_fact_count')::bigint,
           r.metrics -> 'assign_snapshot' ->> 'content_fingerprint'
      INTO expected_rows, expected_facts, expected_fingerprint
      FROM public.voc_run_log r
     WHERE r.run_id = p_run_id || ':assign_snapshot';

    SELECT count(*),
           count(DISTINCT (message_id, seq)),
           md5(COALESCE(
             string_agg(
               format('%s:%s:%s:%s:%s', length(message_id), message_id,
                      seq, assigned_spu, source),
               E'\x1e' ORDER BY message_id, seq, assigned_spu, source
             ),
             ''
           ))
      INTO row_count, distinct_fact_count, content_fingerprint
      FROM public.voc_assign_snapshot
     WHERE run_id = p_run_id;

    IF expected_rows IS NULL OR expected_facts IS NULL
       OR expected_fingerprint IS NULL
       OR (row_count, distinct_fact_count, content_fingerprint)
            IS DISTINCT FROM
          (expected_rows, expected_facts, expected_fingerprint) THEN
      RAISE EXCEPTION 'run_id=% 已有归属快照与原指纹不一致', p_run_id;
    END IF;
    RETURN row_count;
  END IF;

  IF log_row_exists THEN
    RAISE EXCEPTION 'run_id=% 的快照日志缺少 assign_snapshot 指纹', p_run_id;
  END IF;
  IF existing_rows <> 0 THEN
    RAISE EXCEPTION 'run_id=% 已有 % 行快照但没有原始指纹',
                    p_run_id, existing_rows;
  END IF;

  -- 第一次准备是唯一写入点；运行角色没有表级 INSERT 权限。
  IF existing_rows = 0 THEN
    WITH assignment_candidates AS (
      SELECT e.message_id,
             e.seq,
             NULLIF(btrim(a.assigned_spu), '') AS assigned_spu,
             a.source,
             m.message_group_id AS group_id
        FROM public.voc_evidence e
        JOIN public.voc_message m ON m.message_id = e.message_id
        LEFT JOIN public.voc_social_gate g ON g.message_id = m.message_id
       CROSS JOIN LATERAL (
         SELECT raw.assigned_spu, 'fact'::text AS source
           FROM unnest(COALESCE(m.spu, ARRAY[]::text[])) raw(assigned_spu)
         UNION ALL
         SELECT raw.assigned_spu, 'root'::text AS source
           FROM unnest(COALESCE(m.spu_inherited, ARRAY[]::text[])) raw(assigned_spu)
       ) a
       WHERE NULLIF(btrim(a.assigned_spu), '') IS NOT NULL
         AND (
           (
             m.src_line = '电商'
             AND a.source = 'fact'
             AND e.is_product
             AND e.sentiment = '负面'
             AND NULLIF(btrim(e.snippet), '') IS NOT NULL
           )
           OR
           (
             m.src_line = '社媒'
             -- G1/G1b：消息与同组根帖均不得出现白名单外品牌。
             AND NOT EXISTS (
               SELECT 1 FROM unnest(m.brands) b
                WHERE b <> ALL(ARRAY['VIJIM','宙比','小隼']::text[])
             )
             AND NOT EXISTS (
               SELECT 1 FROM public.voc_message parent
                WHERE parent.message_group_id = m.message_group_id
                  AND parent.message_type IN ('帖子','视频')
                  AND EXISTS (
                    SELECT 1 FROM unnest(parent.brands) b2
                     WHERE b2 <> ALL(ARRAY['VIJIM','宙比','小隼']::text[])
                  )
             )
             -- G2/G3 与生产 generation_pool 的冻结配置一致。
             AND NOT (COALESCE(m.content_type, ARRAY[]::text[])
                      && ARRAY['产品种草广告','产品评测','竞品拉踩','二手转让']::text[])
             AND COALESCE(m.content_type, ARRAY[]::text[])
                      && ARRAY['用户咨询','用户使用体验']::text[]
             AND (m.author_name IS NULL
                  OR m.author_name !~* '(VIJIM|ULANZI|优篮子|宙比|JOBY|小隼|FALCAM)')
             AND COALESCE(NULLIF(btrim(e.snippet), ''),
                          NULLIF(btrim(m.content), '')) IS NOT NULL
             -- warm_value_gate 已把 G4 判定物化；无价值与诉求过泛不进 G5。
             AND g.cls IN ('诉求缺口', '产品缺陷')
             AND (
               g.cls <> '诉求缺口'
               OR (
                 NULLIF(btrim(g.claim), '') IS NOT NULL
                 AND (
                   g.claim !~ '(某款|某个|待发布)'
                   OR regexp_replace(
                        regexp_replace(
                          g.claim,
                          '(需要|缺少|希望|新增|推出|提供|具备|某款|某个|待发布|一款|一个|这款|这个|新品|产品|东西|对象|配件|功能|能力|方案|相关|对应|通用|未定|未知|的)',
                          '', 'g'),
                        '[^A-Za-z0-9一-鿿]+', '', 'g') ~ '[A-Za-z0-9一-鿿]{2,}'
                 )
               )
             )
             -- 诉求只有事实归属才进老品；仅 root 的诉求按 G5 决议降级新品。
             AND (g.cls <> '诉求缺口' OR a.source = 'fact')
           )
         )
    ), deduplicated AS (
      SELECT DISTINCT ON (message_id, seq, assigned_spu)
             message_id, seq, assigned_spu, source, group_id
        FROM assignment_candidates
       ORDER BY message_id, seq, assigned_spu,
                CASE source WHEN 'fact' THEN 0 ELSE 1 END
    )
    INSERT INTO public.voc_assign_snapshot
           (run_id, message_id, seq, assigned_spu, source, group_id)
    SELECT p_run_id, message_id, seq, assigned_spu, source, group_id
      FROM deduplicated
     ORDER BY message_id, seq, assigned_spu
    ON CONFLICT (run_id, message_id, seq, assigned_spu) DO NOTHING;
  END IF;

  SELECT count(*),
         count(DISTINCT (message_id, seq)),
         md5(COALESCE(
           string_agg(
             format('%s:%s:%s:%s:%s', length(message_id), message_id,
                    seq, assigned_spu, source),
             E'\x1e' ORDER BY message_id, seq, assigned_spu, source
           ),
           ''
         ))
    INTO row_count, distinct_fact_count, content_fingerprint
    FROM public.voc_assign_snapshot
   WHERE run_id = p_run_id;

  INSERT INTO public.voc_run_log
         (run_id, stage, status, metrics, started_at, finished_at)
  VALUES (
    p_run_id || ':assign_snapshot',
    'assign_snapshot',
    'success',
    jsonb_build_object(
      'assign_snapshot', jsonb_build_object(
        'row_count', row_count,
        'distinct_fact_count', distinct_fact_count,
        'content_fingerprint', content_fingerprint
      )
    ),
    now(), now()
  )
  ON CONFLICT (run_id) DO UPDATE SET
    stage = EXCLUDED.stage,
    status = EXCLUDED.status,
    metrics = COALESCE(public.voc_run_log.metrics, '{}'::jsonb)
              || EXCLUDED.metrics,
    finished_at = EXCLUDED.finished_at;

  RETURN row_count;
END;
$$;

CREATE OR REPLACE FUNCTION public.voc_verify_assign_snapshot(p_run_id text)
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  expected_rows bigint;
  expected_facts bigint;
  expected_fingerprint text;
  actual_rows bigint;
  actual_facts bigint;
  actual_fingerprint text;
BEGIN
  SELECT (r.metrics -> 'assign_snapshot' ->> 'row_count')::bigint,
         (r.metrics -> 'assign_snapshot' ->> 'distinct_fact_count')::bigint,
         r.metrics -> 'assign_snapshot' ->> 'content_fingerprint'
    INTO expected_rows, expected_facts, expected_fingerprint
    FROM public.voc_run_log r
   WHERE r.run_id = p_run_id || ':assign_snapshot';

  IF expected_rows IS NULL OR expected_facts IS NULL
     OR expected_fingerprint IS NULL THEN
    RAISE EXCEPTION 'run_id=% 缺少归属快照指纹', p_run_id;
  END IF;

  SELECT count(*),
         count(DISTINCT (message_id, seq)),
         md5(COALESCE(
           string_agg(
             format('%s:%s:%s:%s:%s', length(message_id), message_id,
                    seq, assigned_spu, source),
             E'\x1e' ORDER BY message_id, seq, assigned_spu, source
           ),
           ''
         ))
    INTO actual_rows, actual_facts, actual_fingerprint
    FROM public.voc_assign_snapshot
   WHERE run_id = p_run_id;

  IF (actual_rows, actual_facts, actual_fingerprint)
       IS DISTINCT FROM
     (expected_rows, expected_facts, expected_fingerprint) THEN
    RAISE EXCEPTION
      'run_id=% 归属快照指纹变化：expected=(%,%,%) actual=(%,%,%)',
      p_run_id, expected_rows, expected_facts, expected_fingerprint,
      actual_rows, actual_facts, actual_fingerprint;
  END IF;

  RETURN actual_rows;
END;
$$;

REVOKE ALL ON FUNCTION public.voc_prepare_assign_snapshot(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.voc_verify_assign_snapshot(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.voc_prepare_assign_snapshot(text),
                          public.voc_verify_assign_snapshot(text)
  TO voc_writer;

COMMIT;

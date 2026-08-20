-- 023：社媒帖子线索字段、SPU 继承推断与 G4 机器缓存
-- 以 voc_admin 执行；迁移与应用发布分开，本文件本身不触发模型判定。
BEGIN;

ALTER TABLE voc_message
  ADD COLUMN IF NOT EXISTS message_group_id text,
  ADD COLUMN IF NOT EXISTS message_type text,
  ADD COLUMN IF NOT EXISTS parent_id text,
  ADD COLUMN IF NOT EXISTS author_name text,
  ADD COLUMN IF NOT EXISTS message_title text,
  ADD COLUMN IF NOT EXISTS spu_inherited text[];

COMMENT ON COLUMN voc_message.spu_inherited IS
  'SPU 组级唯一继承推断；与云听事实列 spu 独立，两者永不互写。';

CREATE INDEX IF NOT EXISTS ix_msg_social_group
  ON voc_message (message_group_id, message_type)
  WHERE src_line = '社媒' AND message_group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_msg_spu_inherited
  ON voc_message USING GIN (spu_inherited);

CREATE TABLE IF NOT EXISTS voc_social_gate (
  message_id  text PRIMARY KEY REFERENCES voc_message(message_id) ON DELETE CASCADE,
  cls         text NOT NULL CHECK (cls IN ('诉求缺口','产品缺陷','无价值')),
  claim       text,
  confidence  numeric(4,3),
  votes       int NOT NULL DEFAULT 1,
  prompt_ver  text NOT NULL,
  judged_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE voc_social_gate IS
  '社媒 G4 统一价值门按消息缓存；同 prompt_ver 周跑增量不重判。';

-- 只改写推断列。每次都从同组当前事实 SPU 重算：并集恰好
-- 一个值时继承，无值或冲突时清空旧推断。因此可在每个入库窗口后
-- 重复执行，且新到达的冲突事实不会留下过期继承。
CREATE OR REPLACE FUNCTION voc_backfill_social_spu_inheritance()
RETURNS bigint
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
DECLARE
  changed bigint := 0;
  cleared bigint := 0;
  fact_cleared bigint := 0;
BEGIN
  WITH social_groups AS (
    SELECT DISTINCT message_group_id
      FROM public.voc_message
     WHERE src_line = '社媒'
       AND message_group_id IS NOT NULL
  ), group_spu AS (
    SELECT g.message_group_id,
           array_agg(DISTINCT NULLIF(btrim(x.spu), ''))
             FILTER (WHERE NULLIF(btrim(x.spu), '') IS NOT NULL) AS spus
      FROM social_groups g
      LEFT JOIN public.voc_message m
        ON m.src_line = '社媒'
       AND m.message_group_id = g.message_group_id
      LEFT JOIN LATERAL
           unnest(COALESCE(m.spu, ARRAY[]::text[])) AS x(spu) ON true
     GROUP BY g.message_group_id
  )
  UPDATE public.voc_message target
     SET spu_inherited = CASE
           WHEN cardinality(g.spus) = 1 THEN g.spus
           ELSE NULL
         END
    FROM group_spu g
   WHERE target.src_line = '社媒'
     AND target.message_group_id = g.message_group_id
     AND COALESCE(cardinality(target.spu), 0) = 0
     AND target.spu_inherited IS DISTINCT FROM CASE
           WHEN cardinality(g.spus) = 1 THEN g.spus
           ELSE NULL
         END;
  GET DIAGNOSTICS changed = ROW_COUNT;

  UPDATE public.voc_message
     SET spu_inherited = NULL
   WHERE src_line = '社媒'
     AND message_group_id IS NULL
     AND COALESCE(cardinality(spu), 0) = 0
     AND spu_inherited IS NOT NULL;
  GET DIAGNOSTICS cleared = ROW_COUNT;

  -- 一条消息后续拿到云听事实 SPU 后，推断已无存在必要。
  -- 只清推断列，绝不用它覆写事实列。
  UPDATE public.voc_message
     SET spu_inherited = NULL
   WHERE src_line = '社媒'
     AND COALESCE(cardinality(spu), 0) > 0
     AND spu_inherited IS NOT NULL;
  GET DIAGNOSTICS fact_cleared = ROW_COUNT;

  RETURN changed + cleared + fact_cleared;
END;
$$;

REVOKE ALL ON FUNCTION voc_backfill_social_spu_inheritance() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION voc_backfill_social_spu_inheritance() TO voc_writer;

-- writer 写机器缓存；human/看板应用与纯读角色只读。
GRANT SELECT, INSERT, UPDATE, DELETE ON voc_social_gate TO voc_writer;
GRANT SELECT ON voc_social_gate TO voc_human, voc_reader;

COMMIT;

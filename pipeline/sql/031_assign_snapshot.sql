-- 031：v3 归属快照、关系投影、战略空状态与身份映射基础结构。
BEGIN;

SET LOCAL search_path = public, pg_temp;

DO $guard$
BEGIN
  IF current_user <> 'voc_admin' THEN
    RAISE EXCEPTION '031 必须以 voc_admin 执行，当前为 %', current_user;
  END IF;
END
$guard$;

CREATE TABLE IF NOT EXISTS public.voc_assign_snapshot (
  run_id       text NOT NULL,
  message_id   text NOT NULL,
  seq          int  NOT NULL,
  assigned_spu text NOT NULL,
  source       text NOT NULL CHECK (source IN ('fact', 'root')),
  group_id     text,
  PRIMARY KEY (run_id, message_id, seq, assigned_spu)
);

CREATE INDEX IF NOT EXISTS ix_assign_snapshot_fact
  ON public.voc_assign_snapshot (run_id, message_id, seq);
CREATE INDEX IF NOT EXISTS ix_assign_snapshot_spu
  ON public.voc_assign_snapshot (run_id, assigned_spu, message_id, seq);

COMMENT ON TABLE public.voc_assign_snapshot IS
  '每轮 G5 归属的不可变物理快照；路由、落库、派生与验收共用同一 run_id。';
COMMENT ON COLUMN public.voc_assign_snapshot.source IS
  'fact=消息事实 SPU；root=029 收敛后的根帖/视频继承 SPU。';

CREATE OR REPLACE FUNCTION public.voc_reject_assign_snapshot_update()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
  RAISE EXCEPTION 'voc_assign_snapshot 不允许 UPDATE；请新建 run_id';
END;
$$;

DROP TRIGGER IF EXISTS trg_voc_assign_snapshot_no_update
  ON public.voc_assign_snapshot;
CREATE TRIGGER trg_voc_assign_snapshot_no_update
  BEFORE UPDATE ON public.voc_assign_snapshot
  FOR EACH ROW EXECUTE FUNCTION public.voc_reject_assign_snapshot_update();

-- DELETE 只能把语句涉及的每个 run_id 整轮删尽。AFTER STATEMENT 使用过渡表
-- 检查残留；部分删除会抛错并回滚整条语句。
CREATE OR REPLACE FUNCTION public.voc_guard_assign_snapshot_delete()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM (SELECT DISTINCT run_id FROM deleted_rows) d
     WHERE EXISTS (
       SELECT 1 FROM public.voc_assign_snapshot s
        WHERE s.run_id = d.run_id
     )
  ) THEN
    RAISE EXCEPTION 'voc_assign_snapshot 只允许按 run_id 整轮 DELETE';
  END IF;
  RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_voc_assign_snapshot_whole_run_delete
  ON public.voc_assign_snapshot;
CREATE TRIGGER trg_voc_assign_snapshot_whole_run_delete
  AFTER DELETE ON public.voc_assign_snapshot
  REFERENCING OLD TABLE AS deleted_rows
  FOR EACH STATEMENT EXECUTE FUNCTION public.voc_guard_assign_snapshot_delete();

ALTER TABLE public.voc_opp_evidence
  ADD COLUMN IF NOT EXISTS assigned_spu text,
  ADD COLUMN IF NOT EXISTS assignment_source text,
  ADD COLUMN IF NOT EXISTS assign_run_id text;

DO $constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_opp_evidence'::regclass
       AND conname = 'ck_oe_assignment_source'
  ) THEN
    ALTER TABLE public.voc_opp_evidence
      ADD CONSTRAINT ck_oe_assignment_source
      CHECK (assignment_source IS NULL
             OR assignment_source IN ('fact', 'root'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_opp_evidence'::regclass
       AND conname = 'ck_oe_assignment_pair'
  ) THEN
    ALTER TABLE public.voc_opp_evidence
      ADD CONSTRAINT ck_oe_assignment_pair
      CHECK ((assigned_spu IS NULL) = (assignment_source IS NULL));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_opp_evidence'::regclass
       AND conname = 'ck_oe_assignment_run'
  ) THEN
    ALTER TABLE public.voc_opp_evidence
      ADD CONSTRAINT ck_oe_assignment_run
      CHECK (assigned_spu IS NULL OR assign_run_id IS NOT NULL);
  END IF;
END
$constraints$;

CREATE INDEX IF NOT EXISTS ix_oe_assignment
  ON public.voc_opp_evidence
     (assign_run_id, assigned_spu, opp_id, message_id, seq);

COMMENT ON COLUMN public.voc_opp_evidence.assigned_spu IS
  '该关系行在 assign_run_id 快照中的唯一 SPU 归属；新品为 NULL。';
COMMENT ON COLUMN public.voc_opp_evidence.assignment_source IS
  '归属来源 fact/root；新品为 NULL。';
COMMENT ON COLUMN public.voc_opp_evidence.assign_run_id IS
  '投影来源快照的运行 ID；v3 新品也记录生成 run_id。';

ALTER TABLE public.voc_opportunity
  ADD COLUMN IF NOT EXISTS scope_source text;
COMMENT ON COLUMN public.voc_opportunity.scope_source IS
  'scope/n_eff 的口径来源；v3 本轮固定为 v3-未计算。';

-- 规格 §4.4 将该索引列为随 core_tag 语义切换一并重建的对象。列形状保持
-- (core_tag, opp_type, category)，重建后同时覆盖存量 OPP-* 与新增 OPP2-*。
DROP INDEX IF EXISTS public.ix_opp_bucket;
CREATE INDEX ix_opp_bucket
  ON public.voc_opportunity (core_tag, opp_type, category);

ALTER TABLE public.voc_opp_snapshot
  ADD COLUMN IF NOT EXISTS denominator_scope text;
COMMENT ON COLUMN public.voc_opp_snapshot.denominator_scope IS
  'base_total/neg_total 的分母口径；v3 老品为冻结 assigned_spu。';

CREATE TABLE IF NOT EXISTS public.voc_opp_id_map (
  legacy_opp_id text NOT NULL,
  v3_opp_id     text NOT NULL,
  legacy_spu    text,
  mapped_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (legacy_spu IS NULL OR NULLIF(btrim(legacy_spu), '') IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_voc_opp_id_map
  ON public.voc_opp_id_map
     (legacy_opp_id, v3_opp_id, COALESCE(legacy_spu, ''));
CREATE INDEX IF NOT EXISTS ix_voc_opp_id_map_v3
  ON public.voc_opp_id_map (v3_opp_id);

COMMENT ON TABLE public.voc_opp_id_map IS
  '旧 OPP-* 到 v3 OPP2-* 的只追加映射；人工状态迁移与旧引用盘点使用。';

CREATE OR REPLACE FUNCTION public.voc_reject_opp_id_map_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
  RAISE EXCEPTION 'voc_opp_id_map 是只追加审计表，不允许 %', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS trg_voc_opp_id_map_append_only
  ON public.voc_opp_id_map;
CREATE TRIGGER trg_voc_opp_id_map_append_only
  BEFORE UPDATE OR DELETE ON public.voc_opp_id_map
  FOR EACH ROW EXECUTE FUNCTION public.voc_reject_opp_id_map_mutation();

-- 快照只能经 033 的 SECURITY DEFINER 准备函数写入；运行角色若能直接 INSERT，
-- 就能在函数记下指纹后给同一 run_id 追加新行，破坏“单写者”。DELETE 保留给
-- smoke/验收清理，并由上面的整轮触发器约束。
REVOKE INSERT, UPDATE, TRUNCATE ON public.voc_assign_snapshot
  FROM voc_writer, voc_human, voc_reader;
GRANT SELECT, DELETE ON public.voc_assign_snapshot TO voc_writer;
GRANT SELECT ON public.voc_assign_snapshot TO voc_human, voc_reader;
GRANT SELECT, INSERT ON public.voc_opp_id_map TO voc_writer;
GRANT SELECT ON public.voc_opp_id_map TO voc_human, voc_reader;

COMMIT;

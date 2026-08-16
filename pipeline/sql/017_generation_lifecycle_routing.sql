-- ============================================================
-- 阶段 2c：生成前分类，来源能力数据化。
--
-- 本迁移只扩展来源契约与通用按来源计数；不重算分类、
-- 不改写存量 opp_id，也不改动 016 建立的 locked 冻结行为。
-- 基线顺序必须是 015 -> 016 -> 017；第三来源进入后不得重跑
-- 仍以两来源硬编码回填的 015。
-- ============================================================

BEGIN;

SET LOCAL search_path = public, pg_temp;

DO $$
BEGIN
  IF current_user <> 'voc_admin' THEN
    RAISE EXCEPTION '017 必须以 voc_admin 执行，当前角色为 %', current_user;
  END IF;
END $$;

-- 执行窗口需停止调度。阻止来源事实、证据关系和机会点在
-- FK 验证与回填期间改变，使自检看到同一份证据集。
LOCK TABLE public.voc_message,
           public.voc_opportunity,
           public.voc_opp_evidence
  IN SHARE ROW EXCLUSIVE MODE;

CREATE TABLE public.voc_source_policy (
  src_line     text PRIMARY KEY,
  requires_spu boolean NOT NULL,
  CONSTRAINT voc_source_policy_name_check
    CHECK (src_line = btrim(src_line) AND src_line <> '')
);

COMMENT ON TABLE public.voc_source_policy IS
  '来源能力配置；requires_spu=true 表示该来源保证消息挂载 SPU。';
COMMENT ON COLUMN public.voc_source_policy.requires_spu IS
  '生成期分类 R2/R3 的来源属性，不在应用代码中比较来源名。';

-- 只登记当前真实存在的来源。第三来源（问卷调研）已推迟到 0.0.2 之后，
-- 名称尚未确认，不预先写进 schema——把未确认的决定固化下来，
-- 后面要么改要么将错就错。接入时一条 INSERT 即可，这正是本表的意义。
INSERT INTO public.voc_source_policy (src_line, requires_spu)
VALUES ('电商', true),
       ('社媒', false);

-- 先建立并验证可扩展 FK，再删除旧的两值 CHECK。全部处于
-- 同一事务，不会暴露无来源约束的中间状态。
ALTER TABLE public.voc_message
  ADD CONSTRAINT voc_message_source_policy_fkey
  FOREIGN KEY (src_line)
  REFERENCES public.voc_source_policy (src_line)
  ON UPDATE RESTRICT ON DELETE RESTRICT
  NOT VALID;

ALTER TABLE public.voc_opportunity
  ADD CONSTRAINT voc_opportunity_source_policy_fkey
  FOREIGN KEY (src_line)
  REFERENCES public.voc_source_policy (src_line)
  ON UPDATE RESTRICT ON DELETE RESTRICT
  NOT VALID;

ALTER TABLE public.voc_message
  VALIDATE CONSTRAINT voc_message_source_policy_fkey;

ALTER TABLE public.voc_opportunity
  VALIDATE CONSTRAINT voc_opportunity_source_policy_fkey;

-- 不用 IF EXISTS：017 明确建立在 013/016 后，约束缺失代表
-- schema 漂移，应失败即停。
ALTER TABLE public.voc_message
  DROP CONSTRAINT voc_message_src_line_check;

ALTER TABLE public.voc_opportunity
  DROP CONSTRAINT voc_opportunity_src_line_check;

-- message_id 是 voc_evidence / voc_opp_evidence 的全局身份根。
-- 新来源若复用了其他来源的 ID，旧的 ON CONFLICT UPDATE
-- 会把原消息整行覆盖。在数据库层禁止已存消息换来源，
-- 应用层也会提前给出可读的冲突错误。
CREATE FUNCTION public.voc_guard_message_source_identity() RETURNS trigger AS $$
BEGIN
  IF NEW.src_line IS DISTINCT FROM OLD.src_line THEN
    RAISE EXCEPTION
      'message_id % 不得从来源 % 改挂到 %',
      OLD.message_id, OLD.src_line, NEW.src_line;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_voc_guard_message_source_identity
  BEFORE UPDATE OF src_line ON public.voc_message
  FOR EACH ROW EXECUTE FUNCTION public.voc_guard_message_source_identity();

ALTER TABLE public.voc_opportunity
  ADD COLUMN source_lines text[],
  ADD COLUMN evi_by_source jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE public.voc_opportunity
  ADD CONSTRAINT voc_opportunity_evi_by_source_object_check
  CHECK (jsonb_typeof(evi_by_source) = 'object')
  NOT VALID;

ALTER TABLE public.voc_opportunity
  VALIDATE CONSTRAINT voc_opportunity_evi_by_source_object_check;

COMMENT ON COLUMN public.voc_opportunity.source_lines IS
  '完整供证来源集合，按名称排序；NULL 表示零证据。src_line 只作 legacy 单值代表。';
COMMENT ON COLUMN public.voc_opportunity.evi_by_source IS
  '按供证来源统计的证据数 JSON 对象；evi_ec/evi_social 仅为一期兼容投影。';

-- weak_evidence 保持旧契约。dual_source 不再读两个旧计数列，
-- 而是统计 evi_by_source 中证据数为正的来源个数。
-- 兼容旧写入方：当通用对象仍为空时，先把正数的
-- evi_ec/evi_social 投影进对象，然后仍用同一通用算法求值。
CREATE OR REPLACE FUNCTION public.voc_derive_flags() RETURNS trigger AS $$
DECLARE
  counts jsonb := COALESCE(NEW.evi_by_source, '{}'::jsonb);
  positive_source_count int;
BEGIN
  NEW.weak_evidence := COALESCE(NEW.evi_total, 0) <= 2;

  IF counts = '{}'::jsonb THEN
    IF COALESCE(NEW.evi_ec, 0) > 0 THEN
      counts := counts || jsonb_build_object('电商', NEW.evi_ec);
    END IF;
    IF COALESCE(NEW.evi_social, 0) > 0 THEN
      counts := counts || jsonb_build_object('社媒', NEW.evi_social);
    END IF;
    NEW.evi_by_source := counts;
  END IF;

  SELECT count(*)::int
    INTO positive_source_count
    FROM jsonb_each(
           CASE WHEN jsonb_typeof(counts) = 'object'
                THEN counts ELSE '{}'::jsonb END
         ) AS source_count(src_line, evidence_count)
   WHERE jsonb_typeof(evidence_count) = 'number'
     AND (evidence_count #>> '{}')::numeric > 0;

  NEW.dual_source := positive_source_count >= 2;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- 只从权威关系层反算，不信任可能漂移的 evi_ec/evi_social。
-- 不更新存量 src_line：保留旧消费者所见的单值；完整集合只看新字段。
WITH source_counts AS (
  SELECT oe.opp_id,
         m.src_line,
         count(*)::int AS evidence_count
    FROM public.voc_opp_evidence oe
    JOIN public.voc_message m USING (message_id)
   GROUP BY oe.opp_id, m.src_line
), source_aggregates AS (
  SELECT opp_id,
         array_agg(src_line ORDER BY src_line) AS source_lines,
         jsonb_object_agg(
           src_line, evidence_count ORDER BY src_line
         ) AS evi_by_source,
         sum(evidence_count)::int AS evidence_total
    FROM source_counts
   GROUP BY opp_id
), expected AS (
  SELECT o.opp_id,
         a.source_lines,
         COALESCE(a.evi_by_source, '{}'::jsonb) AS evi_by_source,
         COALESCE((a.evi_by_source ->> '电商')::int, 0) AS evi_ec,
         COALESCE((a.evi_by_source ->> '社媒')::int, 0) AS evi_social
    FROM public.voc_opportunity o
    LEFT JOIN source_aggregates a USING (opp_id)
)
UPDATE public.voc_opportunity o
   SET source_lines = e.source_lines,
       evi_by_source = e.evi_by_source,
       -- 两列只是旧看板兼容投影。必须在同一次权威回填中
       -- 一起清零/重算；否则零关系机会的旧计数会触发兼容
       -- fallback，把空 evi_by_source 又伪造成非空。
       evi_ec = e.evi_ec,
       evi_social = e.evi_social
  FROM expected e
 WHERE e.opp_id = o.opp_id;

GRANT SELECT ON public.voc_source_policy
  TO voc_writer, voc_human, voc_reader;

-- 不写死现网行数：按来源契约、关系回填和派生结果做结构自检。
DO $$
DECLARE
  bad_policy_count bigint;
  valid_fk_count bigint;
  source_mismatch_count bigint;
  evi_total_mismatch_count bigint;
  dual_mismatch_count bigint;
  frozen_guard_count bigint;
  frozen_trigger_count bigint;
BEGIN
  SELECT count(*) INTO bad_policy_count
    FROM (
      VALUES ('电商', true), ('社媒', false)
    ) AS expected(src_line, requires_spu)
    FULL JOIN public.voc_source_policy actual USING (src_line)
   WHERE actual.src_line IS NULL
      OR expected.src_line IS NULL
      OR actual.requires_spu IS DISTINCT FROM expected.requires_spu;

  SELECT count(*) INTO valid_fk_count
    FROM pg_constraint
   WHERE conname IN ('voc_message_source_policy_fkey',
                     'voc_opportunity_source_policy_fkey')
     AND contype = 'f'
     AND convalidated
     AND conrelid IN ('public.voc_message'::regclass,
                      'public.voc_opportunity'::regclass)
     AND confrelid = 'public.voc_source_policy'::regclass;

  WITH source_counts AS (
    SELECT oe.opp_id,
           m.src_line,
           count(*)::int AS evidence_count
      FROM public.voc_opp_evidence oe
      JOIN public.voc_message m USING (message_id)
     GROUP BY oe.opp_id, m.src_line
  ), source_aggregates AS (
    SELECT opp_id,
           array_agg(src_line ORDER BY src_line) AS source_lines,
           jsonb_object_agg(
             src_line, evidence_count ORDER BY src_line
           ) AS evi_by_source,
           sum(evidence_count)::int AS evidence_total
      FROM source_counts
     GROUP BY opp_id
  )
  SELECT count(*) INTO source_mismatch_count
    FROM public.voc_opportunity o
    LEFT JOIN source_aggregates a USING (opp_id)
   WHERE o.source_lines IS DISTINCT FROM a.source_lines
      OR o.evi_by_source IS DISTINCT FROM
           COALESCE(a.evi_by_source, '{}'::jsonb)
      OR o.evi_ec IS DISTINCT FROM
           COALESCE((a.evi_by_source ->> '电商')::int, 0)
      OR o.evi_social IS DISTINCT FROM
           COALESCE((a.evi_by_source ->> '社媒')::int, 0);

  SELECT count(*) INTO evi_total_mismatch_count
    FROM public.voc_opportunity o
   WHERE o.evi_total IS DISTINCT FROM (
     SELECT count(*)::int
       FROM public.voc_opp_evidence oe
      WHERE oe.opp_id = o.opp_id
   );

  SELECT count(*) INTO dual_mismatch_count
    FROM public.voc_opportunity o
   WHERE o.dual_source IS DISTINCT FROM (
     SELECT count(*) >= 2
       FROM jsonb_each(o.evi_by_source) AS c(src_line, evidence_count)
      WHERE jsonb_typeof(evidence_count) = 'number'
        AND (evidence_count #>> '{}')::numeric > 0
   );

  SELECT count(*) INTO frozen_guard_count
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = 'public'
     AND p.proname = 'voc_guard_locked'
     AND p.prosrc LIKE '%NEW.opp_type%OLD.opp_type%'
     AND p.prosrc LIKE '%NEW.classification_state%OLD.classification_state%'
     AND p.prosrc LIKE '%NEW.classify_rule%OLD.classify_rule%';

  SELECT count(*) INTO frozen_trigger_count
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_proc p ON p.oid = t.tgfoid
   WHERE n.nspname = 'public'
     AND c.relname = 'voc_opportunity'
     AND t.tgname = 'trg_voc_guard_locked'
     AND p.proname = 'voc_guard_locked'
     AND NOT t.tgisinternal
     AND t.tgenabled <> 'D';

  IF bad_policy_count <> 0
     OR valid_fk_count <> 2
     OR source_mismatch_count <> 0
     OR evi_total_mismatch_count <> 0
     OR dual_mismatch_count <> 0
     OR frozen_guard_count <> 1
     OR frozen_trigger_count <> 1 THEN
    RAISE EXCEPTION
      '017 自检失败：policy异常=%，已验证FK=%/2，来源回填漂移=%，evi_total漂移=%，dual_source漂移=%，016冻结guard=%/1，启用trigger=%/1',
      bad_policy_count, valid_fk_count, source_mismatch_count,
      evi_total_mismatch_count, dual_mismatch_count, frozen_guard_count,
      frozen_trigger_count;
  END IF;

  RAISE NOTICE
    '017 自检通过：policy异常=%，已验证FK=%/2，来源回填漂移=%，evi_total漂移=%，dual_source漂移=%，016冻结guard=%/1，启用trigger=%/1',
    bad_policy_count, valid_fk_count, source_mismatch_count,
    evi_total_mismatch_count, dual_mismatch_count, frozen_guard_count,
    frozen_trigger_count;
END $$;

COMMIT;

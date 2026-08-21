-- 034：战略视图的轴、成员与 LLM 判定缓存。
BEGIN;

SET LOCAL search_path = public, pg_temp;

DO $guard$
BEGIN
  IF current_user <> 'voc_admin' THEN
    RAISE EXCEPTION '034 必须以 voc_admin 执行，当前为 %', current_user;
  END IF;
END
$guard$;

CREATE TABLE IF NOT EXISTS public.voc_strategy_axis (
  axis_id     text,
  generation  text NOT NULL,
  axis_type   text NOT NULL,
  axis_name   text NOT NULL,
  summary     text,
  n_cards     int NOT NULL,
  n_spu       int,
  n_eff       numeric,
  evi_total   int NOT NULL,
  prod_lines  text[] NOT NULL DEFAULT '{}',
  categories  text[] NOT NULL DEFAULT '{}',
  top_spus    jsonb,
  run_id      text NOT NULL,
  computed_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_voc_strategy_axis PRIMARY KEY (axis_id),
  CONSTRAINT ck_strategy_axis_generation
    CHECK (generation IN ('OPP-', 'OPP2-')),
  CONSTRAINT ck_strategy_axis_type CHECK (axis_type IN ('通病', '诉求')),
  CONSTRAINT ux_strategy_axis_identity
    UNIQUE (axis_id, generation, axis_type)
);

ALTER TABLE public.voc_strategy_axis
  ADD COLUMN IF NOT EXISTS axis_id text,
  ADD COLUMN IF NOT EXISTS generation text,
  ADD COLUMN IF NOT EXISTS axis_type text,
  ADD COLUMN IF NOT EXISTS axis_name text,
  ADD COLUMN IF NOT EXISTS summary text,
  ADD COLUMN IF NOT EXISTS n_cards int,
  ADD COLUMN IF NOT EXISTS n_spu int,
  ADD COLUMN IF NOT EXISTS n_eff numeric,
  ADD COLUMN IF NOT EXISTS evi_total int,
  ADD COLUMN IF NOT EXISTS prod_lines text[] DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS categories text[] DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS top_spus jsonb,
  ADD COLUMN IF NOT EXISTS run_id text,
  ADD COLUMN IF NOT EXISTS computed_at timestamptz DEFAULT now();

ALTER TABLE public.voc_strategy_axis
  ALTER COLUMN prod_lines SET DEFAULT '{}',
  ALTER COLUMN categories SET DEFAULT '{}',
  ALTER COLUMN computed_at SET DEFAULT now();

DO $axis_constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_strategy_axis'::regclass
       AND contype = 'p'
  ) THEN
    ALTER TABLE public.voc_strategy_axis
      ADD CONSTRAINT pk_voc_strategy_axis PRIMARY KEY (axis_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_strategy_axis'::regclass
       AND conname = 'ck_strategy_axis_generation'
  ) THEN
    ALTER TABLE public.voc_strategy_axis
      ADD CONSTRAINT ck_strategy_axis_generation
      CHECK (generation IN ('OPP-', 'OPP2-'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_strategy_axis'::regclass
       AND conname = 'ck_strategy_axis_type'
  ) THEN
    ALTER TABLE public.voc_strategy_axis
      ADD CONSTRAINT ck_strategy_axis_type
      CHECK (axis_type IN ('通病', '诉求'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_strategy_axis'::regclass
       AND conname = 'ux_strategy_axis_identity'
  ) THEN
    ALTER TABLE public.voc_strategy_axis
      ADD CONSTRAINT ux_strategy_axis_identity
      UNIQUE (axis_id, generation, axis_type);
  END IF;
END
$axis_constraints$;

CREATE INDEX IF NOT EXISTS ix_strategy_axis_type
  ON public.voc_strategy_axis (axis_type, n_eff DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS ix_strategy_axis_partition
  ON public.voc_strategy_axis
     (generation, axis_type, n_eff DESC NULLS LAST, evi_total DESC);

CREATE TABLE IF NOT EXISTS public.voc_strategy_axis_member (
  axis_id       text NOT NULL,
  generation    text NOT NULL,
  axis_type     text NOT NULL,
  opp_id        text NOT NULL,
  role          text NOT NULL,
  cos_to_leader numeric,
  link_spu      text,
  CONSTRAINT pk_voc_strategy_axis_member PRIMARY KEY (axis_id, opp_id),
  CONSTRAINT ck_strategy_member_role CHECK (role IN ('leader', 'member'))
);

ALTER TABLE public.voc_strategy_axis_member
  ADD COLUMN IF NOT EXISTS axis_id text,
  ADD COLUMN IF NOT EXISTS generation text,
  ADD COLUMN IF NOT EXISTS axis_type text,
  ADD COLUMN IF NOT EXISTS opp_id text,
  ADD COLUMN IF NOT EXISTS role text,
  ADD COLUMN IF NOT EXISTS cos_to_leader numeric,
  ADD COLUMN IF NOT EXISTS link_spu text;

DO $member_constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_strategy_axis_member'::regclass
       AND contype = 'p'
  ) THEN
    ALTER TABLE public.voc_strategy_axis_member
      ADD CONSTRAINT pk_voc_strategy_axis_member
      PRIMARY KEY (axis_id, opp_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_strategy_axis_member'::regclass
       AND conname = 'ck_strategy_member_role'
  ) THEN
    ALTER TABLE public.voc_strategy_axis_member
      ADD CONSTRAINT ck_strategy_member_role
      CHECK (role IN ('leader', 'member'));
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_strategy_axis_member'::regclass
       AND conname = 'fk_strategy_member_axis_id'
  ) THEN
    ALTER TABLE public.voc_strategy_axis_member
      ADD CONSTRAINT fk_strategy_member_axis_id
      FOREIGN KEY (axis_id)
      REFERENCES public.voc_strategy_axis (axis_id) ON DELETE CASCADE;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_strategy_axis_member'::regclass
       AND conname = 'fk_strategy_member_axis'
  ) THEN
    ALTER TABLE public.voc_strategy_axis_member
      ADD CONSTRAINT fk_strategy_member_axis
      FOREIGN KEY (axis_id, generation, axis_type)
      REFERENCES public.voc_strategy_axis (axis_id, generation, axis_type)
      ON DELETE CASCADE;
  END IF;
END
$member_constraints$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_strategy_member_once
  ON public.voc_strategy_axis_member (generation, axis_type, opp_id);
CREATE INDEX IF NOT EXISTS ix_strategy_member_opp
  ON public.voc_strategy_axis_member (opp_id);

CREATE TABLE IF NOT EXISTS public.voc_strategy_pair_verdict (
  opp_a        text NOT NULL,
  opp_b        text NOT NULL,
  mode_hash_a  text NOT NULL,
  mode_hash_b  text NOT NULL,
  judge_policy text NOT NULL,
  same         boolean NOT NULL,
  judged_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pk_voc_strategy_pair_verdict
    PRIMARY KEY (opp_a, opp_b, mode_hash_a, mode_hash_b, judge_policy),
  CONSTRAINT ck_strategy_verdict_order CHECK (opp_a < opp_b)
);

ALTER TABLE public.voc_strategy_pair_verdict
  ADD COLUMN IF NOT EXISTS opp_a text,
  ADD COLUMN IF NOT EXISTS opp_b text,
  ADD COLUMN IF NOT EXISTS mode_hash_a text,
  ADD COLUMN IF NOT EXISTS mode_hash_b text,
  ADD COLUMN IF NOT EXISTS judge_policy text,
  ADD COLUMN IF NOT EXISTS same boolean,
  ADD COLUMN IF NOT EXISTS judged_at timestamptz DEFAULT now();

ALTER TABLE public.voc_strategy_pair_verdict
  ALTER COLUMN judged_at SET DEFAULT now();

DO $verdict_constraints$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_strategy_pair_verdict'::regclass
       AND contype = 'p'
  ) THEN
    ALTER TABLE public.voc_strategy_pair_verdict
      ADD CONSTRAINT pk_voc_strategy_pair_verdict
      PRIMARY KEY (opp_a, opp_b, mode_hash_a, mode_hash_b, judge_policy);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'public.voc_strategy_pair_verdict'::regclass
       AND conname = 'ck_strategy_verdict_order'
  ) THEN
    ALTER TABLE public.voc_strategy_pair_verdict
      ADD CONSTRAINT ck_strategy_verdict_order CHECK (opp_a < opp_b);
  END IF;
END
$verdict_constraints$;

GRANT SELECT, INSERT, DELETE
  ON public.voc_strategy_axis, public.voc_strategy_axis_member
  TO voc_writer;
GRANT SELECT, INSERT
  ON public.voc_strategy_pair_verdict
  TO voc_writer;
GRANT SELECT
  ON public.voc_strategy_axis, public.voc_strategy_axis_member
  TO voc_human, voc_reader;
REVOKE ALL ON public.voc_strategy_pair_verdict
  FROM voc_human, voc_reader;

COMMIT;

-- ============================================================
-- VOC 产品原型系统 · 一期 Schema
-- 对应 PRD v8 §3.3 / §7.5 / §10.3
-- 目标：PostgreSQL 18 + pgvector + pg_trgm
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- 全文检索扩展：PRD 原定 pg_bigm，但 M0 实测 pgvector/pgvector:pg18 镜像与其
-- apt 源均不提供 pg_bigm/pgroonga。改用 pg_trgm，已实测验证：
--   · 中/日/英子串检索均正确命中
--   · 5 万行下 GIN 索引正常启用（Bitmap Index Scan），中文 5.3ms / 日文 3.0ms
--   · similarity() 排序对中文有效
-- 已知限制：查询串短于 3 字符时无法走索引，退化为顺序扫描（不影响正确性）。
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ------------------------------------------------------------
-- 事实层：保留期内只追加
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voc_message (
  message_id      text PRIMARY KEY,
  src_line        text NOT NULL CHECK (src_line IN ('电商','社媒')),
  platform        text,
  publish_time    timestamptz,
  content         text,                       -- 正文原文，保留原语种
  content_zh      text,                       -- 翻译后正文，社媒常为空
  url             text,                       -- 下钻链接
  star            numeric(2,1),               -- 社媒恒为 NULL
  category        text,                       -- 社媒恒为 NULL（实测 0/11923）
  product_name    text,
  product_id      text,
  product_grade   text,
  country         text,
  lang            text,                       -- 决定清洗分支
  interactions    bigint,
  brands          text[],
  content_type    text[],                     -- 社媒去水标签，多值
  pull_batch_id   text NOT NULL,
  pulled_at       timestamptz NOT NULL DEFAULT now(),
  retention_until date                        -- 到期去标识
);
CREATE INDEX IF NOT EXISTS ix_msg_cat   ON voc_message (category, publish_time DESC);
CREATE INDEX IF NOT EXISTS ix_msg_brand ON voc_message USING GIN (brands);
CREATE INDEX IF NOT EXISTS ix_msg_ctype ON voc_message USING GIN (content_type);
CREATE INDEX IF NOT EXISTS ix_msg_line  ON voc_message (src_line, publish_time DESC);
CREATE INDEX IF NOT EXISTS ix_msg_ret   ON voc_message (retention_until)
       WHERE retention_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_msg_fts   ON voc_message USING GIN (content gin_trgm_ops);

CREATE TABLE IF NOT EXISTS voc_evidence (
  message_id  text NOT NULL REFERENCES voc_message(message_id) ON DELETE CASCADE,
  seq         int  NOT NULL,                  -- 标签数组下标，幂等键
  tag_raw     text,
  tag         text,                           -- 跨产品线归一后
  sentiment   text,
  snippet     text,                           -- 已修尾部重复
  snippet_raw text,                           -- 原始片段留档
  tax_path    text,
  tax_l1      text,
  is_product  boolean,
  is_scene    boolean,
  low_conf    boolean DEFAULT false,          -- 高星+负面 => 疑似误标
  PRIMARY KEY (message_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_evi_tag ON voc_evidence (tag, sentiment)
       WHERE is_product AND NOT low_conf;
CREATE INDEX IF NOT EXISTS ix_evi_msg ON voc_evidence (message_id);
CREATE INDEX IF NOT EXISTS ix_evi_fts ON voc_evidence USING GIN (snippet gin_trgm_ops);

-- Stage 1 未能归类 / 投票淘汰 / 截断的片段：不丢弃
CREATE TABLE IF NOT EXISTS voc_unclassified_evidence (
  message_id text NOT NULL,
  seq        int  NOT NULL,
  week       text NOT NULL,
  reason     text CHECK (reason IN ('unclassified','vote_dropped','truncated')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (message_id, seq, week),
  FOREIGN KEY (message_id, seq)
    REFERENCES voc_evidence(message_id, seq) ON DELETE CASCADE
);

-- ------------------------------------------------------------
-- 机会点层：机器生成
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voc_opportunity (
  opp_id            text PRIMARY KEY,
  opp_type          text CHECK (opp_type IN ('老品迭代','新品创新')),
  src_line          text CHECK (src_line IN ('电商','社媒')), -- 发现方，非供证方
  channel           text,                     -- 社媒子类型: 需求缺口 / 竞品对标
  prod_line         text,                     -- 灯光 / 支撑 / 未定
  category          text,                     -- 社媒发现恒为 'SOCIAL-NA'
  category_set      text[],                   -- 实际涉及品类集合（允许跨桶）
  core_tag          text,                     -- 电商=分桶tag(写死)；社媒=诉求主题簇
  problem_mode      text,
  title             text,
  desc_phenomenon   text,
  desc_attribution  text,
  desc_suggestion   text,
  -- 安全通道 --
  safety_flag         boolean DEFAULT false,
  safety_evidence_ids text[],
  -- 统计 --
  evi_total     int DEFAULT 0,
  evi_ec        int DEFAULT 0,
  evi_social    int DEFAULT 0,
  low_star_rate numeric(5,4),
  dual_source   boolean DEFAULT false,
  weak_evidence boolean DEFAULT false,        -- evi_total <= 2
  rank_score    numeric(8,3),
  countries     text[],
  product_names text[],
  rep_snippets  text[],
  -- 可复现性 --
  mode_vec      vector(1024),
  prompt_ver    text,
  model_id      text,
  model_ver     text,
  ctx_hash      text,
  -- 生命周期 --
  first_week    text,
  last_week     text,
  needs_review  boolean DEFAULT false,     -- 仅由程序化校验决定，见 §5.9
  review_notes  jsonb,                     -- LLM 复核意见，供人工抽查，不参与判定
  backlog       boolean DEFAULT true,         -- 冷启动默认不进 PM 视野
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_opp_vec    ON voc_opportunity
       USING hnsw (mode_vec vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_opp_bucket ON voc_opportunity (core_tag, opp_type, category);
CREATE INDEX IF NOT EXISTS ix_opp_rank   ON voc_opportunity (safety_flag DESC, rank_score DESC);
CREATE INDEX IF NOT EXISTS ix_opp_line   ON voc_opportunity (src_line, last_week);

-- ------------------------------------------------------------
-- 关系层
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voc_opp_evidence (
  opp_id      text NOT NULL REFERENCES voc_opportunity(opp_id) ON DELETE CASCADE,
  message_id  text NOT NULL,
  seq         int  NOT NULL,
  attach_week text,
  match_by    text CHECK (match_by IN ('rule','vector','llm','cross_line')),
  confidence  numeric(4,3),
  PRIMARY KEY (opp_id, message_id, seq),
  FOREIGN KEY (message_id, seq)
    REFERENCES voc_evidence(message_id, seq) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_oe_msg ON voc_opp_evidence (message_id, seq);

-- 所有【未执行】的提议统一落这里
CREATE TABLE IF NOT EXISTS voc_proposal (
  proposal_id bigserial PRIMARY KEY,
  op_type     text CHECK (op_type IN ('MERGE','SPLIT','REVIVE','REWRITE')),
  opp_ids     text[] NOT NULL,
  payload     jsonb,
  rationale   text NOT NULL,
  week        text,
  status      text DEFAULT 'pending'
              CHECK (status IN ('pending','accepted','rejected')),
  decided_by  text,
  decided_at  timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_prop_status ON voc_proposal (status, created_at);

-- 仅记录【已执行】的操作
CREATE TABLE IF NOT EXISTS voc_opp_lineage (
  lineage_id  bigserial PRIMARY KEY,
  op_type     text CHECK (op_type IN ('CREATE','MERGE','SPLIT','DEPRECATE','REVIVE')),
  parent_ids  text[],
  child_ids   text[],
  week        text,
  reason      text,
  proposal_id bigint REFERENCES voc_proposal(proposal_id),
  decided_by  text DEFAULT 'machine',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- 快照：含分母 + 内容版本（回滚与闭环验证依赖）
CREATE TABLE IF NOT EXISTS voc_opp_snapshot (
  opp_id     text NOT NULL REFERENCES voc_opportunity(opp_id) ON DELETE CASCADE,
  week       text NOT NULL,
  evi_total  int,
  evi_new    int,
  rank_score numeric(8,3),
  base_total int,        -- 该 (category, core_tag) 当周全部产品证据数（含正负）
  neg_total  int,        -- 其中负面数
  -- 内容快照，支持按周回滚
  title            text,
  problem_mode     text,
  desc_phenomenon  text,
  desc_attribution text,
  desc_suggestion  text,
  prompt_ver text,
  model_ver  text,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (opp_id, week)
);

CREATE TABLE IF NOT EXISTS voc_run_log (
  run_id            text PRIMARY KEY,
  week              text,
  stage             text,
  src_line          text,
  window_start      timestamptz,
  window_end        timestamptz,
  matched_rows      bigint,
  exported_rows     bigint,
  truncated         boolean,
  batch_count       int,
  unclassified_rows int,
  llm_calls         int,
  llm_tokens        bigint,
  llm_failed_modes  int,
  status            text,
  error_message     text,
  metrics           jsonb,
  started_at        timestamptz,
  finished_at       timestamptz
);
CREATE INDEX IF NOT EXISTS ix_run_week ON voc_run_log (week, stage);

-- ------------------------------------------------------------
-- 人工层：机器只读、人只写；约束触发器见 002/012，角色权限见 004
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voc_opportunity_manual (
  opp_id        text PRIMARY KEY REFERENCES voc_opportunity(opp_id),
  status        text NOT NULL DEFAULT '考虑中'
                CHECK (status IN ('不考虑','考虑中','在跟进','项目中','已完成')),
  owner         text,
  note          text,
  decision_note text,                         -- 「不考虑」时必填（由 002/012 触发器强制）
  target_release text,
  release_date  date,                         -- 闭环验证锚点
  improved_product_ids text[],
  updated_at    timestamptz NOT NULL DEFAULT now(),
  updated_by    text
);
CREATE INDEX IF NOT EXISTS ix_manual_status ON voc_opportunity_manual (status, updated_at);

CREATE TABLE IF NOT EXISTS voc_status_log (
  log_id      bigserial PRIMARY KEY,
  opp_id      text NOT NULL REFERENCES voc_opportunity(opp_id),
  from_status text,
  to_status   text NOT NULL,
  reason      text,
  changed_by  text NOT NULL,
  changed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_slog_opp ON voc_status_log (opp_id, changed_at DESC);

-- ------------------------------------------------------------
-- 标签体系维表（每周快照，用于检测上游变更）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS voc_tag_taxonomy (
  tag        text NOT NULL,
  prod_line  text NOT NULL,
  tax_path   text,
  tax_l1     text,
  is_product boolean,
  is_scene   boolean,
  week       text NOT NULL,
  PRIMARY KEY (tag, prod_line, week)
);

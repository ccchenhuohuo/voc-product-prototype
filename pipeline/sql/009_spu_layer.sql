-- ============================================================
-- VOC 数据管道重构：SPU 容器、问题条目与共性度
-- 以 voc_admin 执行；计算层每周重算，人工层永不随重算覆盖。
-- ============================================================

ALTER TABLE voc_opportunity ADD COLUMN IF NOT EXISTS n_eff numeric;
ALTER TABLE voc_opportunity ADD COLUMN IF NOT EXISTS scope text;

-- 消息聚合与证据聚合必须分开：若先把 evidence 连接进来，消息数与均星会按
-- 一条消息的标签数重复计算。SPU 数组也先按 message_id 去重，防御上游重复值。
CREATE MATERIALIZED VIEW IF NOT EXISTS voc_spu AS
WITH base_message AS (
  SELECT DISTINCT
         m.message_id,
         NULLIF(btrim(s.spu), '') AS spu,
         m.product_name,
         m.sku,
         m.category,
         m.prod_line,
         m.product_grade,
         m.launch_period,
         m.star,
         m.publish_time
    FROM voc_message m
   CROSS JOIN LATERAL unnest(m.spu) AS s(spu)
   WHERE m.src_line = '电商'
     AND cardinality(m.spu) > 0
     AND NULLIF(btrim(s.spu), '') IS NOT NULL
), message_agg AS (
  SELECT spu,
         array_agg(DISTINCT product_name ORDER BY product_name)
           FILTER (WHERE product_name IS NOT NULL) AS product_names,
         mode() WITHIN GROUP (ORDER BY category)
           FILTER (WHERE category IS NOT NULL) AS category,
         mode() WITHIN GROUP (ORDER BY prod_line)
           FILTER (WHERE prod_line IS NOT NULL) AS prod_line,
         (array_agg(product_grade ORDER BY
            CASE regexp_replace(product_grade, '级$', '')
              WHEN 'PS' THEN 1 WHEN 'S' THEN 2 WHEN 'A' THEN 3
              WHEN 'B' THEN 4 WHEN 'C' THEN 5 WHEN 'D' THEN 6
              WHEN '其他' THEN 7 ELSE 8
            END,
            product_grade)
          FILTER (WHERE product_grade IS NOT NULL))[1] AS grade,
         -- 云听上市时间当前是 2024Q1 一类可字典序排序的标准值；字段仍保留 text
         -- 以兼容「2023及以前」等展示值，不擅自伪造日期精度。
         min(launch_period) FILTER (WHERE launch_period IS NOT NULL) AS launch_period,
         count(DISTINCT message_id) AS message_count,
         round(avg(star), 2) AS avg_star,
         min(to_char(publish_time, 'IYYY-"W"IW'))
           FILTER (WHERE publish_time IS NOT NULL) AS first_publish_week,
         max(to_char(publish_time, 'IYYY-"W"IW'))
           FILTER (WHERE publish_time IS NOT NULL) AS last_publish_week
    FROM base_message
   GROUP BY spu
), sku_value AS (
  SELECT DISTINCT b.spu, NULLIF(btrim(x.sku), '') AS sku
    FROM base_message b
   CROSS JOIN LATERAL unnest(COALESCE(b.sku, ARRAY[]::text[])) AS x(sku)
   WHERE NULLIF(btrim(x.sku), '') IS NOT NULL
), sku_agg AS (
  SELECT spu, array_agg(sku ORDER BY sku) AS skus
    FROM sku_value
   GROUP BY spu
), message_spu AS (
  SELECT DISTINCT message_id, spu FROM base_message
), evidence_agg AS (
  SELECT ms.spu,
         count(*) FILTER (WHERE e.sentiment = '负面') AS negative_evi_count,
         count(*) FILTER (WHERE e.sentiment = '正面') AS positive_evi_count
    FROM message_spu ms
    JOIN voc_evidence e USING (message_id)
   GROUP BY ms.spu
)
SELECT m.spu,
       m.product_names,
       s.skus,
       m.category,
       m.prod_line,
       m.grade,
       m.launch_period,
       m.message_count,
       COALESCE(e.negative_evi_count, 0) AS negative_evi_count,
       COALESCE(e.positive_evi_count, 0) AS positive_evi_count,
       m.avg_star,
       m.first_publish_week,
       m.last_publish_week
  FROM message_agg m
  LEFT JOIN sku_agg s USING (spu)
  LEFT JOIN evidence_agg e USING (spu);

CREATE UNIQUE INDEX IF NOT EXISTS ux_voc_spu_spu ON voc_spu (spu);

-- 阈值来自实测：每个 SPU/问题至少 2 条证据后，收敛为 161 个条目 / 51 张卡
--（2026-08-15 落库实测）。被挡掉的单证据仍保留在事实层与战略层。
CREATE MATERIALIZED VIEW IF NOT EXISTS voc_spu_issue AS
WITH attached AS (
  SELECT DISTINCT
         NULLIF(btrim(s.spu), '') AS spu,
         oe.opp_id,
         oe.message_id,
         oe.seq,
         oe.attach_week,
         e.tax_path
    FROM voc_opp_evidence oe
    JOIN voc_message m USING (message_id)
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
   CROSS JOIN LATERAL unnest(m.spu) AS s(spu)
   WHERE m.src_line = '电商'
     AND cardinality(m.spu) > 0
     AND NULLIF(btrim(s.spu), '') IS NOT NULL
)
SELECT spu,
       opp_id,
       count(*) AS evi_count,
       mode() WITHIN GROUP (ORDER BY tax_path)
         FILTER (WHERE tax_path IS NOT NULL) AS tax_path,
       min(attach_week) AS first_attach_week,
       max(attach_week) AS last_attach_week
  FROM attached
 GROUP BY spu, opp_id
HAVING count(*) >= 2;

CREATE UNIQUE INDEX IF NOT EXISTS ux_voc_spu_issue_key
  ON voc_spu_issue (spu, opp_id);
CREATE INDEX IF NOT EXISTS ix_voc_spu_issue_opp
  ON voc_spu_issue (opp_id);

-- 计算层可能因周度阈值变化消失，但 PM 的判断必须保留，因此 spu 不对物化
-- 视图建外键；opp_id 仍锚定长期保留的共性问题机器表。
CREATE TABLE IF NOT EXISTS voc_spu_issue_manual (
  spu                text NOT NULL,
  opp_id             text NOT NULL REFERENCES voc_opportunity(opp_id),
  status             text NOT NULL DEFAULT '考虑中'
                     CHECK (status IN ('考虑中','在跟进','项目中','已完成','不考虑')),
  owner              text,
  decision_note      text,
  baseline_evi_count int,
  target_release     text,
  release_date       date,
  closed_at          timestamptz,
  revived_at         timestamptz,
  revive_reason      text,
  updated_by         text,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (spu, opp_id)
);

CREATE INDEX IF NOT EXISTS ix_spu_issue_manual_status
  ON voc_spu_issue_manual (status, updated_at);

CREATE TABLE IF NOT EXISTS voc_spu_issue_log (
  log_id      bigserial PRIMARY KEY,
  spu         text NOT NULL,
  opp_id      text NOT NULL REFERENCES voc_opportunity(opp_id),
  from_status text,
  to_status   text NOT NULL,
  reason      text,
  changed_by  text NOT NULL,
  changed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_spu_issue_log_key
  ON voc_spu_issue_log (spu, opp_id, changed_at DESC);

-- 与一期状态审计同口径。baseline 必须在「不考虑」发生的那一刻落盘，
-- 因为 voc_spu_issue 下周刷新即覆盖，之后无法还原 3 倍复活判据的分母。
CREATE OR REPLACE FUNCTION voc_log_spu_issue_status() RETURNS trigger AS $$
DECLARE current_evi_count int;
BEGIN
  NEW.updated_at := now();

  IF TG_OP = 'UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
    RETURN NEW;
  END IF;

  IF NEW.status = '不考虑'
     AND (NEW.decision_note IS NULL OR btrim(NEW.decision_note) = '') THEN
    RAISE EXCEPTION
      '置为「不考虑」时 decision_note 必填（证据基准与后续复议依赖它）';
  END IF;

  IF NEW.status = '不考虑' THEN
    SELECT i.evi_count::int INTO current_evi_count
      FROM voc_spu_issue i
     WHERE i.spu = NEW.spu AND i.opp_id = NEW.opp_id;
    IF current_evi_count IS NULL THEN
      RAISE EXCEPTION
        '置为「不考虑」时找不到当前 SPU 问题条目，无法记录 baseline_evi_count';
    END IF;
    NEW.baseline_evi_count := current_evi_count;
  END IF;

  -- closed_at 只跟随「已完成」。离开该状态就清空：复活后仍挂着旧闭环时间
  -- 会让「上市后是否还有反馈」的判读凭空多一个假分界；历史留在审计日志里。
  -- 注意分界线本身取 release_date（上市日期）而非这里，见 spec §3 触发二。
  IF NEW.status = '已完成' THEN
    NEW.closed_at := COALESCE(NEW.closed_at, now());
  ELSE
    NEW.closed_at := NULL;
  END IF;

  INSERT INTO voc_spu_issue_log
         (spu, opp_id, from_status, to_status, reason, changed_by)
  VALUES (NEW.spu,
          NEW.opp_id,
          CASE WHEN TG_OP = 'UPDATE' THEN OLD.status ELSE NULL END,
          NEW.status,
          NEW.decision_note,
          COALESCE(NEW.updated_by, current_user));
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voc_log_spu_issue_status ON voc_spu_issue_manual;
CREATE TRIGGER trg_voc_log_spu_issue_status
  BEFORE INSERT OR UPDATE ON voc_spu_issue_manual
  FOR EACH ROW EXECUTE FUNCTION voc_log_spu_issue_status();

-- REFRESH 只有物化视图 owner 能执行。Dagster 使用 voc_writer，因此用固定
-- search_path 的 SECURITY DEFINER 薄函数收窄权限，而不是把视图所有权交给机器。
CREATE OR REPLACE FUNCTION voc_refresh_spu_layer() RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW public.voc_spu;
  REFRESH MATERIALIZED VIEW public.voc_spu_issue;

  -- 共性度使用未套用「成卡 >=2」阈值的全量挂靠，避免单证据产品被静默
  -- 丢掉而低估扩散；同一消息内重复 SPU 先去重，多 SPU 则各自计一次归属。
  WITH attached AS (
    SELECT DISTINCT
           oe.opp_id,
           oe.message_id,
           oe.seq,
           NULLIF(btrim(s.spu), '') AS spu
      FROM public.voc_opp_evidence oe
      JOIN public.voc_message m USING (message_id)
     CROSS JOIN LATERAL unnest(m.spu) AS s(spu)
     WHERE m.src_line = '电商'
       AND cardinality(m.spu) > 0
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

REVOKE ALL ON FUNCTION voc_refresh_spu_layer() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION voc_refresh_spu_layer() TO voc_writer;

GRANT SELECT ON voc_spu, voc_spu_issue
  TO voc_writer, voc_human, voc_reader;

GRANT SELECT ON voc_spu_issue_manual TO voc_writer, voc_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON voc_spu_issue_manual TO voc_human;

GRANT SELECT ON voc_spu_issue_log TO voc_writer, voc_reader;
GRANT SELECT, INSERT ON voc_spu_issue_log TO voc_human;
GRANT USAGE, SELECT ON SEQUENCE voc_spu_issue_log_log_id_seq TO voc_human;

COMMENT ON TABLE voc_spu_issue_manual IS
  'SPU 问题人工层。周度刷新只覆盖物化视图，机器对本表只读。';
COMMENT ON COLUMN voc_spu_issue_manual.baseline_evi_count IS
  '转为「不考虑」时的证据数；用于证据增长至 3 倍后的复议基准。';

-- 首次部署同时完成历史展开与 n_eff/scope 回填；重复执行只会按现状重算。
SELECT voc_refresh_spu_layer();

-- 只读自检：阈值与 NULL 语义是两条最容易被后续改动破坏的业务断言。
SELECT COALESCE(bool_and(evi_count >= 2), true) AS issue_threshold_holds,
       count(*) AS spu_issue_count
  FROM voc_spu_issue;
SELECT count(*) FILTER (WHERE n_eff IS NOT NULL) AS n_eff_covered,
       count(*) FILTER (WHERE (n_eff IS NULL) <> (scope IS NULL)) AS null_pair_errors,
       count(*) FILTER (WHERE n_eff IS NULL AND scope IS NULL) AS no_spu_unfabricated
  FROM voc_opportunity;

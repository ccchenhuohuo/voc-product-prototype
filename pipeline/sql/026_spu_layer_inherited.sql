-- 026：SPU 问题层与共性度纳入继承 SPU，并把计数单位从「证据行」改为「独立声音」
--
-- 背景：023 引入 voc_message.spu_inherited（社媒组级唯一继承推断），
-- classification._has_spu 与 025 的 voc_spu 容器都已按 `spu ∪ spu_inherited`
-- 判定归属。但 SPU 问题层的两处取数至今仍只 unnest(m.spu)：
--   * voc_spu_issue（015 定义）——产品卡上的问题卡；
--   * voc_refresh_spu_layer() 里的 n_eff/scope（018 覆盖版）——战略视图分档。
-- 后果：靠继承拿到 SPU 的社媒证据会把机会点路由进「老品迭代」（路由认继承），
-- 却在产品卡上不产出问题卡、也不计入 n_eff。机会点在列表里有，点进对应产品
-- 卡看不到——正是 018 修过的「相邻两页对同一份证据给出矛盾结论」。
--
-- 但只把 unnest 换成 `spu ∪ spu_inherited` 是不够的，还会引入两个计数错误
-- （2026-08-18 实测）：
--   1. 成卡阈值 HAVING count(*) >= 2 数的是 (message_id, seq) 对，
--      一条评论被打两个标签就能独立凑够门槛。现存 916 张卡中 21 张（2.3%）
--      全部证据来自同一条消息。继承会放大这个口子。
--   2. n_eff 同样按证据行计数。而继承是按消息组整组赋同一个 SPU：实测
--      437 个产生继承的组里，单组最大注入 196 条、68 个组 ≥10 条。一个热帖
--      就能让某 SPU 的 evi_count 压倒其余产品，把辛普森指数拉向 1，
--      使本该「品线级」的机会点被判成「单品」——恰好废掉战略视图那一层。
--
-- 因此本迁移同时改计数单位：
--   * 成卡阈值改为 count(DISTINCT message_id) >= 2，即「至少两个独立用户」；
--     evi_count 保留原义（证据行数，供前端显示热度），另加 msg_count 列。
--   * n_eff 按 (opp_id, spu, thread) 去重后计数，thread 取
--     COALESCE(message_group_id, message_id)：一个帖子的整条评论线程对某个
--     SPU 只贡献一票，热帖不再吞掉扩散度。电商行没有 message_group_id，
--     thread 退化为 message_id，逐条计票，口径与改造前一致。
--
-- 不改的东西：n_eff 仍不套成卡阈值（009 原意，避免单证据产品被静默丢掉而
-- 低估扩散）；仍不按 opp_type 过滤；分档阈值 1.5/3 不变；来源仍不按
-- src_line 过滤（018 已去掉该限制）。
--
-- 去重说明：backfill 函数保证 spu 非空时 spu_inherited 必为 NULL，两者不重叠；
-- 即便将来重叠，DISTINCT 也已按 (opp_id, message_id, seq, spu) 去重。
BEGIN;

SET LOCAL search_path = public, pg_temp;

-- I09：迁移必须以 voc_admin 执行，否则新建 MV 的 owner 与
-- SECURITY DEFINER 的 voc_refresh_spu_layer() 不一致，refresh 会在
-- 迁移之后才失败。这里 fail-fast，不留到运行期。
DO $guard$
BEGIN
  IF current_user <> 'voc_admin' THEN
    RAISE EXCEPTION '026 必须以 voc_admin 执行，当前为 %', current_user;
  END IF;
END
$guard$;

DROP MATERIALIZED VIEW IF EXISTS voc_spu_issue;

CREATE MATERIALIZED VIEW voc_spu_issue AS
WITH attached AS (
  SELECT DISTINCT
         NULLIF(btrim(s.spu), '') AS spu,
         oe.opp_id,
         oe.message_id,
         oe.seq,
         oe.attach_week,
         e.tax_path
    FROM voc_opp_evidence oe
    JOIN voc_opportunity o ON o.opp_id = oe.opp_id
    JOIN voc_message m USING (message_id)
    JOIN voc_evidence e
      ON e.message_id = oe.message_id AND e.seq = oe.seq
   CROSS JOIN LATERAL unnest(
          COALESCE(m.spu, ARRAY[]::text[])
       || COALESCE(m.spu_inherited, ARRAY[]::text[])
        ) AS s(spu)
   WHERE o.opp_type = '老品迭代'
     AND o.classification_state = '确定'
     AND NULLIF(btrim(s.spu), '') IS NOT NULL
)
SELECT spu,
       opp_id,
       count(*) AS evi_count,                        -- 证据行数：热度，口径同旧版
       count(DISTINCT message_id)::int AS msg_count, -- 独立声音数：成卡依据
       mode() WITHIN GROUP (ORDER BY tax_path)
         FILTER (WHERE tax_path IS NOT NULL) AS tax_path,
       min(attach_week) AS first_attach_week,
       max(attach_week) AS last_attach_week
  FROM attached
 GROUP BY spu, opp_id
HAVING count(DISTINCT message_id) >= 2;

CREATE UNIQUE INDEX ux_voc_spu_issue_key
  ON voc_spu_issue (spu, opp_id);
CREATE INDEX ix_voc_spu_issue_opp
  ON voc_spu_issue (opp_id);

GRANT SELECT ON voc_spu_issue TO voc_reader, voc_human, voc_writer;

-- n_eff/scope：同一口径 + 线程封顶。函数体其余部分照抄 018 的活定义。
CREATE OR REPLACE FUNCTION voc_refresh_spu_layer() RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
  REFRESH MATERIALIZED VIEW public.voc_spu;
  REFRESH MATERIALIZED VIEW public.voc_spu_issue;

  -- 共性度不套「成卡 >=2」阈值，避免单证据产品被静默丢掉而低估扩散。
  -- 来源口径与 voc_spu_issue 对齐：事实或组级继承挂了 SPU 就计入，不论渠道。
  -- 计票单位是【线程】而非证据行：社媒继承按组整体赋同一 SPU，一个热帖
  -- 可注入上百行（实测单组最大 196），按行计票会让该 SPU 独吞权重、
  -- 把辛普森指数拉向 1。电商无 message_group_id，thread 退化为 message_id。
  WITH attached AS (
    SELECT DISTINCT
           oe.opp_id,
           NULLIF(btrim(s.spu), '') AS spu,
           COALESCE(m.message_group_id, oe.message_id) AS thread
      FROM public.voc_opp_evidence oe
      JOIN public.voc_message m USING (message_id)
     CROSS JOIN LATERAL unnest(
            COALESCE(m.spu, ARRAY[]::text[])
         || COALESCE(m.spu_inherited, ARRAY[]::text[])
          ) AS s(spu)
     WHERE NULLIF(btrim(s.spu), '') IS NOT NULL
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

-- B16：建完新口径的物化视图必须当场刷新并自检，否则 n_eff/scope 会停在
-- 018 的旧结果，产品卡与战略分档长期矛盾（018 自身就是这么做的）。
SELECT voc_refresh_spu_layer();

DO $selfcheck$
DECLARE
  bad_scope bigint;
  bad_card  bigint;
BEGIN
  -- n_eff 与 scope 必须成对存在或成对为空
  SELECT count(*) INTO bad_scope FROM voc_opportunity
   WHERE (n_eff IS NULL) <> (scope IS NULL);
  IF bad_scope <> 0 THEN
    RAISE EXCEPTION '026 自检失败：n_eff/scope 不成对的机会点 % 条', bad_scope;
  END IF;

  -- 成卡阈值必须按独立消息数生效
  SELECT count(*) INTO bad_card FROM voc_spu_issue WHERE msg_count < 2;
  IF bad_card <> 0 THEN
    RAISE EXCEPTION '026 自检失败：msg_count<2 却成卡的行 % 条', bad_card;
  END IF;
END
$selfcheck$;

COMMIT;

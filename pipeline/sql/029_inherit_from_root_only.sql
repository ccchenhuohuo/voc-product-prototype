-- 029：SPU 继承收敛为「只继承原帖」
--
-- 背景：023 的 voc_backfill_social_spu_inheritance() 把整组的事实 SPU 聚合成
-- 并集，只要并集恰好一个值，就赋给组内所有没有事实 SPU 的消息——不区分
-- message_type。这是【同级继承】：一条评论被云听识别出的 SPU，会赋给同组
-- 其余所有评论。
--
-- 设计本意是父链继承（评论继承其上级），但 parent_id 不可用：25,482 条
-- 社媒消息带 parent_id，能解析到库内父消息的是 0 条（ID 命名空间与
-- message_id 不同）。当时退化成了组级平铺，退得过远——「只继承原帖」
-- 这一层其实用 message_group_id + message_type 就能实现，不需要 parent_id。
--
-- 实测污染（2026-08-19 生产库）：
--   · 抖音组 douyin-7622225804197856241 共 50 条全是评论，只有 1 条被识别出
--     A200（正文在聊竞品「有买永诺的200W灯 有买ulanzi的40W灯」），其余 49 条
--     全部继承 A200；内容是竞品吐槽、催新品、甚至扯到车企对比，无一在说 A200。
--   · 全库 437 个组产生继承、注入 3,109 条消息，单组最多注入 196 条。
--
-- 本迁移的口径变更：
--   事实 SPU 的采集范围从「组内所有消息」收敛为「组内 message_type
--   IN ('帖子','视频') 的消息」。组内没有原帖时不产生任何继承。
--
-- 预期影响（改动前实测）：
--   注入消息数 3,109 → 1,750（−43.7%），触发继承的组 2,021 → 1,892（−6.4%）
--   进入老品证据池的继承行 369 → 134（−63.7%）
--   被砍掉的 1,359 条构成：1,255 条「有原帖但 SPU 来自评论」（纯污染）
--                          + 213 条「组内无原帖」（业主已决议一刀切）
--
-- 本迁移只替换函数定义，不自动回填。回填由调用方显式执行
-- （ingest 的每个入库窗口后会调用，或手工 SELECT 该函数）。
BEGIN;

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
    -- 与 023 的唯一差异：事实 SPU 只从原帖/视频这一级采集。
    -- 组内无原帖，或原帖不带 SPU 时，spus 为 NULL，下方 CASE 落到 NULL 分支，
    -- 该组不产生继承（也会清掉此前遗留的继承值）。
    SELECT g.message_group_id,
           array_agg(DISTINCT NULLIF(btrim(x.spu), ''))
             FILTER (WHERE NULLIF(btrim(x.spu), '') IS NOT NULL) AS spus
      FROM social_groups g
      LEFT JOIN public.voc_message m
        ON m.src_line = '社媒'
       AND m.message_group_id = g.message_group_id
       AND m.message_type IN ('帖子', '视频')
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

COMMENT ON COLUMN voc_message.spu_inherited IS
  'SPU 继承推断：只从组内原帖/视频的事实 SPU 继承（029 起；023 原为组级平铺）。'
  '与云听事实列 spu 独立，两者永不互写。';

COMMIT;

-- ============================================================
-- SPU 抽取留痕字段。本迁移只扩充表结构，不回填存量数据，
-- 不刷新视图或物化视图。
-- ============================================================

BEGIN;

ALTER TABLE voc_message
  ADD COLUMN IF NOT EXISTS spu_raw text[];

ALTER TABLE voc_message
  ADD COLUMN IF NOT EXISTS spu_unmatched text[];

COMMENT ON COLUMN voc_message.spu_raw IS
  'SPU 抽取 token 在规范化前的原始值，按 token 留痕用于回溯。';

COMMENT ON COLUMN voc_message.spu_unmatched IS
  '规范化后为空（纯分隔符等无效值）的 token 留痕，不进入 spu。'
  '抽取层不按任何名单筛选 SPU：云听依本公司产品体系打标，挂到 SPU 即本品；'
  '「社媒独有 SPU 是否成卡」是 voc_spu 的展示层口径，不在抽取层销毁数据。';

COMMIT;

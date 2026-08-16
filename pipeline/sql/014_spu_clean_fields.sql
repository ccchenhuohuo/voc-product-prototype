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
  '规范化失败或未命中电商 SPU 白名单的 token 留痕，不进入 spu。';

COMMIT;

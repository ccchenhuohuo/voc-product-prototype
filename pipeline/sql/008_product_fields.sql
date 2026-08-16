-- ============================================================
-- VOC 数据管道重构：产品字段与标签四级
-- 以 voc_admin 执行；只扩列与回填，不删除一期兼容字段。
-- ============================================================

ALTER TABLE voc_message ADD COLUMN IF NOT EXISTS spu text[];
ALTER TABLE voc_message ADD COLUMN IF NOT EXISTS sku text[];
ALTER TABLE voc_message ADD COLUMN IF NOT EXISTS model text[];
ALTER TABLE voc_message ADD COLUMN IF NOT EXISTS launch_period text;
ALTER TABLE voc_message ADD COLUMN IF NOT EXISTS prod_line text;
ALTER TABLE voc_message ADD COLUMN IF NOT EXISTS is_own_brand boolean;

ALTER TABLE voc_evidence ADD COLUMN IF NOT EXISTS tax_stage text;
ALTER TABLE voc_evidence ADD COLUMN IF NOT EXISTS tax_domain text;
ALTER TABLE voc_evidence ADD COLUMN IF NOT EXISTS tax_sub text;
ALTER TABLE voc_evidence ADD COLUMN IF NOT EXISTS tax_leaf text;

-- tax_path 的第 1 段是树名，不是业务层级。固定按位置拆分，才能在中间
-- 缺层时保留真实层级；NULLIF 避免把空串伪装成一个有效分类。
UPDATE voc_evidence
   SET tax_stage  = NULLIF(btrim(split_part(tax_path, '/', 2)), ''),
       tax_domain = NULLIF(btrim(split_part(tax_path, '/', 3)), ''),
       tax_sub    = NULLIF(btrim(split_part(tax_path, '/', 4)), ''),
       tax_leaf   = NULLIF(btrim(split_part(tax_path, '/', 5)), '')
 WHERE tax_stage  IS DISTINCT FROM NULLIF(btrim(split_part(tax_path, '/', 2)), '')
    OR tax_domain IS DISTINCT FROM NULLIF(btrim(split_part(tax_path, '/', 3)), '')
    OR tax_sub    IS DISTINCT FROM NULLIF(btrim(split_part(tax_path, '/', 4)), '')
    OR tax_leaf   IS DISTINCT FROM NULLIF(btrim(split_part(tax_path, '/', 5)), '');

COMMENT ON COLUMN voc_evidence.tax_l1 IS
  'deprecated：历史值是标签树名，不是 5A 阶段；新查询请使用 tax_stage。';

-- 电商消息约 91.6% 有 SPU，GIN 支撑数组包含/重叠检索与后续卡片下钻。
CREATE INDEX IF NOT EXISTS ix_msg_spu ON voc_message USING GIN (spu);

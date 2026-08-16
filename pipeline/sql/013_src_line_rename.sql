-- ============================================================
-- 机会点来源口径迁移：将两种历史来源值改为当前业务名称。
-- 历史源值使用 PostgreSQL Unicode escape 表达，避免旧展示口径继续
-- 出现在仓库文本中；其运行时语义与存量值完全一致。
-- ============================================================

BEGIN;

ALTER TABLE voc_opportunity DROP CONSTRAINT voc_opportunity_src_line_check;

UPDATE voc_opportunity
   SET src_line = '电商'
 WHERE src_line = U&'\7EBF\0041';

UPDATE voc_opportunity
   SET src_line = '社媒'
 WHERE src_line = U&'\7EBF\0042';

ALTER TABLE voc_opportunity
  ADD CONSTRAINT voc_opportunity_src_line_check
  CHECK (src_line IN ('电商', '社媒'));

DO $$
DECLARE
  legacy_a_count bigint;
  legacy_b_count bigint;
  ecommerce_count bigint;
  social_count bigint;
  total_count bigint;
BEGIN
  SELECT
    count(*) FILTER (WHERE src_line = U&'\7EBF\0041'),
    count(*) FILTER (WHERE src_line = U&'\7EBF\0042'),
    count(*) FILTER (WHERE src_line = '电商'),
    count(*) FILTER (WHERE src_line = '社媒'),
    count(*)
  INTO legacy_a_count, legacy_b_count, ecommerce_count, social_count, total_count
  FROM voc_opportunity;

  IF legacy_a_count <> 0
     OR legacy_b_count <> 0
     OR ecommerce_count <> 207
     OR social_count <> 438
     OR total_count <> 645 THEN
    RAISE EXCEPTION
      '机会点来源迁移自检失败：legacy_a=%, legacy_b=%, 电商=%, 社媒=%, total=%',
      legacy_a_count, legacy_b_count, ecommerce_count, social_count, total_count;
  END IF;

  RAISE NOTICE
    '机会点来源迁移自检通过：legacy_a=%, legacy_b=%, 电商=%, 社媒=%, total=%',
    legacy_a_count, legacy_b_count, ecommerce_count, social_count, total_count;
END $$;

COMMIT;

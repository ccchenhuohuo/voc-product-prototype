-- 019：原声片段里的 JSON 空值哨兵——归一化触发器 + 历史回填
--
-- 云听把 JSON 空值原样打进平行数组：`[null, 支架松动, null]`。清洗层的
-- parse_array 按逗号切开后得到字符串 "null"——非空、非空白，于是
-- NULLIF(btrim(snippet), '') 挡不住它，generation_pool 的
-- COALESCE(snippet, content) 也不会回落到正文。结果是 LLM 收到的证据正文
-- 就是四个字母的 "null"。
--
-- 2026-08-17 实测污染面：
--   * voc_evidence 148,934 条里 23,608 条（15.9%）的 snippet 是字面量 'null'；
--   * 按渠道拆，「需求缺口」入池 7,914 行中 5,082 行（64.2%）中招，
--     「竞品对标」1,775 行中 308 行（17.4%），「产品体验」几乎未受影响
--     （该分支要求负面+产品，这类证据只有 58 条）；
--   * 这 23,608 条里 23,592 条（99.9%）的消息正文原本就在，
--     所以归一化之后 COALESCE 会自动回落到正文，是把内容捞回来而不是补数据。
--   * 哨兵只出现在原声片段一列：sentiment / tag / tag_raw / tax_path 以及
--     brands / content_type / spu 三个数组列实测均为 0。
--
-- 清洗层（clean.py 的 null_token）已在源头拦截，本迁移把权威放到数据库：
-- 任何写入路径——重跑、回补、手工修数——都不可能再把哨兵写进来。
BEGIN;

CREATE OR REPLACE FUNCTION voc_normalize_null_sentinel() RETURNS trigger AS $$
BEGIN
  -- 位置语义由 clean.py 的 explode 保证（三个平行数组按下标对应），
  -- 这里只做就地置空，不涉及任何元素增删。
  IF lower(btrim(NEW.snippet)) IN ('', 'null', 'none', 'nil', 'nan', 'undefined') THEN
    NEW.snippet := NULL;
  END IF;
  IF lower(btrim(NEW.snippet_raw)) IN ('', 'null', 'none', 'nil', 'nan', 'undefined') THEN
    NEW.snippet_raw := NULL;
  END IF;
  IF lower(btrim(NEW.sentiment)) IN ('', 'null', 'none', 'nil', 'nan', 'undefined') THEN
    NEW.sentiment := NULL;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voc_normalize_null_sentinel ON voc_evidence;
CREATE TRIGGER trg_voc_normalize_null_sentinel
  BEFORE INSERT OR UPDATE ON voc_evidence
  FOR EACH ROW EXECUTE FUNCTION voc_normalize_null_sentinel();

-- 历史回填。触发器已就位，这条 UPDATE 只是把存量行推一遍。
UPDATE voc_evidence
   SET snippet = snippet
 WHERE lower(btrim(snippet))     IN ('null', 'none', 'nil', 'nan', 'undefined')
    OR lower(btrim(snippet_raw)) IN ('null', 'none', 'nil', 'nan', 'undefined')
    OR lower(btrim(sentiment))   IN ('null', 'none', 'nil', 'nan', 'undefined');

-- 只读自检。
DO $$
DECLARE
  leftover bigint;
  recovered bigint;
BEGIN
  SELECT count(*) INTO leftover FROM voc_evidence
   WHERE lower(btrim(snippet))     IN ('null', 'none', 'nil', 'nan', 'undefined')
      OR lower(btrim(snippet_raw)) IN ('null', 'none', 'nil', 'nan', 'undefined')
      OR lower(btrim(sentiment))   IN ('null', 'none', 'nil', 'nan', 'undefined');
  IF leftover <> 0 THEN
    RAISE EXCEPTION '回填后仍有 % 行残留哨兵', leftover;
  END IF;

  -- 归一化之后这些证据应当能从消息正文取到文本，否则就是真的没内容。
  SELECT count(*) INTO recovered
    FROM voc_evidence e JOIN voc_message m USING (message_id)
   WHERE e.snippet IS NULL
     AND NULLIF(btrim(m.content), '') IS NOT NULL;
  RAISE NOTICE '片段为空但正文可回落的证据：% 条', recovered;
END $$;

COMMIT;

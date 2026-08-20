-- 027：voc_message 补整段情感列
--
-- 云听导出有两级情感：整段 MessageSentiment（消息情感）与按标签切分的
-- TagSentiment（标签情感）。片段级自 001 起就入 voc_evidence.sentiment 并
-- 参与电商生成池筛选（e.sentiment='负面'）；整段级此前从未映射，一直丢弃。
--
-- 2026-08-19 定论（片段/全文粒度排查）：一条评论「你内胆需要更新一下…
-- 前面网袋很不错。但是内胆没办法快取」被切出的片段「还有前面网袋很不错」
-- 单句情感为正面，整段实为抱怨——只有片段级情感时，这类形态无法察觉。
-- 本列先存不筛：G4 读完整正文自行判断，不依赖它；留作
-- 「整段 vs 片段情感背离」类交叉校验与后续分析的数据资产。
--
-- 执行顺序：必须先于携带 msg_sentiment 键的入库代码上线（upsert 按字典键
-- 生成列名，列不存在会让每次 ingest 直接失败）。回填靠下一次重灌，
-- 本迁移不回填、不阻塞现有数据。
BEGIN;

ALTER TABLE voc_message
  ADD COLUMN IF NOT EXISTS msg_sentiment text;

COMMENT ON COLUMN voc_message.msg_sentiment IS
  '云听整段情感（MessageSentiment）。与 voc_evidence.sentiment（按标签切分）'
  '互补；仅存储，当前不参与任何门的筛选。';

COMMIT;

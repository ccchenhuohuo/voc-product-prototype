-- 022：来源总量探针缓存——首页只读云听同窗口可用量，不在请求路径调 API
--
-- COMMENT / SOCIAL / SERVICE 各保留最近一次探针结果。探针脚本只请求
-- total=1，但云听任务元数据仍返回完整 matched_count；首页用该值与
-- voc_message 实际接入量对比。
BEGIN;

CREATE TABLE IF NOT EXISTS voc_source_probe (
  query_task_type text PRIMARY KEY,      -- COMMENT / SOCIAL / SERVICE
  matched_count   bigint NOT NULL,
  window_start    timestamptz NOT NULL,
  window_end      timestamptz NOT NULL,
  probed_at       timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE voc_source_probe IS
  '云听来源同窗口可用总量的离线探针缓存；首页只读，探针脚本按来源覆盖写。';

-- 管道角色负责探针写入；看板与只读角色只拿 SELECT。
GRANT SELECT, INSERT, UPDATE ON voc_source_probe TO voc_writer;
GRANT SELECT ON voc_source_probe TO voc_human, voc_reader;

-- 自检：读写边界必须与脚本/首页职责一致。
DO $$
BEGIN
  IF NOT has_table_privilege('voc_writer', 'voc_source_probe', 'INSERT')
     OR NOT has_table_privilege('voc_writer', 'voc_source_probe', 'UPDATE') THEN
    RAISE EXCEPTION 'voc_writer 仍无法写入 voc_source_probe';
  END IF;
  IF NOT has_table_privilege('voc_human', 'voc_source_probe', 'SELECT') THEN
    RAISE EXCEPTION 'voc_human 仍无 voc_source_probe 读权限';
  END IF;
  IF NOT has_table_privilege('voc_reader', 'voc_source_probe', 'SELECT') THEN
    RAISE EXCEPTION 'voc_reader 仍无 voc_source_probe 读权限';
  END IF;
END $$;

COMMIT;

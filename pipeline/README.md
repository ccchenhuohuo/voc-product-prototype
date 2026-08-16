# VOC 数据管道（`pipeline/`）

从云听 CEM 抽取 VOC，经清洗、生成、消解与 SPU 展开后写入 PostgreSQL，供 `system/` 的 PM 机会看板读取。生产调度入口是 Dagster；`scripts/` 下的命令用于初始化、回填、探针、备份和故障处置。

> 来源口径：`voc_message.src_line` 与 `voc_opportunity.src_line` 均使用 `电商/社媒`。`voc_opportunity.channel` 继续保存 `需求缺口/竞品对标`，不承担来源语义；社媒无产品占位符 `SOCIAL-NA` 保持不变。

## 运行要求与安装

- Python **3.11 或更高版本**
- PostgreSQL 18，扩展 `vector` 与 `pg_trgm`
- 本地开发需能访问配置的 PostgreSQL、云听和百炼接口

```bash
cd pipeline
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

最小运行环境变量：

```dotenv
YUNTING_MCP_KEY=<YOUR_YUNTING_API_KEY>
BAILIAN_API_KEY=<YOUR_BAILIAN_API_KEY>
VOC_PG_PASSWORD=<YOUR_VOC_WRITER_PASSWORD>
```

常用可选项及代码默认值：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `YUNTING_MCP_URL` | `https://ytcem.cn/mcp` | 云听 MCP 地址 |
| `VOC_PROJECT_ID` | 代码内现行项目 ID | 云听项目 |
| `BAILIAN_EMBEDDING_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 百炼兼容接口 |
| `VOC_EMBED_MODEL` | `text-embedding-v4` | 向量模型 |
| `VOC_CHAT_MODEL` | `qwen-plus` | 对话模型 |
| `VOC_LLM_CONCURRENCY` | `64` | 全局在飞 LLM 请求上限 |
| `VOC_PG_HOST` / `VOC_PG_PORT` | `127.0.0.1` / `5434` | 业务库地址 |
| `VOC_PG_DB` / `VOC_PG_USER` | `voc` / `voc_writer` | 业务库与机器角色 |
| `VOC_PG_HUMAN_USER` / `VOC_PG_HUMAN_PASSWORD` | `voc_human` / 空 | 仅人工表写入路径使用 |
| `VOC_AUTO_MERGE` | 未设置（关闭） | 设为 `1` 才启用静默自动融合 |

生产进程的规范密钥入口是 `/etc/voc-analytics/runtime.env`（权限 `0600`）。当前部署脚本仍会把源目录中的 `.env` 一并同步到发布目录，这是待修的密钥副本风险；不要依赖该副本，也不要把真实值写入仓库。

## 数据流与代码结构

```text
云听 COMMENT / SOCIAL
  -> ingest.py + clean.py
  -> voc_message / voc_evidence
  -> pipeline.py + stages/       生成机会点
  -> resolve.py + lifecycle.py   消解、提案、放行
  -> explode.py                  刷新 SPU 容器、问题条目与共性度
  -> PostgreSQL                  system/ 只读机器层、写人工层
```

```text
voc_analytics/        管线核心
  ingest.py           抽取：云听导出 -> xlsx -> voc_message / voc_evidence
  clean.py            清洗：字段映射、尾部修复、国别归一、SPU 规范化与原值留痕
  taxonomy.py         标签树快照与路径解析
  pipeline.py         编排：分桶 -> 生成机会点 -> 落库
  stages/stage1.py    问题模式切分（分批 + 最大团分解）
  stages/generate.py  机会点文本生成
  stages/validate.py  程序化校验
  resolve.py          字段等值候选、向量排序、L3 裁决与跨渠道汇聚
  lifecycle.py        生命周期候选逻辑、复活、陈旧检测、放行
  execute.py          执行 PM 已通过的提案并写血缘
  explode.py          刷新 SPU 容器、问题条目与共性度（纯 SQL）
  definitions.py      Dagster 资产图与周度调度定义
sql/                  按编号顺序执行的 001-014 迁移
scripts/              部署、抽取、生成、备份与探针
tests/                各里程碑验收
baseline/             L3 判定标注测试集
```

`lifecycle.check_tombstone()` 当前仅定义、尚未接入 Dagster 或脚本生成流程，因此不能把专用“墓碑抑制”路径视为已生效；是否接线或删除留待后续重构判断。

## 数据库迁移基线

现行迁移必须按编号顺序理解；实际应用需区分存量升级与新库：

| 编号 | 内容 |
|---|---|
| `001`–`004` | 基础表、触发器、视图、角色权限 |
| `005`–`007` | PM 看板、提案执行、放行时间与 PM 偏好 |
| `008` | 产品字段与标签四级字段 |
| `009` | `voc_spu`、`voc_spu_issue`、人工层、审计表与共性度 |
| `010` | 看板应用读取机器表的补充授权 |
| `011`–`012` | SPU 条目与机会点状态日志改为 AFTER 审计触发器 |
| `013` | `voc_opportunity.src_line` 统一为 `电商/社媒`，并校验已核实的存量分布 |
| `014` | 为 SPU 规范化增加原值与未匹配值留痕字段 |

`scripts/m0_deploy_pg.sh` 是灾难恢复所需的建库与角色初始化脚本，**当前只执行 `001`–`004`**；仓库没有自动迁移器。符合已核实 012 基线的存量库用 `013` 迁移来源值，再应用 `014`；新库的当前 `001` 已直接建立新来源约束，应跳过带固定 645 行断言的 `013`，但仍需在相应数据前置条件满足后建立其余结构并应用 `014`。本阶段只交付 `013/014` 脚本，未执行迁移；不要在未经备份、基线核对和变更审批时对生产库执行。

## 启动与常用入口

本地启动 Dagster 开发界面：

```bash
cd pipeline
dagster dev -m voc_analytics.definitions -p 3002
```

生产由三个 systemd 服务运行：`voc-analytics-grpc`（`127.0.0.1:4003`）、`voc-analytics-dagster-daemon`、`voc-analytics-dagster-webserver`（`127.0.0.1:3002`）。代码定义了 `voc_weekly` 的每周一 `02:00`（`Asia/Shanghai`）计划，但计划默认未启用，且当前定义没有显式选择上一周分区；部署后必须先启用并核对分区行为。部署和故障处置见 [运维交接](docs/运维交接.md)。

一次性抽取指定周（脚本接收**位置参数**；例行抽取由 Dagster 调度）：

```bash
cd pipeline
python scripts/run_ingest.py 2026-W33
python scripts/run_ingest.py 2026-W20 2026-W33 --backfill
```

脚本不会自行读取 `runtime.env`。生产人工执行前应由受控的服务环境或 shell 注入所需变量；不要打印、复制或提交密钥。生产回填须从 `/opt/ulanzi/voc-analytics/current` 使用已安装包运行。当前若干脚本仍把 `/home/sdy/voc-analytics` 插到 `sys.path` 首位，这是可能误载开发代码的已知风险，修正前需核对实际导入路径。

其他人工入口：

```bash
# 只读导出，检查云听字段填充率；需要云听配置和上游网络
python scripts/probe_fields.py

# 最小生成探针；基于库内事实层调用百炼，并写入后清理哨兵周 9999-W01
python scripts/smoke.py

# 管线清洗单测（不连数据库）
python tests/test_clean_fields.py
```

`scripts/run_generate.py` 与 `scripts/rerun_both.sh` 会调用 LLM 并写库，其中后者还会清理机会点机器层，只用于受控回填。用途、前置条件和参数以各脚本头部说明为准。

## 核心约束

- 业务规则在数据库里：状态机、锁语义、「不考虑」必填理由与安全类保护由触发器强制。
- 机器只写机器表：`voc_writer` 对人工表只读；人工决定不会被周度重算覆盖。
- Dagster 是唯一生产调度入口；脚本只用于初始化、开发调试、探针或受控回填。

## 当前状态与下一阶段

`008`/`009` 已在代码中完成产品字段、SPU 展开层和 `n_eff/scope`；`010`–`012` 已补齐应用读权及两个人工状态层的 AFTER 审计触发器。历史事实层是否已完成全量回填仅凭仓库无法确认，需由运维记录或数据库核验。

阶段一已在代码与迁移脚本中统一来源口径，并把 SPU 规范化与原值留痕落到抽取层。抽取层**不按任何名单筛选 SPU**——云听依本公司产品体系打标，挂到 SPU 即本品；「社媒独有 SPU 是否成卡」是展示层口径，留到阶段三的 `voc_spu` 里定。分类解耦与全量重跑仍以 [docs/rework_plan.html](docs/rework_plan.html) 的后续阶段为准。

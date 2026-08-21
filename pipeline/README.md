# VOC 数据管道（`pipeline/`）

从云听 CEM 抽取 VOC，经清洗、生成、消解与 SPU 展开后写入 PostgreSQL，供 `system/` 的 PM 机会看板读取。生产唯一入口是 `scripts/` 手工链：日常跑批与全量重建分别由 `run_ingest.py`、`run_generate.py` 和 `rerun_both.sh` 按 runbook 发起。

> 来源口径：`voc_message.src_line` 是 `voc_source_policy` 中已登记的证据来源；`voc_opportunity.src_line` 只是 legacy 单值代表，不能代表混合来源机会的完整事实。新建混合组取 `sorted(source_lines)[0]`，存量行 recount 不改写原值；权威来源事实读 `source_lines` / `evi_by_source`。来源字段不参与机会点身份。

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

生产进程的规范密钥入口是 `/etc/voc-analytics/runtime.env`（权限 `0600`）。历史部署脚本已归档并加阻断守卫；手工链从受控环境注入变量，不复制 `.env` 或把真实值写入仓库。

## 数据流与代码结构

```text
云听 COMMENT / SOCIAL
  -> ingest.py + clean.py
  -> voc_message / voc_evidence
  -> voc_assign_snapshot         冻结本轮证据归属
  -> db.generation_pool()        统一内容证据池（只读快照）
  -> classification.py          逐证据 R0–R3（来源 requires_spu 策略）
  -> routing.py + stages/        生命周期分桶、提示词与机会生成
  -> resolve.py + lifecycle.py   按生命周期消解、提案、放行
  -> explode.py                  刷新 SPU 容器、问题条目与共性度
  -> PostgreSQL                  system/ 只读机器层、写人工层
```

```text
voc_analytics/        管线核心
  ingest.py           抽取：云听导出 -> xlsx -> voc_message / voc_evidence
  clean.py            清洗：字段映射、尾部修复、国别归一、SPU 规范化与原值留痕
  classification.py   唯一纯函数：按 SPU 与来源 requires_spu 属性执行 R0–R3
  routing.py          生成前逐证据分类，按生命周期的内容键分桶
  taxonomy.py         标签树快照与路径解析
  pipeline.py         编排：统一池 -> 生命周期路由 -> 生成 -> 消解/落库 -> 对账
  stages/stage1.py    问题模式切分（分批 + 最大团分解）
  stages/generate.py  机会点文本生成
  stages/validate.py  程序化校验
  resolve.py          生命周期候选键、向量排序与老品 L3 裁决
  lifecycle.py        生命周期候选逻辑、复活、陈旧检测、放行
  execute.py          执行 PM 已通过的提案并写血缘
  explode.py          刷新 SPU 容器与 v2/v3 问题物化层（纯 SQL）
  scripts/run_generate.py  生成前提案落地、生命周期生成与收尾
  scripts/rerun_both.sh    默认不清库的全量双生命周期重建入口
sql/                  按编号管理的迁移（执行前须核对环境基线）
scripts/              部署、抽取、生成、备份与探针
tests/                各里程碑验收
baseline/             L3 判定标注测试集
```

`execute.run()` 在生成前消费 PM 已通过的本代提案，`lifecycle.check_revive()` 在收尾快照后、放行前提议本代 REVIVE。v3 已移除未接入生成链路的专用墓碑抑制分支。

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
| `013` | `voc_opportunity.src_line` 统一为 `电商/社媒`，并以固定 645 行分布断言保护存量迁移 |
| `014` | 为 SPU 规范化增加原值与未匹配值留痕字段；只扩结构，不回填存量 |
| `015` | 按完整证据集重算分类与 `evi_total`，增加分类审计字段，并收紧 `voc_spu_issue` |
| `016` | 生命周期隔离：对 PM 已接管的 locked 行冻结 `opp_type / classification_state / classify_rule` |
| `017` | 生成期生命周期路由：`voc_source_policy.requires_spu`、可扩展来源外键、`source_lines` 与 `evi_by_source` |
| `018`–`019` | `n_eff` 口径对齐与 JSON 空值哨兵归一化 |
| `020`–`023` | run log 读权、最近邻/来源缓存、社媒线索字段与 G4 缓存 |
| `024` | 删除已废弃的业务子类型列并按最终列契约重建看板视图 |
| `025`–`027` | 社媒 SPU 容器/继承挂载、独立声音计数与消息整段情感字段 |
| `029` | 社媒继承收敛为组内唯一根帖/视频来源 |
| `030`–`033` | v3 统一 SPU 判定、归属快照、双问题物化层与单写者快照准备函数 |

`scripts/m0_deploy_pg.sh` 是灾难恢复所需的建库与角色初始化脚本，仓库没有自动迁移器。**仓库迁移链已到 `033`（编号 `028` 未使用），但文件存在不等于任一数据库已应用。**实际应用必须按迁移记录和环境基线逐项确认，并在备份、变更审批后受控执行。

## 阶段 2a 分类契约

分类不再由机会点的渠道参数决定，而是对该机会点的证据集合按以下顺序求值，先命中先生效：

| 规则 | 条件 | `opp_type` | `classification_state` |
|---|---|---|---|
| R0 | 零条证据 | `NULL` | `无效` |
| R1 | 任一证据在冻结快照中有事实或根帖继承 SPU | `老品迭代` | `确定` |
| R2 | 无 SPU，且任一证据来源 `requires_spu=false` | `新品创新` | `确定` |
| R3 | 无 SPU，且全部证据来源均 `requires_spu=true` | `NULL` | `无效` |

`classification_state` 与 `opp_type` 是正交维度；无效不是 `opp_type` 的第三个取值。`classify_rule` 保存命中的 `R0/R1/R2/R3`，用于审计与回放。统一池对所有 `requires_spu=true` 的来源执行同一 R3 准入约束；电商只是当前该策略的一个实例。

生成代码先用当前内存分组的证据运行同一个纯分类函数，为当次生成提供分类；这不是最终权威结果。新建、挂载或 MERGE 改变 `voc_opp_evidence` 时，关系写入与完整证据集回算共用同一事务。v3 关系行投影冻结快照的 `assigned_spu / assignment_source / assign_run_id`，权威回算统一更新 `opp_type`、`classification_state`、`classify_rule`、证据计数和排序基础值；任一步失败则整体回滚，不会留下半更新。

`015_classification_contract.sql` 以完整证据集回填分类和 `evi_total`，并在 `voc_spu_issue` 中保留 `HAVING count(*) >= 2`。264 / 368 / 13 / 645 是该脚本的历史自检断言，不是本轮复核的生产结果；`015/016` 实际环境状态未验证。

## 阶段 2c 生成路由、ID 与失败契约

`rerun_both.sh` 在两个生命周期进程启动前，以共享 `run_id` 单写一次 `voc_assign_snapshot`；`db.generation_pool()` 的正式 v3 路径只投影这份快照。它以真实 `(message_id, seq)` 为证据身份，按产品体验、「用户使用体验」负面、需求缺口和竞品对标等内容条件取数。每条证据先经 `classification.py` 分类，再进入互斥生命周期桶：老品按 `assigned_spu` 分块并对多 SPU 证据扇出，新品按 claim 预聚类且不进入 L1/L2/L3。「用户使用体验」只是内容入池条件；有快照归属为 R1 老品，无归属的社媒/问卷证据为 R2 新品。依已定规则，有事实 SPU 的电商证据必为老品，无事实 SPU 则 R3 无效。

两条生命周期在 G 系列之后完全隔离，不做生成后缝合。同一 SPU 的电商与社媒老品证据在 SPU 桶内自然聚合；无 SPU 的社媒诉求进入新品，无法归属的产品缺陷落入明确终态。仅根帖继承 SPU 的诉求按既定 G5 规则降级为新品，代码不会从正文自行提取或猜测 SPU。

新建机会的 ID 为 `OPP2-` + SHA-256 前 128 bit。哈希材料是经 NFKC、大小写与空白归一的版本化 JSON：老品身份为 `(老品迭代, SPU, problem_mode)`；新品身份为 `(新品创新, NULL, problem_mode)`。来源、旧业务子类型、品类、周次、模型版本和 Stage1 临时名称均不参与身份。老品 L1 只召回同 SPU 的 `OPP2-*`，新品直接新建；存量 `OPP-*` 与 v3 命名空间隔离。

Arrearage、鉴权失败与硬配额/余额不足立即打开熔断，不重试、不继续；同一 run 的两个生命周期进程通过原子共享熔断文件传播首因，另一进程在下一次请求前停止。并行层传播原始异常并取消未开始任务。Stage1 把计划/完成/失败/取消批次与对应条数写入 run log，账本必须满足 `planned = completed + failed + cancelled`。两个生命周期生成进程任一非零时终止另一个；只有两者均成功才执行归属投影校验、拆分、快照、v2/v3 派生刷新和放行收尾。`rerun_both.sh` 依赖 Bash ≥5.1，并在停止进程或清库前先做版本闸门。

池规模的静态毛估为：旧社媒候选 `5,005 + 2,623 = 7,628` 条消息；「用户使用体验」3,249 条整桶 × 23.4% 负面率 ≈ 毛新增 760，即约 8,388（+10.0%）。多标签重叠、`low_conf`、非空门槛与消息/证据粒度差异使精确净增**未验证**；电商 4,232 是证据数，不能与上述社媒消息数直接相加。

## 启动与常用入口

生产跑批入口（手工链）：

```bash
cd pipeline
python scripts/run_ingest.py 2026-W33
bash scripts/rerun_both.sh
```

没有内置自动调度器或生产服务；跑批、回填、失败重试和全量重建都由运维按 [运维交接](docs/运维交接.md) 手工发起并核对 `voc_run_log`。

一次性抽取指定周（脚本接收**位置参数**）：

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

# 管线清洗 + R0–R3 + 2c 路由/ID/熔断单测（不连数据库）
python -m pytest tests/test_clean_fields.py tests/test_classification.py tests/test_lifecycle_routing.py tests/test_opportunity_identity.py tests/test_llm_fail_stop.py tests/test_stage1_accounting.py
```

`scripts/run_generate.py` 与 `scripts/rerun_both.sh` 会调用 LLM 并写库，其中后者还会清理机会点机器层，只用于受控回填。生成前钩子消费 PM 已通过的 MERGE/SPLIT 提案（静默自动融合仍需 `VOC_AUTO_MERGE=1`），收尾在快照后检测 REVIVE；两项计数都写入 `voc_run_log.metrics` 的 finalize 账本。脚本使用 `set -euo pipefail`；两个生命周期生成进程任一非零时立即终止另一个，并禁止 `--finalize-only` 收尾。真正的完整性依据是 run log 中每层计划/完成/失败/取消账本闭合，不是只看 shell 退出码。用途、前置条件和参数以各脚本头部说明为准。

## 核心约束

- 业务规则在数据库里：状态机、锁语义、「不考虑」必填理由与安全类保护由触发器强制。
- 机器只写机器表：`voc_writer` 对人工表只读；人工决定不会被周度重算覆盖。
- 老品迭代与新品创新互不转换；016 对 PM 已接管的 locked 行冻结三个分类字段，后续迁移不改动该行为。
- 新建 `OPP2-*` 与存量 ID 长期共存；没有专门迁移授权时禁止批量重键。
- fatal 或对账不闭合时必须以失败结束，不得写成功日志或执行收尾。
- 手工链是唯一生产调度入口；`run_generate.py` 生成前执行 PM 提案，收尾执行复活检测、派生层刷新和放行。

## 当前状态与下一阶段

`008`/`009` 已在仓库代码中完成产品字段、SPU 展开层和 `n_eff/scope`；`010`–`012` 已在仓库代码中补齐应用读权及两个人工状态层的 AFTER 审计触发器。这些描述只代表代码状态。历史事实层是否完成全量回填、`013/014` 是否已应用，均仅凭仓库无法确认，需由运维记录或数据库核验。

阶段一已在代码与迁移脚本中统一来源口径，并把 SPU 规范化与原值留痕落到抽取层。抽取层**不按任何名单筛选 SPU**——云听依本公司产品体系打标，挂到 SPU 即本品；「社媒独有 SPU 是否成卡」是展示层口径，留到阶段三的 `voc_spu` 里定。

阶段二现拆分为 2a / 2b / 2c。仓库迁移链已到 `027`；本报告只做代码与文档变更，不连库、不调 LLM、不重跑管道，实际环境状态与生成结果仍须由验收方按 runbook 核对。

# VOC 数据管道 v3 施工交付报告

## 1. 交付结论与边界

已按 `pipeline/docs/施工任务书_v3.md` 与 `pipeline/docs/架构规格_v3.md` 完成 v3 代码、迁移、离线测试和验收脚本。冻结的两份指令源未修改。

本轮只做仓库内施工，没有连接数据库、执行 SQL、调用 LLM/embedding、远程登录、部署、重跑生成或执行 git 操作。需要数据库的 `pipeline/tests/m4_v3_snapshot.py` 仅作为验收方手工用例交付，本轮未运行，因此本报告不声明任何生产数据结果。

## 2. 迁移文件

| 文件 | 改了什么、为什么 | 对应交付物 |
|---|---|---|
| `pipeline/sql/030_has_spu.sql` | 新增唯一 SQL 口径 `voc_has_spu(text)`，按事实 SPU 与根帖继承 SPU 的并集判定；可重跑地重算存量 R0–R3，并以非零即回滚的口径/分类自检保护。历史迁移 015 不改写。 | 任务书 §2.1 `030`；规格 §7.4 |
| `pipeline/sql/031_assign_snapshot.sql` | 建 `voc_assign_snapshot`、禁止 UPDATE、只允许按 run 整轮 DELETE，并撤销运行角色直接 INSERT（只能经 033 的单写者函数落盘）；为 `voc_opp_evidence` 增归属投影三列及约束/索引；增 `scope_source`、`denominator_scope`；重建 `ix_opp_bucket`；建只追加 `voc_opp_id_map`。 | 任务书 §2.1 `031`、§2.1bis、§2.4；规格 §4.2/§4.4/§9.1 |
| `pipeline/sql/032_spu_issue_v2v3.sql` | 将原问题层拆为 `_v2`、`_v3` 两个 MV 和初始指向 `_v2` 的普通兼容视图；v3 只读 `oe.assigned_spu` 且校验等于 `core_tag`；刷新函数保留独立容器 MV `voc_spu`，分别刷新两个问题 MV，不刷新普通视图；v3 清空旧 `n_eff/scope`，v2 维持历史口径；最近邻刷新限定 OPP2 命名空间。 | 任务书 §2.1 `032`、重点提示 2；规格 §3/§4.3/§9.2 |
| `pipeline/sql/033_snapshot_prepare.sql` | 新增带事务 advisory lock 的单写者、幂等快照准备函数；一条 `INSERT … SELECT` 冻结 G5 归属，fact 优先于 root；记录行数、唯一事实数和内容指纹；重试只核对原指纹后返回，绝不重置冻结基线；新增 finalize 前的指纹复核函数。 | 任务书 §2.1 `033`、§2.1bis；规格 §4.2 |

## 3. 管道代码

| 文件 | 改了什么、为什么 | 对应交付物 |
|---|---|---|
| `pipeline/voc_analytics/db.py` | 增快照准备/验证入口；正式生成池必须带 `assign_run_id` 并只投影该物理快照，G4 预热保留明确隔离的预快照路径。 | §2.1bis 唯一读口 |
| `pipeline/voc_analytics/classification.py` | v3 优先按冻结归属分类；数组读取仅保留给历史迁移与离线四状态夹具，正式取数不投影数组。 | §2.1bis；规格 §7.4 |
| `pipeline/voc_analytics/routing.py` | 老品桶键改为精确 SPU；多 SPU 扇出；贯穿 `assigned_spu/assignment_source`；同 SPU fact/root 冲突时 fact 优先；仅 root 的诉求按既定 G5 决议降级。 | §2.2 分块键与扇出；规格 §1/§4.2/§13.4 |
| `pipeline/voc_analytics/resolve.py` | 删除生成后跨源缝合；新品直接 create，不进入 L1/L2/L3；老品 L1 只召回同 SPU、同生命周期、OPP2 命名空间。 | §2.2；固定决策 3 |
| `pipeline/voc_analytics/pipeline.py` | 老品 `core_tag=SPU`、新品 `core_tag=NULL`；生成前验证快照并检查 `R=F`；关系写入归属三列；recount 按关系投影而非消息数组；finalize 校验快照来源、每卡唯一 SPU 和投影上界；v3 写 `scope_source='v3-未计算'`。 | §2.1bis、§2.2、重点提示 1 |
| `pipeline/voc_analytics/execute.py` | MERGE 复制证据时原样携带 `assigned_spu/assignment_source/assign_run_id`；执行入口可按代次过滤。 | §2.2 MERGE 归属贯穿 |
| `pipeline/voc_analytics/lifecycle.py` | 删除未接线的新品墓碑抑制函数；复活、放行增加代次过滤。 | §2.2；固定决策 4 |
| `pipeline/voc_analytics/config.py` | `MODE_MERGE_COS` 固定为 `0.75`。 | 固定决策 7 |
| `pipeline/voc_analytics/explode.py` | 同步双 MV 刷新后的返回统计：兼容层、v2、v3 与战略待计算数分开报告。 | §2.2；重点提示 2 |

## 4. 运行、探针与灰度脚本

| 文件 | 改了什么、为什么 | 对应交付物 |
|---|---|---|
| `pipeline/scripts/run_generate.py` | 所有提案执行、拆分、快照、复活、放行与收尾扫描限定 OPP2；删除跨源收尾循环；finalize 首先做归属投影校验；快照分母按当前 run 的 `assigned_spu` 计算并写 `denominator_scope`；刷新双派生层。 | §2.2 代次过滤/分母/收尾 |
| `pipeline/scripts/rerun_both.sh` | 默认不清库；清库仅显式开关为 1 且兼容视图已指向 v3 时允许；G4 预热后、两个子进程启动前由 supervisor 单写一次共享快照；两个生命周期共用 run ID。 | §2.1bis；§2.4 灰度硬边界 |
| `pipeline/scripts/smoke.py` | 删除跨源调用和旧别名；按 G4 预热→快照准备→冻结生成池顺序执行；清理按整轮快照进行。 | §2.2 smoke 清理 |
| `pipeline/scripts/probe_conservation.py` | 要求显式 run ID，按冻结快照口径报告 U/F/R，R 取真实路由结果。 | 规格 §5；§2.3 守恒测试 |
| `pipeline/scripts/probe_spu_bucketing.py` | 要求显式 run ID，直接读冻结快照；移除标签分桶与重新调阈值的实验分支。 | 固定决策 1/6/7 |
| `pipeline/scripts/audit_legacy_refs.py` | 新增只读旧引用盘点：覆盖 snapshot、NN 两端、proposal/lineage 数组及四张人工/日志表；输出“无映射行/ID”并在发现已裁决旧提案时返回非零。 | §2.4 旧引用审计 |

`pipeline/scripts/voc_backup.sh` 未修改：它执行全量导出并单独导出人工表，不包含 `REFRESH MATERIALIZED VIEW voc_spu_issue`，也不把该对象作为独立导出目标；其字符串命中只来自 `voc_spu_issue_manual` / `voc_spu_issue_log` 表名。对它强行改成 `_v2/_v3` 会改错对象。

## 5. 前端与现行说明

| 文件 | 改了什么、为什么 | 对应交付物 |
|---|---|---|
| `system/app/queries.py` | 产品列表、产品详情、问题原声、质量查询和 MCP SPU 证据读取统一按 `(spu, opp_id)` 与 `oe.assigned_spu` 连接；未承载原声只读当前已发布 v3 finalize 对应的物理快照；全表机会扫描增加 OPP2 边界；战略查询排除 `v3-未计算`；schema gate 增 v3 对象/列检查。 | §2.2、重点提示 3、规格 §4.2/§5.1/§9.2 |
| `system/app/routes/strategy.py` | 增 `scope_unavailable`，区分“v3 尚未计算”与普通空结果。 | §2.2 战略空状态 |
| `system/app/templates/strategy.html` | 明示 v3 战略口径未计算，不展示 v2 旧扩散值。 | §2.2；固定决策 5 |
| `system/app/templates/issue-voices.html` | 撤除“有效产品数”读数。 | §2.2；固定决策 5 |
| `pipeline/README.md` | 更新迁移链、快照、SPU 分块、新品身份、代次隔离和无跨源收尾的现行说明。 | §2.2 文档清理/交付说明 |
| `system/README.md` | 删除已放弃的新品墓碑抑制能力承诺。 | §2.2；规格 §2.5 |
| `pipeline/docs/pipeline_redesign.html` | 删除已放弃函数的现行能力描述，并把生命周期说明改为 v3 的隔离语义。 | §2.2；规格 §2.5 |

## 6. 测试文件及契约变更说明

没有删除测试，也没有把断言改宽。因 v3 契约变化而修改的既有测试如下；每一项都用新的确定性约束替换旧行为：

| 文件 | 改了什么；为什么不是放宽 |
|---|---|
| `pipeline/tests/test_classification.py` | 增冻结归属优先与 fact/root/空归属四状态；对非法投影继续 fail-fast。覆盖更强。 |
| `pipeline/tests/test_finalize_refreshes_spu_layer.py` | 删除对已废弃跨源补证和 v3 `n_eff` 回填的期待，改断言投影先验、双 MV 刷新、OPP2 过滤与新分母自检；是替换被明确废止的契约。 |
| `pipeline/tests/test_gate_migrations.py` | 在原 023–029 迁移契约之后新增 030–033 的函数、约束、双 MV/双刷新、代次边界、单写者与指纹静态断言；旧迁移断言全部保留。 |
| `pipeline/tests/test_generation_pool_contract.py` | 正式池从消息数组断言改为指定 run 的快照 JOIN，并单列预热例外；新增“正式路径不得读数组”的约束。 |
| `pipeline/tests/test_generation_reconciliation.py` | 夹具补齐归属守恒账本，新增 R/F 闭合为成功必要条件。 |
| `pipeline/tests/test_grounding.py` | 既有 grounding 夹具补齐 v3 归属字段，不移除任何 grounding 断言。 |
| `pipeline/tests/test_lifecycle_routing.py` | 标签桶预期改为精确 SPU 桶；新增电商+社媒同桶、多 SPU 扇出、fact 优先和 root 诉求降级。 |
| `pipeline/tests/test_manual_chain_contract.py` | 删除已废弃跨源步骤/别名断言，改为默认不清库、灰度禁清、单次快照准备、共享 run ID 与 OPP2 收尾边界。 |
| `pipeline/tests/test_opportunity_identity.py` | 老品身份断言改为 SPU，新增新品 NULL core 与相同 problem_mode 幂等。 |
| `pipeline/tests/test_persist_parallel.py` | 并行落库夹具补齐归属字段；原并行度和失败传播断言保留。 |
| `pipeline/tests/test_precluster_reconciliation.py` | 预聚类夹具补齐冻结归属账本；原聚类闭合断言保留。 |
| `pipeline/tests/test_resolve_lifecycle_keys.py` | 新品测试改为明确禁止调用 L1；老品候选限定同 SPU/OPP2，原 L2/L3 行为断言保留。 |
| `pipeline/tests/m35_stability.py` | 手工稳定性用例要求已准备的 run ID，避免读实时数组。 |
| `pipeline/tests/m4_lifecycle.py` | 仅清理已删除能力的免责说明；测试逻辑未削减。 |
| `system/tests/test_full_voice.py` | 产品问题原声断言改为 `oe.assigned_spu`，防止跨 SPU 泄漏。 |
| `system/tests/test_home_dashboard.py` | 首页机会扫描增加 OPP2 代次边界并验证 v3 状态。 |
| `system/tests/test_query_metrics.py` | 产品/原声查询新增物理快照与 `oe.assigned_spu` 断言；战略页新增明确空状态断言。 |

新增测试：

| 文件 | 覆盖内容 |
|---|---|
| `pipeline/tests/test_assignment_persistence.py` | `save_opportunity()` 归属三列落库和 MERGE 原样复制。 |
| `pipeline/tests/m4_v3_snapshot.py` | 供验收方在隔离数据库手工执行：运行角色无直接 INSERT 权限、四种 `voc_has_spu` 状态、双进程同 run、重试幂等、不同 run 隔离；本轮未执行。 |

## 7. 离线验收结果

- pipeline：`236 passed, 1 xfailed, 23 subtests passed`，共 237 个顶层用例，高于任务书记录的 223 个。
- system：`157 passed`，高于任务书记录的 156 个；仅有一个第三方测试客户端弃用警告。
- 对管道、脚本、测试和系统应用共 104 个 Python 文件执行只读 AST 语法检查，全部通过。
- `bash -n pipeline/scripts/rerun_both.sh` 与 `bash -n pipeline/scripts/voc_backup.sh` 通过。
- 仓库生产路径代码、现行 README/HTML 中 `cross_source_merge` / `check_tombstone` 零命中；冻结规格、任务书与历史审计记录保留原文。
- `system/app/queries.py` 中从 `msg.spu` / `m.spu` / `spu_inherited` 推导产品归属的模式零命中。
- 除不可改写的 009/018/026 历史迁移外，生产路径没有 `REFRESH MATERIALIZED VIEW voc_spu_issue`；032 只刷新 `_v2` 与 `_v3`。
- `MODE_MERGE_COS = 0.75`；生成/收尾脚本中旧跨源命令行别名零命中。

## 8. 发现的规格/任务书问题与本次处置

以下问题只按冻结范围做了最小实现，没有自行补产品决策：

1. **刷新规则文字冲突。**规格 §4.3 写 `voc_refresh_spu_layer()`“按当前指向刷新”，任务书 §2.1 与本次重点提示则明确要求分别刷新 `_v2`、`_v3`。本实现采用后者的具体验收要求，同时刷新两个 MV；兼容普通视图从不执行 REFRESH，`voc_spu` 仍独立刷新。
2. **“v2 定义原样改名”与 shadow 隔离冲突。**若 026 定义完全不加代次条件，OPP2 行会继续通过消息数组进入 `_v2`，与规格 §9.2 的两代隔离相冲突。本实现对 `_v2` 增 `OPP-*`、对 `_v3` 增 `OPP2-*`，其余列和聚合契约保持一致。
3. **§5 的 P 放行式与既有失败契约没有闭合。**`P >= F` 只有在每个路由行至少落到一张卡时成立；现有生成契约允许少量 Stage1/grounding 分组进入显式失败或 unclassified 终态，规格没有说明这些行如何仍写入关系表。实现严格阻断 `R != F`、跨 SPU 卡、缺失快照投影和投影超出快照，但没有伪造关系行来满足 `P >= F`；请验收方裁定是收紧生成失败契约，还是修订 P 的口径。
4. **`voc_opp_id_map` 缺少填充算法。**规格要求建表和逐表盘点，却没有定义旧卡裂分到新卡时如何确定映射。本次只交付只追加表与只读缺口审计，不依据标题/向量自行猜映射。
5. **兼容视图不足以单独完成前端回滚。**任务书要求新前端的产品证据一律按 `oe.assigned_spu` 读取，但存量 OPP-* 关系行的新增归属列为 NULL，且一条 v2 关系可能对应多个消息数组 SPU，无法原位 1:1 回填。因此只把 `voc_spu_issue` 切回 `_v2` 能恢复问题列表，却不能保证新前端的 v2 原声/质量明细同时恢复；规格 §9.3“改一句视图即可秒级回滚”的消费面描述不完整。本次未自行增加第二个兼容对象或猜测 v2 归属，等待验收方确定回滚时是否同步回退应用代码或补充兼容派生层。
6. **任务书对备份脚本的调用点描述与仓库不符。**该脚本没有刷新兼容对象，只有名称包含该前缀的人工表；因此没有修改。完整导出仍会自然包含新表与两个 MV 的定义/数据。
7. **非实质元数据偏差。**任务描述称规格为 819 行，仓库冻结文件实际为 864 行，标题和状态均为 v3.2“已放行施工”。施工以实际完整文件为准。

## 9. 验收方后续动作

按硬边界，本轮未代替验收方执行以下动作：依序应用 030–033、运行只读旧引用盘点、执行 `m4_v3_snapshot.py`、独立复算守恒/投影、刷新两个 MV、完成 shadow 验收后再原子切换兼容视图。切换 SQL、部署与生产重跑不包含在本次产出中。

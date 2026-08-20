# 架构规格 v3 深度对抗性审计

审计对象：`pipeline/docs/架构规格_v3.md`。审计日期：2026-08-19。

本审计只读了规格、`/tmp/spec_facts_for_audit.md`、仓库代码/迁移/查询和既有审计材料；没有连接数据库、执行 SQL、调用 LLM/向量接口或修改生产。生产数字只把事实表中明确给出的值当作事实；需要当前库复采的地方单独标出 SQL 口径。

## 逐问结论索引

| 问题 | 结论 | 详见 |
|---|---|---|
| A1 身份约束/触发器 | 结构约束大多仍成立，但没有 v3 身份唯一性；派生层语义不成立 | B1、I1、已验证 1-2 |
| A2 多 SPU 主键/对账/总数 | 关系主键允许扇出；现有对账缺少事实到 SPU 实例的守恒式，14.2% 不能直接换算总数 | B2-B3、I2 |
| A3 `spu_inherited` | 组级误继承会把整组证据污染到错误产品，当前没有回溯补偿 | I3、I11、I13 |
| A4 opp_id/快照/NN/lineage | 重键会删除或悬空历史；快照分母还会因 tag/SPU 语义错配而失真 | B3、B9-B10、I7、I10 |
| B5 SPU 桶分组质量 | 共有标签未实际注入提示词；混合问题域会增加误合并/漏合并 | B7、I6 |
| B6 小桶规模 | `MIN_EVIDENCE` 不是程序闸门，卡片数上界接近路由实例数 | I4 |
| B7 热门 SPU | 0.85 只做跨批候选召回，357 行跨度下未经验证 | I5、S2 |
| C8 删除跨源汇聚 | 同桶不保证同卡，老品 L1/L2/L3 是否兜底必须写明 | I6 |
| C9 新品墓碑 | 直接 create 会绕过现有（且未接线的）墓碑抑制 | B6 |
| C10 派生层/前端 | `voc_spu_issue` 有大量未列消费者，必须先建兼容层再切换 | B5、I8-I9 |
| D11 改动遗漏 | 至少遗漏 `execute.py`、`explode.py`、`prompts.py`、015/018/026 统一口径、MCP 和测试脚本 | B7、I7-I9、I13、S4 |
| D12 对账恒等式 | v2 行级恒等式不足以验证 v3 扇出完整性 | B2 |
| D13 达标标准 | §6 没有可执行门槛 | B8 |
| D14 回滚 | 没有可逆的 ID/派生层/读路径方案 | B9 |
| E 其他 | 派生问题层会四象限投影，`src_line` 兼容值会误导下游 | B1、I12 |

## 阻断

### B1. 多 SPU 扇出与现有问题层的语义不相容

**依据：**规格 §1.2 `pipeline/docs/架构规格_v3.md:42-46`、§4 `:150`；`pipeline/voc_analytics/routing.py:56-60,126-130` 当前仍按 tag/topic；`pipeline/voc_analytics/pipeline.py:747-765` 将桶键写入 `core_tag`；`pipeline/sql/026_spu_layer_inherited.sql:53-85` 的 `voc_spu_issue` 按消息数组展开。

**失败场景：**一条社媒证据的 `m.spu={A,B}`，v3 路由把它复制到 A、B 两个桶，各桶生成 `opp_A`、`opp_B`。现有物化视图对每个机会点再次 `unnest(m.spu || m.spu_inherited)`，于是同时产出 `(A,opp_A),(B,opp_A),(A,opp_B),(B,opp_B)`。A 产品页出现 B 桶的问题，B 产品页也出现 A 桶的问题；这不是“跨 SPU 共性被保留”，而是问题卡归属错误。`(spu,opp_id)` 唯一索引不会报错，因为四行键各不相同。

**建议修法：**必须把“本证据被分配给哪个 SPU”作为持久化关系的一部分（新增 `opp_id,message_id,seq,assigned_spu` 关系及唯一键），或让 v3 的 SPU 问题层以老品 `o.core_tag` 作为唯一目标 SPU，仅在消息的事实/继承并集中验证它；禁止继续对整条消息的全部 SPU 做无条件投影。跨 SPU MERGE 也必须被拒绝。迁移自检要求 `opp_A` 只能出现在 A，且继承 SPU 仍可追溯。

### B2. v3 没有新的扇出对账恒等式，现有账本可能“全绿但漏卡”

**依据：**规格 §1.2 `:45-46`、§6 `:167-172` 没有定义扇出账本；`pipeline/voc_analytics/pipeline.py:463-541,554-569` 只检查已经进入路由/Stage1 的行；`pipeline/voc_analytics/stages/stage1.py:363-395` 只对桶内下标守恒。

**失败场景：**一条 `(message_id,seq)` 应扇出到 A、B，但实现只复制到 A。所有“已路由行”都被 Stage1 处理，`selected_evidence_rows == s1_input`，生成对账通过；B 的缺失永远不在当前账本中。反过来，若同一 SPU 被数组重复值或重跑复制，当前账本也只会把增加的副本当作新的合法行。

**建议修法：**在规格中冻结四个口径并写入 `run_log.metrics`：

* `U`：G5 后老品唯一证据键数 `count(distinct(message_id,seq))`；
* `F`：每条证据的 `spu ∪ spu_inherited` 去空、去重后的基数之和；
* `R`：实际生成的 `(message_id,seq,assigned_spu)` 路由实例数；
* `P`：落库关系中老品的上述三元组数。

必须同时满足 `R=F`、`P=R`、每个实例恰有一个 `assigned_spu`，以及 Stage1 `input_rows=R`。还要分生命周期、SPU、来源分别对账。14.2% 是消息级多 SPU 比例，不足以推出证据级增量；当前快照必须由验收方补采 `U/F`（SQL 口径就是上述 CTE，按 `generation_pool` 的 G1-G5 条件固定后再统计），并记录 `fanout_factor=F/U`。

### B3. 现有派生视图的唯一键“成立”，但业务身份仍不成立

**依据：**规格 §0 `:16-18`、§1.2 `:57-60`；`pipeline/sql/001_schema.sql:136-147` 的关系主键为 `(opp_id,message_id,seq)`；`pipeline/sql/026_spu_layer_inherited.sql:75-90` 的问题层键为 `(spu,opp_id)`。

**失败场景：**同一证据在 A、B 两个机会点中各有一行关系，数据库不会冲突；但 `evi_total`、`dual_source`、`weak_evidence` 会在两个机会点各自计数，产品页又按消息数组把两个机会点投影到两个 SPU。实施者若以“主键没有冲突”作为验收，会把四象限错误当成成功。

**建议修法：**把 B1 的分配关系作为数据库契约，增加针对 v3 身份的唯一性/归属自检；`voc_derive_flags` 可继续按机会点计算，但全局质量指标必须同时报告“机会点证据行数”和“唯一事实键数”，禁止把二者混称为证据总数。

### B4. 新品 `core_tag` “claim 或 NULL 二选一”会产生两套 opp_id 宇宙

**依据：**规格 §4 `pipeline/docs/架构规格_v3.md:143-147`；`pipeline/voc_analytics/pipeline.py:22-42` 的哈希输入包含 `core_tag`；`pipeline/voc_analytics/resolve.py:23-35` 的 L1 也按 `core_tag` 精确相等。

**失败场景：**同一新品模式第一次按 claim 写入、下一轮按 NULL 写入，`make_opp_id()` 生成两个不同 ID；任何尚未迁移的 L1/墓碑查询在统一写 NULL 时又会把所有新品视作同一 NULL 桶；若写 claim，claim 文本的微小归一化差异又会制造多个身份。规格允许实施时再选，无法保证重跑幂等、快照连续或回滚可逆。

**建议修法：**在施工前选择并冻结一个身份契约，建议把 `claim_cluster_id` 作为独立、版本化字段，`core_tag` 对新品固定为 NULL（或明确固定为规范化 cluster id，不能直接用可变文案），并将身份版本写入 `opportunity_identity_key()`、迁移脚本、快照和验收测试。不得把二选一留给实施阶段。

### B5. `voc_spu_issue` 退役没有安全的中间态，页面会直接报错或空白

**依据：**规格 §1.4 `:78-80`、§4 `:148-150`、§5 `:163-165`；`system/app/queries.py:6-31,222-254,289-344,505-680,733-792,845-1041,1137-1233`；`system/app/routes/spu.py:13-40`、`system/app/routes/issue.py:17-81`；`pipeline/voc_analytics/explode.py:7-15`；`pipeline/sql/026_spu_layer_inherited.sql:51-90,153-175`。

**失败场景：**先 `DROP MATERIALIZED VIEW voc_spu_issue` 再发布前端，首页侧栏、SPU 列表、问题详情、状态写入、复活查询和 MCP 的 SQL 都直接引用该对象，请求收到 `relation does not exist`。反方向只改前端而不先建立新派生层，刷新期间问题列表为空；`n_eff/scope` 若停止写入，战略页仍按 `scope` 过滤，可能显示旧数据或零行。

**建议修法：**先建立 v3 兼容派生对象（保留 `spu,opp_id,evi_count,msg_count,tax_*` 和历史状态所需列），完成全量刷新和自检；再一次性切换 `system/app/queries.py`、MCP 查询、路由、模板和 schema gate；灰度验证后才停用旧视图。`voc_spu` 容器必须保留并继续使用 `spu ∪ spu_inherited`，不能随问题层一起删除。

### B6. 新品直接 `create` 会明确绕过墓碑抑制，且仓库中没有其他调用补上它

**依据：**规格 §2.3 `pipeline/docs/架构规格_v3.md:107-120`；`pipeline/voc_analytics/lifecycle.py:76-92` 定义 `check_tombstone()`；全仓库无其调用点；`pipeline/voc_analytics/pipeline.py:873-880` 只调用 `resolve.resolve_one()`；`lifecycle.py:95-135` 的唤醒依赖快照和 `voc_status_log`。

**失败场景：**一个新品机会曾被 PM 置为「不考虑」，下一轮同一 claim 再来，`resolve_one()` 被改成直接返回 create，且保存路径没有 `check_tombstone()`；于是新建第二张卡，旧墓碑不会抑制，也不会累计到可 REVIVE 的同一条记录。现状函数本身已是未接线能力，v3 会把缺口固化为显式行为。

**建议修法：**规格必须明确新品是否有墓碑生命周期。若保留，生成前对新品调用独立的 tombstone 规则（不能依赖被移除的 L1/L2/L3），并规定命中后的证据挂载、REVIVE 提案和 ID 关系；若放弃，删除/改写 `check_tombstone()`、状态文档、快照基线和前端“复议”契约，而不是静默丢失抑制。

### B7. “共有标签作提示词特征”在现行 Stage1 输入中没有实现契约

**依据：**规格 §1.2 `pipeline/docs/架构规格_v3.md:48-55`；`pipeline/voc_analytics/pipeline.py:78-94` 将 `bucket.topic` 写入 `ctx_info.tag`；`pipeline/voc_analytics/stages/stage1.py:156-161` 只渲染品类/标签/语义路径；`pipeline/voc_analytics/stages/stage1.py:131-153` 的单条元数据没有 tag；`pipeline/voc_analytics/prompts.py:21-41` 只收到一行桶上下文。

**失败场景：**SPU A 的 8 条证据同时涉及电池、结构和外观。v3 桶上下文会显示 `标签:A`（实际是 SPU，不是共同标签），单条证据也不带 tag；模型在 50 条混合输入中只能按措辞猜域，电池故障与外观问题可能被合并，或同一问题被拆成多个模式。规格中的 81 个共有标签没有任何数据结构、提示词格式或版本化要求。

**结论：**与 v2“同标签即同问题域”的桶相比，v3 的桶内可比性先验更差，不能据此声称分组更好；84.4% 是共有标签覆盖的行数，不是 SPU 桶内语义纯度，也不能证明标签提示足以补偿。

**建议修法：**把 `common_tags` 的计数/排序/覆盖率定义写进任务书，提示词显式传入“桶内标签直方图”和每条证据的标签（只作特征，不作硬分块），将 `prompts.py` 加入改动清单并升 prompt 版本。探针必须验证有/无该特征的成对结果。

### B8. §6 的“探针达标”没有可执行的放行门槛

**依据：**规格 §6 `pipeline/docs/架构规格_v3.md:167-172` 只规定抽 10 个 SPU 和“达标”，没有指标、样本标注、阈值或失败动作。

**失败场景：**探针产出 10 个 SPU、程序没有异常，实施者即可宣布达标；但其中 8 个 SPU 的跨源同问题被拆成两卡，最大 SPU 的跨批合并漏召回，仍会进入全量施工。

**建议修法（建议直接写入规格）：**

* 路由：`R=F`、零缺失/重复分配、零跨 SPU 问题卡；
* 分组：对分层抽样的至少 200 个证据对做人审金集，pairwise precision ≥0.90、recall ≥0.80；同一问题跨电商/社媒的召回 ≥0.90；
* 热门桶：27 个 `>50` SPU 全部跑，模式合并 false-merge ≤0.05、漏合并 ≤0.10；最大 357 条桶单独达标；
* 稳定性：同输入重复运行 pairwise ARI ≥0.90；
* 规模：老品 `n≥2` 桶的单证据模式率不高于 25%，且不高于 v2 基线 18% 超过 5 个百分点；Stage1/持久化失败和 unclassified 均有显式计数，不能静默跳过。

未达到任一门槛则只保留探针结果，不施工；阈值由业主确认后固化为自动验收脚本。

### B9. 回滚路径完全缺失，且当前设计动作不可逆

**依据：**规格 §1.4 `pipeline/docs/架构规格_v3.md:80-85`、§4 `:145-151`、§6 `:167-172`；`pipeline/scripts/rerun_both.sh:287-299` 会删除机会点并截断 snapshot/proposal/lineage；`pipeline/sql/001_schema.sql:179-197,225-247` 的快照/人工外键。

**失败场景：**v3 全量写入 OPP2 后发现跨源同问题拆卡率恶化。旧机会行已经删除，旧快照因 `ON DELETE CASCADE` 一并删除，旧人工表若有行又会阻断清库；没有旧 ID 到新 ID 的映射，无法把前端读路径切回 v2。

**建议修法：**施工前建立只追加的 `legacy_opp_id → v3_opp_id[]` 映射和完整 v2 导出；用 feature flag/兼容视图双读，先 shadow 生成 v3，再切读；保留旧 opp、snapshot、状态和 NN，质量门不通过只切回旧视图并删除 v3 shadow。若必须物理重建，先定义恢复备份、派生层刷新、NN 重建和人工状态恢复顺序，并将其作为演练验收项。

### B10. 快照分母 SQL 仍把 SPU 当云听标签，v3 快照会全部失真

**依据：**规格 §4 `pipeline/docs/架构规格_v3.md:143-147`；`pipeline/scripts/run_generate.py:133-149`；`pipeline/sql/001_schema.sql:178-197`。

**失败场景：**v3 机会 `o.core_tag='SPU-A'`，事实证据的 `e.tag='电池'`。收尾快照中的 `base_total` 和 `neg_total` 仍用 `e.tag IS NOT DISTINCT FROM o.core_tag`，两者不相等，结果每张 v3 快照分母为 0。后续周对比、墓碑基线和任何依赖分母的报表都会把真实变化当成零。

**建议修法：**快照分母改为 v3 明确定义的 SPU 证据集合（按 `assigned_spu`/事实或继承 SPU 过滤，并按唯一 `(message_id,seq)` 去重），增加 `snapshot_schema_version`/`denominator_scope`，禁止复用 v2 的 tag 分母 SQL。迁移验收必须抽查 `base_total >= neg_total` 且与冻结的 SPU 池重算一致。

## 重要

### I1. 现有约束/触发器大多仍能运行，但没有 `(SPU,problem_mode)` 的数据库唯一性

**依据：**规格 §0 `:16-18`、§4 `:145-147`；`pipeline/sql/001_schema.sql:85-130`；`pipeline/sql/002_triggers.sql:10-40,77-110`；`pipeline/sql/015_classification_contract.sql:43-69`。

**失败场景：**同一 SPU 的两批 LLM 分别输出“磁吸脱落”和“磁吸片脱落”，归一化后仍不同，两个机会点都成功插入。`opp_id` 哈希只阻止完全相同的规范化字符串，不能阻止语义重复；`ix_opp_bucket` 是普通索引，不是唯一约束。

**结论：**`opp_id` 主键、`opp_type` CHECK、`voc_derive_flags`、安全合并触发器在结构上仍成立；`voc_guard_locked` 还会保护已锁定行的 `core_tag`。但它们不保证 v3 业务身份唯一。

**建议修法：**在应用层保留 L3 判定并增加迁移前重复检测；若业务要求数据库硬保证，新增规范化身份列/唯一索引（排除 merged tombstone 并处理人工历史），不要直接对可变中文 `problem_mode` 建唯一索引。

### I2. 多 SPU 关系不会违反主键，但证据总数不再等于唯一事实数

**依据：**事实表 `spec_facts_for_audit.md:24-25`（挂 SPU 消息 27,868、多 SPU 3,957、14.2%）；`pipeline/sql/001_schema.sql:136-147`；`pipeline/sql/002_triggers.sql:100-110`；`pipeline/sql/015_classification_contract.sql:71-112`；`pipeline/sql/003_views.sql:66-76`。

**失败场景：**一条证据挂到 A、B 两个机会点后，两个机会点各自 `evi_total=1`、`weak_evidence=true`；全局覆盖率查询用 `count(DISTINCT(message_id,seq))`，而产品页按机会点关系行数显示，两个数字自然不同。若把 14.2% 直接乘 6,489，会得到错误的增长量，因为多 SPU 比例是消息级，且每条消息有可变 seq 数。

**建议修法：**同时保留 `evi_total`（机会点关系行）和 `unique_fact_count`（唯一事实键），指标、验收和前端明确显示口径。验收方需在冻结 G5 池后补采 `U/F`、每消息 SPU 基数分布和 `count(*)` 关系行；不能从 14.2% 单独推断最终总数。

### I3. `spu_inherited` 的错误会污染整个组，而不是一条评论

**依据：**`pipeline/sql/023_social_thread_columns.sql:35-97` 只在组内 SPU 并集恰好一个时继承；`pipeline/sql/026_spu_layer_inherited.sql:12-30` 记录了单组最多注入 196 行、68 组至少 10 行的实测；`pipeline/voc_analytics/classification.py:17-29,67-72` 把继承视为老品归属；`pipeline/sql/026_spu_layer_inherited.sql:67-85` 将其做产品卡成卡依据。

**失败场景：**组内唯一 SPU 是由于上游漏标而“唯一”，该 SPU 被写入 `spu_inherited`；同组 196 条社媒证据全部进入该 SPU 桶并可形成问题卡，产品页显示另一产品的问题。当前函数只会在以后出现事实 SPU 或组冲突时清空推断列，不会撤销已经生成的机会点关系。

**建议修法：**保留 `assigned_spu_source=fact|inherited` 和组 ID，产品页默认区分继承证据；为继承设置抽样人工核验、误继承率/污染行数指标，发现冲突时自动删除或重算受影响机会点。误继承率没有在事实表给出，验收方需补采：按 `message_group_id` 统计唯一事实 SPU、继承 SPU、人工复核标签，明确分母。

### I4. 中位数 8 的小桶没有程序闸门，存在卡片爆炸上界

**依据：**事实表 `spec_facts_for_audit.md:6-7`（277 SPU、6,489 行、中位 8、p90 49）；`pipeline/voc_analytics/config.py:151-155` 明确 `MIN_EVIDENCE` 只是提示词变量；`pipeline/voc_analytics/stages/stage1.py:164-190,343-395` 没有按最小证据数过滤；新品提示词还允许单条成组 `pipeline/voc_analytics/prompts.py:45-53`。

**失败场景：**8 条 SPU 桶中模型将 8 条都输出为单条模式，代码照样生成 8 个机会点；277 个桶的保守下界是 277 卡，上界是每个路由实例一张卡，最多接近 6,489 卡（若 6,489 是扇出后的行数；若它是原始行，需再乘实际 fanout）。

**建议修法：**探针输出 `groups_per_spu`、singleton fraction、unclassified fraction 和 `cards_per_100_evidence`，以 v2 同口径对比；对老品明确单证据是否允许、允许时的展示/降级规则。不要把提示词中的 `MIN_EVIDENCE` 当程序约束。

### I5. 0.85 模式名合并阈值尚未证明适用于 357 行 SPU

**依据：**规格 §1.2 `pipeline/docs/架构规格_v3.md:49-54`；`pipeline/voc_analytics/stages/stage1.py:33-35,398-432`；`pipeline/voc_analytics/config.py:182-184`；`stage1.py:38-57,90-97` 在大图时退化为贪心最大团，组上限 40。

**失败场景：**357 条桶被切成约 8 个 50 行批次；同一失效模式若在不同批次命名差异超过 0.85，永不进入 LLM 合并候选，产生多卡；异质 SPU 中多个“电池/结构”短名余弦超过 0.85，又会触发成对判定并可能误合并。大图使用贪心团分解后，批内证据级召回已经不可逆，模式名合并只能补一部分。

**建议修法：**对 27 个热门 SPU 逐一报告批内 pairwise 召回、跨批候选召回、误合并/漏合并和 LLM 调用数；必要时按 `tax_domain` 做候选分层或使用可解释的两阶段聚类。0.85 只能作为候选阈值，不能在未达标前写成“沿用即安全”。

### I6. 删除 `cross_source_merge` 后，同桶混合不等于跨源同问题必然合并

**依据：**规格 §1.2 `pipeline/docs/架构规格_v3.md:42-55`、§1.3 `:68-70`；`pipeline/voc_analytics/resolve.py:23-35,76-107`；`pipeline/voc_analytics/stages/stage1.py:22-41`。

**失败场景：**同一 SPU 的电商原声“第三次使用后支撑腿断裂”和社媒原声“我的也断了”进入同一桶，但 Stage1 生成两个模式名；若 `resolve_one` 的 L2 Top-k/L3 没判同，删除收尾跨源补证后两张卡并存。分桶只保证候选在同一桶，不能保证同一问题卡。

**建议修法：**规格明确老品是否保留 `resolve_one` 作为 SPU 内跨批/跨源兜底；若保留，增加跨源同问题 gold-set 召回门槛和 `resolve` 指标；若称 Stage1 为唯一聚合层，就必须增加确定性同桶二次归并，不能用“天然混合”代替验收。

### I7. `execute.py`、`explode.py` 和迁移后的执行器不在代码清单中

**依据：**规格 §5 `pipeline/docs/架构规格_v3.md:153-165`；`pipeline/voc_analytics/execute.py:27-71,107-147` 会按 `opp_ids` 做 MERGE、写 lineage；`pipeline/voc_analytics/explode.py:7-15` 读取 `voc_spu_issue`、`n_eff`；`pipeline/scripts/run_generate.py:150-168` 调用二者；`pipeline/sql/006_execute.sql:5-20` 和 `pipeline/sql/021_opp_nn_cache.sql:28-59` 依赖 opp_id。

**失败场景：**47 条现有 `voc_proposal`（事实表 `spec_facts_for_audit.md:27-33`）仍引用旧 ID；v3 清库后执行器读取不到参与者，逐条进入 `failed`，或被截断后无法追溯。即使当前 `voc_opp_lineage=0`，未来 accepted MERGE/SPLIT 也会继续使用旧 ID 形状。

**建议修法：**把 `execute.py`、`explode.py`、`sql/006_execute.sql`、`sql/021_opp_nn_cache.sql` 和相关 smoke/pytest 加入任务书；明确旧 proposal 是映射、拒绝还是归档；重建 NN 后必须验证两端 ID 全存在。

### I8. `voc_refresh_spu_layer()` 的删减会与 `explode.py`、旧迁移和前端字段不一致

**依据：**规格 §4 `pipeline/docs/架构规格_v3.md:147-150`；`pipeline/sql/018_n_eff_source_alignment.sql:24-75`、`pipeline/sql/026_spu_layer_inherited.sql:95-149` 仍写 `n_eff/scope`；`pipeline/voc_analytics/explode.py:10-15` 仍统计 `n_eff_count`；`system/app/queries.py:21,767,960,1019,1143-1151` 和模板 `system/app/templates/issue-voices.html:18` 仍读取它们。

**失败场景：**只删除函数里的 n_eff 计算而保留 `explode.refresh()` 的统计 SQL，收尾在刷新后报列/视图契约错误；只停止写入而不清除旧值，新的 SPU 级机会点继续显示上一轮跨 SPU 的 `scope`，首页战略计数与实际 v3 身份不一致。

**建议修法：**先确定战略页“冻结旧数据”还是“明确空状态”；为 v3 写单独迁移函数和返回结构，不在 018/026 上做半截覆盖；将 `explode.refresh()`、run log、查询和模板一起版本化。

### I9. 前端清单漏掉了大量 `voc_spu_issue`/`n_eff` 消费者，MCP 也会受影响

**依据：**规格 §5 `pipeline/docs/架构规格_v3.md:163-165` 只点名少数行；实际 `system/app/queries.py:6-31,222-344,505-680,733-792,845-1041,1137-1233,1390-1829,1935-2224` 仍广泛引用；`system/mcp_app/tools/{spus,find,voices,overview,catalog,bundle}.py` 通过 `app.queries` 调用这些常量；`system/mcp_app/schema_gate.py:8-67` 检查对象存在。

**失败场景：**产品页看似已迁移，但 MCP 的 `find_spus`、`expand_voices`、状态/复活查询仍 JOIN 旧视图；schema gate 或线上工具请求失败。若只把模板中的“有效产品数”删掉，SQL 仍会在数据库层报错。

**建议修法：**用仓库级引用清单逐个迁移所有 SQL 常量、MCP 字段契约、schema gate、路由和测试；发布前用 `rg -n 'voc_spu_issue|n_eff|scope'` 作为静态门，允许保留的兼容对象必须注明保留期限。

### I10. 状态/墓碑历史的多 SPU 重键不是一对一迁移

**依据：**规格 §1.4 `pipeline/docs/架构规格_v3.md:80-85`；`pipeline/sql/001_schema.sql:225-249`、`pipeline/sql/009_spu_layer.sql:132-165`；`pipeline/voc_analytics/lifecycle.py:95-103`。

**失败场景：**v2 一张跨 3 个 SPU 的问题卡有一条 `voc_opportunity_manual` 状态和一条墓碑日志；v3 会裂成 3 个 `(SPU,problem_mode)`。把同一状态复制给三张卡会制造三份 PM 决策，选一张又会让另外两张无基线；旧 snapshot 只有一个 opp_id，无法自动选择对应 SPU。

**建议修法：**在迁移任务书定义“跨 SPU 旧卡”的拆分策略（复制、人工确认或只归战略历史），为每个新卡写 `legacy_opp_id`、`legacy_spu` 和状态来源；墓碑 baseline 必须按新 SPU 重新采样，不能简单复制旧 `evi_total`。

### I11. 继承数组的“事实 SPU 优先”必须贯穿 v3 路由、派生和页面

**依据：**`pipeline/sql/023_social_thread_columns.sql:66-95`（事实存在时清空继承）；`pipeline/sql/025_spu_scope.sql:7-19`（容器使用并集）；`pipeline/sql/026_spu_layer_inherited.sql:67-85,116-120`（问题层和 n_eff 使用并集）；`system/app/queries.py:539-552,758-760,902-914`（页面也自行使用并集）。

**失败场景：**某消息在全量重跑期间先有继承 SPU、后补到事实 SPU；路由和页面在不同时间窗口读取到不同数组，机会点 core_tag 是旧继承 SPU，但页面查询按新事实 SPU过滤，卡片忽隐忽现。

**建议修法：**生成前固定一次继承快照（run_id/input hash），关系层保存归属来源；同一轮的路由、派生、页面验收全部使用该快照，事实补标后下一轮再重算并记录受影响 opp_id。

### I12. `src_line` 的兼容投影在混合桶中仍是单值，需防止下游误读

**依据：**`pipeline/voc_analytics/pipeline.py:744-759` 将混合来源的 `src_line` 取排序第一项；`pipeline/sql/001_schema.sql:87-89` 仍允许单值；`pipeline/sql/017_generation_lifecycle_routing.sql:107-110` 注释称 `source_lines` 才是完整集合。

**失败场景：**同一 SPU 卡同时有电商、社媒证据，排序后 `src_line='电商'`；未迁移的报表按 `src_line` 过滤，误以为它只有电商证据，导致双源覆盖率/来源图错误。

**建议修法：**规格明确 `src_line` 仅 legacy display，所有 v3 质量/前端 SQL 强制使用 `source_lines/evi_by_source`；在迁移自检中检查 `dual_source = (jsonb_object_keys(evi_by_source) 正数来源数 >= 2)`。

### I13. SQL 分类契约与 Python 的继承口径不一致

**依据：**规格 §1.2 `pipeline/docs/架构规格_v3.md:36-46`；`pipeline/voc_analytics/classification.py:17-29,67-72` 同时检查 `spu`/`spu_inherited`；`pipeline/sql/015_classification_contract.sql:71-103` 的 `has_spu` 只检查 `cardinality(m.spu)>0`；`pipeline/sql/018_n_eff_source_alignment.sql:36-46` 也只展开 `m.spu`，需依赖 026 才覆盖继承。

**失败场景：**社媒消息只有 `spu_inherited=['SPU-A']`。Python 路由把它送入老品；若 v3 迁移/回填复用 015 的 SQL，`has_spu=false`，同一机会被标成新品或 R2，随后被排除在老品问题层之外。SQL 自检与运行时 `recount()` 会给出相反生命周期。

**建议修法：**新增单一数据库函数/视图统一计算 `spu ∪ spu_inherited`，015、018、026、快照和验收全部调用该口径；迁移测试至少覆盖“事实 SPU、继承 SPU、冲突/空数组”四种状态。不要把 026 的覆盖当作 015 已经正确。

## 建议

### S1. 为 SPU 归属增加可审计来源和数据质量指标

在 `voc_opp_evidence` 或旁路关系中记录 `assigned_spu`、`assignment_source`（fact/inherited）、`message_group_id`、`run_id`。每轮报告事实行、扇出行、跨 SPU 行、继承行和继承污染抽样结果；否则产品页无法解释一条声音为什么同时出现在多个问题卡。

### S2. 把热门桶做成容量基准，而不是只看总耗时

`stage1.py:38-57,398-432` 在节点数大时使用贪心团分解，模式名合并又是近似二次候选。对 357 行桶记录 token、LLM calls、批次失败、候选对数和最终组数；必要时限制候选窗口、按标签特征分层或增加确定性预聚类，并把资源上限写入探针标准。

### S3. 固化 v3 prompt/身份版本和可复现样本

`prompts.py:8` 当前版本为 `v1.3.0`，而 v3 要新增 SPU/common-tags 上下文。应升版本、保存输入 hash、SPU fanout 快照和金集标注；不能用同一 prompt 版本混淆 v2/v3 的 `mode_vec` 和快照。

### S4. 更新手工脚本、注释和测试入口

`pipeline/scripts/run_generate.py:89-168`、`pipeline/scripts/rerun_both.sh:387-395` 仍把收尾描述为“汇聚 + 拆分”，参数也保留 `--skip-cross` 别名；`pipeline/scripts/smoke.py:1-10,279-295` 仍主动调用跨源汇聚。规格应列出脚本、smoke、pytest 合同的迁移或归档，避免值班人员按旧语义执行。

### S5. 保留兼容列可以，但必须规定“旧值/空值/新值”的可见性

如果按 §4 保留 `n_eff/scope`，给它们加 `data_version` 或 `scope_source`；查询必须过滤版本，不要让 v2 的旧 scope 混入 v3 产品页。战略页已接受无新数据，但不能以静默旧数据冒充 v3。

## 已验证无误

以下结论经过静态代码/DDL核对，不需要因 v3 身份改动而重复怀疑；仍需按上面的新口径做运行验收：

1. `pipeline/sql/001_schema.sql:136-147` 的关系主键确实允许同一 `(message_id,seq)` 挂到多个 `opp_id`；不会因多 SPU 扇出本身触发主键冲突。冲突是业务派生语义，不是数据库唯一性错误。
2. `pipeline/sql/002_triggers.sql:100-110` 的 `voc_derive_flags` 只按每个机会点的统计字段派生 `weak_evidence/dual_source`；只要 recount 仍基于权威关系表，触发器机制无需因 SPU 键形状重写。
3. `pipeline/voc_analytics/classification.py:17-29,67-72` 已把 `spu` 与 `spu_inherited` 作为同一 R1 归属口径；v3 不应再引入第三种生命周期。
4. `pipeline/sql/023_social_thread_columns.sql:48-77` 的继承函数只有组内事实 SPU 并集恰好一个才继承，并在事实 SPU 出现或组冲突时清空；它不是任意猜测多个 SPU 的函数。问题在误继承后的影响面和缺乏补偿，不在该条件本身。
5. `pipeline/sql/025_spu_scope.sql:7-19` 的 `voc_spu` 容器已经包含社媒事实/继承 SPU；v3 退役问题卡层时，容器仍可作为产品页实体全集。
6. `pipeline/voc_analytics/resolve.py:23-35` 的 L1 查询不按来源分区；若老品继续使用 L1/L2/L3，SPU 作为 `core_tag` 后可在同一 SPU 内召回候选。它不能替代 B7/B8 所要求的分组质量保证。
7. `pipeline/sql/021_opp_nn_cache.sql:34-59` 每次收尾会全量清空并重建最近邻，旧 ID 不会因缓存表保留而“自动继承”；但重键后必须把 NN 刷新放在新派生层之后并做两端悬空检查。
8. 事实表中的规模、标签覆盖、L1 空转、14.2% 多 SPU 和人工表行数均已与规格逐项对照；本报告没有把运行中库行数当成最终快照，也没有重复主张既有审计已确认的路线/调度结论。

## 验收方需补采的最小数据

以下均为只读 SQL/标注口径，本文未执行：

* **扇出：**在冻结 G5 结果集建立 `eligible` CTE，再用 `CROSS JOIN LATERAL unnest(COALESCE(m.spu,ARRAY[]::text[]) || COALESCE(m.spu_inherited,ARRAY[]::text[])) x(spu)`，按 `(message_id,seq)` 计算 `count(DISTINCT NULLIF(btrim(x.spu),''))`。输出 `U`、`F`、`F/U`、每 SPU 分位数和每来源分布；同时从 v3 关系表输出 `P` 并核对 `P=F`。
* **继承：**按 `message_group_id` 计算事实 SPU 并集、继承 SPU、继承行数，抽取所有“唯一继承”组按组人工判定，报告 `false_inherit_groups / sampled_unique_groups` 及受影响证据行。
* **历史引用：**分别对 `voc_opp_snapshot`、`voc_opp_nn` 两端、`voc_proposal.opp_ids`、`voc_opp_lineage.parent_ids/child_ids`、四张人工/日志表做旧 ID 映射；重点报告旧 ID 无 v3 映射的数量，而非只看当前行数是否为 0。
* **质量金集：**对 10 个探针 SPU（含 357 行最大桶）及全部 27 个 `>50` 桶抽取跨域、跨源、单证据样本，由验收方标注“同模式/不同模式”，按 B8 的 precision/recall/稳定性阈值判定。

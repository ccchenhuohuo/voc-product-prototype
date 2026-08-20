# 架构规格 v3.1 闭环复审

复审对象为 `pipeline/docs/架构规格_v3.md`（v3.1）。本复审只读仓库文件和冻结事实表 `/private/tmp/spec_facts_for_audit.md`，未连接数据库、执行 SQL、调用 LLM/向量 API，也未执行 git 操作。

## 一、闭环判定表

| 编号 | 判定 | 一句话依据/缺口 |
|---|---|---|
| B1 | 部分解决 | §4.2 增加归属字段并要求 MV 改读 `oe.assigned_spu`（规格:212-253），但没有物理归属快照或原子刷新方案；现行 MV 仍按消息数组展开（`pipeline/sql/026_spu_layer_inherited.sql:53-85`）。 |
| B2 | 部分解决 | U/F/R/P 已定义（规格:273-307），但 `P>=F` 与三元组计数只是数量约束，没有把预期三元组集合与落地集合做 anti-join；现有账本只核对已进入 Stage1 的行（`pipeline/voc_analytics/pipeline.py:536-541`）。 |
| B3 | 部分解决 | 规格区分关系行与唯一事实（规格:273-297），但没有数据库级 `(SPU,problem_mode)` 身份唯一性，且普通 `ix_opp_bucket` 仍非唯一索引（规格:318-327；`pipeline/sql/001_schema.sql:127-145`）。 |
| B4 | 部分解决 | 新品 `core_tag=NULL` 已冻结（规格:132-147），但身份材料仍把可变 `problem_mode` 纳入 `version:3`；当前哈希契约确实包含这些字段（`pipeline/voc_analytics/pipeline.py:22-42`），没有 v3.0/v3.1 身份版本隔离。 |
| B5 | 处置有误 | 保留对象和换数据源方向正确（规格:231-253），但规格没有定义 flag 配置表、MV 查询如何读取 flag、刷新时的原子性或 v2/v3 并存对象；仅写“feature flag”不能形成可执行切换，现行 MV 仍从消息数组展开（`pipeline/sql/026_spu_layer_inherited.sql:51-85`）。 |
| B6 | 已解决 | 规格明确撤销新品墓碑抑制并要求删函数及文档（规格:160-173）；当前函数确实无生产调用，M4 仅是“不验证”注释（`pipeline/voc_analytics/lifecycle.py:76-92`；`pipeline/tests/m4_lifecycle.py:4-10`）。这是功能撤销，不是恢复抑制能力。 |
| B7 | 部分解决 | 共有标签从“现状”改为待实现并列入提示词改动（规格:337-353），但未冻结标签来源、计数去重、排序并列规则和成对试验的通过阈值；现行桶上下文只有 category/prod/tag（`pipeline/voc_analytics/pipeline.py:78-94`；`pipeline/voc_analytics/stages/stage1.py:156-176`）。 |
| B8 | 未解决 | 规格补了指标和数值（规格:394-433），却没有金集生成、pair 定义、false-merge/漏合并分母或自动脚本契约；因此验收方不能独立计算 8.2/8.3，现有 Stage1 只做内部账本（`pipeline/voc_analytics/stages/stage1.py:343-395`）。 |
| B9 | 处置有误 | shadow、映射、回滚顺序已写（规格:437-467），但“同名 `voc_spu_issue` + feature flag 读 v2/v3”没有配置/刷新/原子读路径契约；现有重跑脚本仍清空 snapshot/proposal/lineage（`pipeline/scripts/rerun_both.sh:287-299`），快照又对机会点有级联外键（`pipeline/sql/001_schema.sql:178-197`）。 |
| B10 | 部分解决 | 已要求按 `assigned_spu`、唯一事实键重写并加 `denominator_scope`（规格:380-390），但没有可直接执行的分母 SQL、快照列迁移和冻结集合比对；当前实现仍以 `e.tag = o.core_tag` 计数（`pipeline/scripts/run_generate.py:133-149`）。 |
| I1 | 部分解决 | 规格承认不加唯一约束并改由 L3/重复检测承担（规格:318-327），但重复检测没有放行阈值或失败动作；现有索引仍是普通索引（`pipeline/sql/001_schema.sql:127-130`）。 |
| I2 | 部分解决 | U/F 与双口径报告已补齐（规格:273-314），但没有按 SPU/来源的集合级核对，且仍沿用 B2 的不足；现有全局账本没有 assigned-SPU 维度（`pipeline/voc_analytics/pipeline.py:463-541`）。 |
| I3 | 部分解决 | `assignment_source` 和继承抽样指标已写（规格:214-229、299-307），但没有 `message_group_id` 关系留痕，也没有发现误继承后自动删除/重算受影响机会点的动作；继承函数只改消息列（`pipeline/sql/023_social_thread_columns.sql:48-97`）。 |
| I4 | 部分解决 | 规格明确 `MIN_EVIDENCE` 非程序闸门并增加基线比较（规格:423-433），但仍没有老品单证据的确定性处置或卡数硬上界；配置也明确只是提示词变量（`pipeline/voc_analytics/config.py:151-155`）。 |
| I5 | 部分解决 | 27 个热门桶和 357 行桶的探针范围已写（规格:409-417），但误合并/漏合并仍无金集定义；357 行会实际走贪心退化（`pipeline/voc_analytics/stages/stage1.py:38-42`），结果质量不能仅靠调用数验收。 |
| I6 | 部分解决 | 老品保留 `resolve_one` 且增加跨源召回门槛（规格:73-76、403-407），但没有说明门槛测 Stage1 还是 resolve，也没有分别记录两者召回；现有 L1 只按 `(core_tag,opp_type)` 且 L2 最多取 10 个（`pipeline/voc_analytics/resolve.py:17-35`）。 |
| I7 | 部分解决 | `execute.py`、`explode.py`、006/021 和 47 条 proposal 处置已列入清单（规格:355-375、444-453），但旧 ID 在 snapshot/NN/lineage 两端的映射写入和悬空失败动作仍未冻结；执行器当前只按原 `opp_ids` 改挂（`pipeline/voc_analytics/execute.py:34-71`）。 |
| I8 | 部分解决 | 规格要求同步 explode、独立 v3 刷新函数和战略空状态（规格:255-269、355-375），但没有规定把旧 `n_eff/scope` 置空还是以何谓 `scope_source` 过滤；当前 explode 仍无条件统计 `n_eff`（`pipeline/voc_analytics/explode.py:7-15`），018/026 仍写它们（`pipeline/sql/026_spu_layer_inherited.sql:109-149`）。 |
| I9 | 已解决 | 通过保留同名对象、列、索引和 GRANT，消费方可零改动（规格:231-253），并已把 queries、MCP 契约和测试列入清单（规格:355-375）；现有查询确实广泛依赖该对象（`system/app/queries.py:6-31`、`:222-344`）。 |
| I10 | 未解决 | “不迁移、只留映射；有人工行就停工”不是跨 SPU 旧卡状态/快照的一套确定迁移策略（规格:94-110、437-453）；人工表外键没有级联且旧快照按 `opp_id` 绑定（`pipeline/sql/001_schema.sql:178-197`、`:225-249`）。 |
| I11 | 未解决 | `assign_run_id` 只是关系列和文字约束，规格没有快照表、不可变写入、唯一键或 hash 校验；当前页面和派生层仍实时重读消息数组（`system/app/queries.py:539-552`、`:758-760`；`pipeline/sql/026_spu_layer_inherited.sql:109-120`）。 |
| I12 | 已解决 | 规格明确 `src_line` 只作 legacy 展示并强制质量/前端使用 `source_lines/evi_by_source`（规格:309-314）；生成结果已经同时写这三种来源字段（`pipeline/voc_analytics/pipeline.py:744-759`）。 |
| I13 | 未解决 | 规格提出 `voc_has_spu(message_id)`，但没有把 015/018/026 纳入文件清单或给出函数定义/迁移顺序（规格:255-269、337-375）；当前 Python 已并集而 SQL 契约仍只看 `m.spu`（`pipeline/voc_analytics/classification.py:17-30`；`pipeline/sql/015_classification_contract.sql:71-103`）。 |

## 二、仍然阻断施工的项

### B5 / B9：单个物化视图的 feature flag 没有实现语义

失败场景：v2 与 v3 需要不同的 MV 定义，但规格没有规定 MV 查询读取哪张 flag 表、何时刷新以及切换时如何保证读一致；若先把定义改成 `oe.assigned_spu`，回滚时同名对象没有 v2 数据可读；若保留 v2 定义，v3 会继续四象限展开（`pipeline/sql/026_spu_layer_inherited.sql:51-85`）。`run_generate.py` 当前也仍在收尾执行旧的跨源汇聚（`pipeline/scripts/run_generate.py:105-113`）。建议修法：保留 `voc_spu_issue_v2` 与 `voc_spu_issue_v3` 两个独立物化对象，用一个稳定兼容视图或应用配置选择读端，并对两套对象分别刷新、自检、原子切换；不要把未定义的 feature flag 当作已经存在的 MV 切换机制。

### B8：质量阈值不能独立验收

失败场景：同一组结果若没有预先标注的“同模式/不同模式”金集，任何 pair 都无法判定 false-merge 或漏合并，`0.05/0.10` 可以被任意解释（规格:403-417；`pipeline/voc_analytics/stages/stage1.py:343-395`）。建议修法：冻结抽样框、样本键、双人标注及冲突裁决规则；定义 pair 的分母、跨源子集和 27 个桶的汇总方式，生成机器可读 gold-set 与自动评分脚本后才允许探针放行。

### I10：旧卡裂分没有状态语义

失败场景：一张旧卡映射到三个 SPU，旧 `voc_opportunity_manual`/snapshot 只有一个 `opp_id`，按复制会产生三份 PM 决策，按丢弃又无法解释历史（`pipeline/sql/001_schema.sql:178-197`、`:225-249`）。建议修法：冻结 1→N 映射表的状态策略（人工确认、只保留历史、或按 SPU 重采样），为每个新卡写 `legacy_opp_id/legacy_spu/status_source`；迁移前对所有 snapshot、NN、proposal、lineage 和人工表做零悬空验收，不能用“有行就停工”代替策略。

### I11：run_id 没有冻结载体

失败场景：生成开始后 `voc_backfill_social_spu_inheritance()` 改写消息数组（`pipeline/sql/023_social_thread_columns.sql:38-97`）；路由读取旧数组，派生层读取新数组，即使关系行带 `assign_run_id` 也无法证明本轮使用的是同一集合。建议修法：新增只追加的 `voc_assignment_snapshot(run_id,message_id,seq,assigned_spu,assignment_source,input_hash)`，以 `(run_id,message_id,seq,assigned_spu)` 唯一约束并禁止更新；路由、生成、快照、MV 刷新全部只读该表，下一轮再生成新 run。

### I13：统一 has_spu 仍停在意图层

失败场景：只有 `spu_inherited` 的消息在 Python 被送入老品，但迁移 SQL 复用 015 的 `cardinality(m.spu)>0` 判成新品/R3，出现同一事实双生命周期（`pipeline/voc_analytics/classification.py:17-30`；`pipeline/sql/015_classification_contract.sql:71-103`）。建议修法：把函数定义、015/018/026/快照/验收的调用顺序和四种边界测试写进 SQL 清单；函数至少应对事实数组与继承数组做去空去重并由所有 SQL 复用。

## 三、订正引入的新缺陷

### 1. `voc_spu_issue` 改为 1:1 投影

结论：方向成立但切换设计不成立。`HAVING count(DISTINCT message_id) >= 2` 仍有意义：它是在每个 `(assigned_spu,opp_id)` 内要求两个独立消息，1:1 投影不会把“独立消息”变成冗余；`evi_count` 仍可表示关系行热度，`explode.refresh()` 的 `issue_count` 仍是 MV 行数（`pipeline/voc_analytics/explode.py:10-15`）。真正缺陷是同名 MV 无法被 feature flag 改定义，必须采用双对象/兼容视图方案；同时必须先物化 assigned-spu 快照再刷新。

### 2. 新品 `core_tag=NULL`

结论：身份语义比把 mode_name 重复写入两个字段更清楚，但不稳定性没有消失。`problem_mode` 仍来自 LLM 输出、mode_name 或 title 兜底（`pipeline/voc_analytics/pipeline.py:50-62`），身份哈希仍把它纳入且版本硬编码为 3（`pipeline/voc_analytics/pipeline.py:22-42`）。应把身份版本升为新的值或显式加入 `identity_schema`，否则 v3.0 与 v3.1 的同名身份不可区分。放弃墓碑抑制的理由与身份不稳是自洽的，但它是明确的能力撤销，不能再宣称有新品抑制。

### 3. 老品保留 `resolve_one`

结论：保留为 SPU 桶内兜底在语义上可行，但现有实现需要改造。SPU 重键后 L1 会召回该 SPU 下全部确定且未合并机会点，候选规模不能由静态代码给出；L2 只保留最多 10 个候选（`pipeline/voc_analytics/resolve.py:17-19`），候选面变大时召回可能下降。当前 `resolve_one` 没有新品分流（`pipeline/voc_analytics/resolve.py:76-107`），必须实现规格所说的 branch。§8.2 的跨源召回也没有拆成 Stage1 召回和 resolve 召回两个指标，不能据此定位失败环节。

### 4. 删除 `check_tombstone()`

结论：静态上不会打断生产调用，仓库搜索只见定义和文档/测试说明，M4 实际调用的是 `check_revive()`（`pipeline/tests/m4_lifecycle.py:50-93`）。但删除函数后必须同步清理 `pipeline/README.md:82`、`system/README.md:86`、`pipeline/tests/m4_lifecycle.py:9` 和 `pipeline/docs/pipeline_redesign.html:195`，否则规格自己的零命中发布门失败。`check_revive()`、`tombstone_baseline()` 不能一并删除（`pipeline/voc_analytics/lifecycle.py:95-135`）。

### 5. `assigned_spu` 三列与 run_id 冻结

结论：老品 `assigned_spu=core_tag` 的冗余是有价值的反四象限不变量，`assignment_source` 也提供了事实/继承可审计性，并非过度设计；新品全 NULL 则是生命周期语义所需，但应另存“无 SPU 的分类结果/输入 hash”以解释 NULL。问题在于当前关系表没有这些列或快照载体（`pipeline/sql/001_schema.sql:136-147`），仅添加 `assign_run_id` 不能冻结任何数据。必须用独立只追加快照表或等价不可变对象实现，不能把列名当作冻结保证。

## 四、量化主张核实

### §4.3 消费面统计

结论：`system/app/queries.py` 出现 73 次可由静态搜索复现，且查询确实从早期常量到问题/状态查询大量使用对象（`system/app/queries.py:6-31`、`:222-344`、`:733-792`、`:1137-1233`）。但表中 `voc_backup.sh` 标为 1 次，实际脚本在备份表清单和命令处至少出现两次（`pipeline/scripts/voc_backup.sh:17-31`）；“19 个消费文件”也没有定义是否排除迁移、文档、测试和备份，当前仓库共有 29 个命中文件。因此判定：**该消费面统计表不成立；其中 `queries.py=73` 是可复现的子项，19 文件总数无法核实**。

### §7.3“每张 v3 快照 base_total/neg_total 都是 0”

结论：现行 SQL 的确按 `e.tag IS NOT DISTINCT FROM o.core_tag`，而 v3 规格把老品 `core_tag` 改为 SPU（`pipeline/scripts/run_generate.py:133-145`；`pipeline/sql/001_schema.sql:93-94`），所以这是明确的全量失真风险；但没有允许连接数据库，不能证明生产中每一张实际快照都为 0，也不能排除某些 `e.tag` 恰好等于 SPU。判定：**无法核实**，应把“每张”改成基于冻结数据集的可验收断言。

### §5 四条守恒等式，特别是 `P>=F`

结论：`P>=F` 与 `count(DISTINCT(message_id,seq,assigned_spu))=F` 在逻辑上不矛盾，因为一个路由实例可以挂到同一 SPU 的多张问题卡；现有关系主键也允许同一事实挂多个 `opp_id`（`pipeline/sql/001_schema.sql:136-147`）。但两式只比较计数，缺少“期望三元组集合 = 实际三元组集合”的集合等式，漏掉一个实例再重复另一个实例仍可能通过。判定：**成立（互不矛盾），但不足以作为完整守恒验收**。

### §8 各阈值是否可测

结论：8.1 的 R/F 和跨 SPU 不变量在实现快照后可测；8.4 的重复运行 ARI 也可计算。但 8.2 的 200 个 pair 和 8.3 的 false-merge≤0.05/漏合并≤0.10 没有金集、标注协议、pair 分母、跨源子集定义，无法独立计算；8.5 的 v2 单证据基线虽要求先测，却没有固定抽样快照。判定：**无法核实**，当前阈值不能作为自动放行脚本输入（规格:403-433）。

### §2.3“预聚类已按 claim 向量去重，因此并卡是期望行为”

结论：**不成立**。预聚类按 claim 做 k-NN 连通分量，并按消息选择代表 claim（`pipeline/voc_analytics/stages/precluster.py:67-92`、`:263-325`），它不是语义金集，也不能保证不同 claim 不落同一连通分量或同一 `problem_mode`；Stage1 仍会在每个 cluster 内继续分组（`pipeline/voc_analytics/pipeline.py:213-218`、`pipeline/voc_analytics/stages/stage1.py:164-212`）。因此 NULL core_tag 下的并卡只能由探针实测，不能事先宣称是期望行为。

## 五、放行结论

**仍需修改规格。** v3.1 已实质修复身份二选一、派生对象退役风险、扇出口径缺失和回滚空白的主要方向，但 B5/B9 的 feature flag 方案不可实现，B8/I10/I11/I13 仍没有可独立验收的施工契约；此外新品身份版本和金集口径也未闭环（规格:132-147、231-253、394-467；`pipeline/sql/026_spu_layer_inherited.sql:51-85`；`pipeline/sql/001_schema.sql:136-197`）。在物化视图切换机制、不可变归属快照、历史状态策略及质量金集/自动评分脚本写死并通过探针前，不应进入施工阶段。

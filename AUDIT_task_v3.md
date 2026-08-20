# v3 施工任务书可施工性审查

## 一、阻断施工的问题

1. **归属快照没有施工入口，也没有唯一写入者。** 任务书只要求 031 建表、加不可变规则和给关系表加三列（`pipeline/docs/施工任务书_v3.md:52-58`），没有规定生成开始时如何执行规格要求的整轮 `INSERT ... SELECT`、如何把 `run_id` 传入每条关系、如何记录/校验内容指纹，以及路由、派生层、验收如何统一改为只读快照。规格要求这些机制（`pipeline/docs/架构规格_v3.md:282-294`）。现有生成池仍直接取 `m.spu/m.spu_inherited`（`pipeline/voc_analytics/db.py:318-380`），`recount()` 也直接读消息数组（`pipeline/voc_analytics/pipeline.py:831-854`），两个并行进程还共用同一个 `RUN_ID`（`pipeline/scripts/rerun_both.sh:53,311-317`）。按任务书施工会出现两个进程竞争写同一快照、只写到半轮、或路由与收尾读到不同归属；这是中途失败而非质量偏差。

   **建议修法：** 在并行生成前增加单一的快照准备步骤（或数据库 advisory lock + 幂等 owner），明确快照 run_id 与 `voc_run_log` 的对应关系；把 `assigned_spu/assignment_source/assign_run_id` 从快照一路传到关系写入、合并、派生和验收；落 run log 的 count/distinct/md5 指纹，并将所有 v3 路径中的消息数组读取替换为快照 JOIN。

2. **新归属列在现有落库和提案合并路径中不会被写入。** `save_opportunity()` 插入 `voc_opp_evidence` 时只写旧六列（`pipeline/voc_analytics/pipeline.py:982-988`），`execute._merge()` 复制证据时也只选旧列（`pipeline/voc_analytics/execute.py:53-59`）。因此即使 routing 在内存行上写了字段，v3 MV 读取 `oe.assigned_spu` 仍会得到 NULL；MERGE 后的证据还会丢失归属来源。`voc_spu_issue_v3` 的数据源契约因此无法成立（规格 `pipeline/docs/架构规格_v3.md:313-315`）。

   **建议修法：** 明确并实现关系行的四列写入/复制/冲突处理；MERGE、attach、recount、快照投影和自检全部以归属快照为准，并为历史 v2 行规定 NULL 语义。

3. **030 要求“CREATE OR REPLACE 015 中的 has_spu 表达式”在技术上没有可替换对象。** 015 的 `has_spu` 是迁移执行期间 CTE 的一次性表达式（`pipeline/sql/015_classification_contract.sql:73-112`），不是函数、视图或持久化规则；`CREATE OR REPLACE` 无法改写已经执行过的 CTE。只建 `voc_has_spu()` 会留下旧机会点分类和旧自检结果。任务书的表述见 `pipeline/docs/施工任务书_v3.md:45-50`，规格的统一口径见 `pipeline/docs/架构规格_v3.md:503-531`。

   **建议修法：** 030 明确写出可重跑的当前库回填/重分类 SQL，并按需 `CREATE OR REPLACE` 持久函数（如 `voc_guard_locked`）；历史 015 文件保持不变，但不能把一次性 CTE 描述为可替换对象。

4. **032 把 `voc_spu_issue` 改成普通视图后，现有刷新函数会在收尾直接失败。** 026/018 定义的 `voc_refresh_spu_layer()` 仍执行 `REFRESH MATERIALIZED VIEW public.voc_spu_issue`（`pipeline/sql/026_spu_layer_inherited.sql:95-103`；同类定义在 `pipeline/sql/018_n_eff_source_alignment.sql:24-32`），而 `explode.refresh()` 无条件调用该函数（`pipeline/voc_analytics/explode.py:7-15`）。任务书只说同步返回结构（`pipeline/docs/施工任务书_v3.md:76`），没有规定函数改为刷新 `_v2/_v3` 两个 MV、再按兼容视图当前指向读取。普通 view 不能被 `REFRESH MATERIALIZED VIEW`，所以 finalize 会报错。

   **建议修法：** 在 032/独立 v3 函数中明确两个 MV 的刷新和自检顺序，兼容视图只负责读；`explode.py`、备份脚本和所有 `REFRESH MATERIALIZED VIEW voc_spu_issue` 调用改指具体版本。

5. **战略空状态无法按任务书落地。** 规格要求新增 `voc_opportunity.scope_source` 并由查询和页面按它过滤（`pipeline/docs/架构规格_v3.md:227-233`）。现有 schema 没有该列（`pipeline/sql/001_schema.sql:85-126`），战略查询既不选择也不过滤它（`system/app/queries.py:1046-1060`），路由只能对已有 `scope` 做过滤（`system/app/routes/strategy.py:16-32`）。任务书只列页面/路由动作，没有查询和迁移动作（`pipeline/docs/施工任务书_v3.md:77-81`）。实施者无法在不补写 schema/query 的情况下显示明确的 v3 空状态，或者会继续显示旧 `scope`。

   **建议修法：** 031（或单独迁移）增加 `scope_source`，`build_opportunity` 对 v3 行固定写 `'v3-未计算'`；同步 `STRATEGY_OPPORTUNITIES`、路由和模板，并测试 v3 行不会进入旧 scope 结果集。`n_eff/scope` 的刷新函数还必须停止为 v3 行写值（规格 `pipeline/docs/架构规格_v3.md:347-353`）。

6. **shadow/回滚所需的 ID 映射和旧引用盘点没有交付物。** 规格要求建立只追加的 `voc_opp_id_map`，并盘点 snapshot、NN、proposal、lineage、人工表和日志中的旧 ID（`pipeline/docs/架构规格_v3.md:601-610`）；现有清库路径会删除机会点、快照、proposal、lineage（`pipeline/scripts/rerun_both.sh:287-299`）。任务书只要求 rerun 增加“不清库”模式，三个迁移均未要求 `voc_opp_id_map` 或映射报告（`pipeline/docs/施工任务书_v3.md:43-64,77-78`）。没有这一步，旧引用无法验证、灰度期无法判定悬空，回滚也无法按规格完成。

   **建议修法：** 增加映射表、施工前导出/映射/悬空计数和 47 条 proposal 的处置步骤；明确 OPP2 的 `prompt_ver/identity_version` 过滤边界，并将清库开关与灰度状态绑定。

7. **前端消费方仍按消息数组重新展开 SPU，必然重现 v3 四象限。** `BOARD_ISSUES`、`SPU_ISSUES` 和原声查询都用 `msg.spu`/`msg.spu_inherited` 判断归属（`system/app/queries.py:635-663,823-857,960-977`）。这绕过 `oe.assigned_spu`，会让 A 产品页再次显示 B 的机会点，即使 `_v3` MV 本身已经正确。任务书只要求撤掉“有效产品数”读数，没有要求修改这些查询（`pipeline/docs/施工任务书_v3.md:80-81`），与规格的归属契约（`pipeline/docs/架构规格_v3.md:239-315`）不一致。

   **建议修法：** 所有产品页/原声/质量 SQL 按 `(spu, opp_id)` 与 `oe.assigned_spu` 连接；不要在 v3 路径再从消息数组推导 SPU，并把 `queries.py` 列入代码交付和回归测试。

## 二、会导致返工的歧义

1. **SPU 是否规范化不唯一。** 现有 `_old_product_topic()` 会 NFKC、折叠空白并 `casefold()`（`pipeline/voc_analytics/routing.py:44-60`）。若直接把 SPU 接入，它会把 `MA39` 变成 `ma39`；另一种实现保留数据库原值。前者可能使 `core_tag` 与快照的 `assigned_spu` 不相等，触发规格自检（`pipeline/docs/架构规格_v3.md:254-261,373-380`）。建议写死唯一的 SPU canonicalizer，并由快照、桶键、core_tag、视图共同使用。

2. **“同一 run_id 二次写入被拒绝”的范围不明确。** 可以理解为只拒绝完全相同主键的重复 INSERT，也可以理解为该 run_id 已有任意行后拒绝整个第二批 INSERT；前者允许归属变更悄悄追加，后者会阻断分块写入/重试。任务书 `pipeline/docs/施工任务书_v3.md:56,91` 没有定义。建议明确“单条一次性整轮 INSERT + 任何第二次写入均拒绝”，并提供仅供整轮清理的受控函数；否则用内容指纹检测变更并将整轮置失败。

3. **UPDATE/DELETE 保护的实现边界不明确。** 规格说“只允许整轮删除”（`pipeline/docs/架构规格_v3.md:282-284`），任务书只说加规则或触发器（`pipeline/docs/施工任务书_v3.md:56`）。行级触发器会把合法的整轮删除也拒绝；放得太宽又能删半轮。建议指定 statement-level 保护或 `voc_delete_assign_snapshot(run_id)` 唯一清理入口，并测试部分删除必失败、整轮清理可审计。

4. **“同样处置 018/026 中只展开 m.spu”有两种实现。** 一种是继续实时展开 `spu ∪ spu_inherited`，另一种是改为 `oe.assigned_spu`/快照；两者会得到不同的派生层和 `n_eff`。规格明确要求 v3 派生读归属列、018/026 的旧定义不能半截覆盖（`pipeline/docs/架构规格_v3.md:313-315,347-354`），任务书 `pipeline/docs/施工任务书_v3.md:49` 没写替换边界。建议明确旧 MV 只作为 `_v2` 原样保存，v3 函数和 `_v3` 只读归属快照。

5. **“不清库模式”的启用语义不唯一。** 可读成默认永远 shadow、另有显式 `--truncate`；也可读成保留现有清库命令但增加一个默认关闭的环境变量。当前脚本的清库动作在启动阶段固定执行（`pipeline/scripts/rerun_both.sh:287-299`），任务书只写一句模式要求（`pipeline/docs/施工任务书_v3.md:78`）。建议规定互斥的 `--mode shadow|truncate`、默认 `shadow`，并在灰度标志存在时硬拒绝 `truncate`。

6. **静态 `rg` 验收范围会把任务书/规格自身算进去。** 按字面执行 `rg cross_source_merge` 或 `rg check_tombstone`，任务书和规格正文仍会命中（`pipeline/docs/施工任务书_v3.md:71,74`；`pipeline/docs/架构规格_v3.md:478-481,500`），因此“零命中”永远不成立。建议限定 `pipeline/voc_analytics pipeline/scripts system/app system/app/templates` 等生产代码路径，并明确是否保留历史测试/迁移注释。

7. **若只看验收数字，多个点可被绕过。** `l1_empty=0` 可以通过不再递增该指标而非真正绕过 L1；`base_total >= neg_total 且非零` 可以查询旧 v2 行而非本轮 v3 行；`R=F` 可以用人工构造的给定证据集而不验证冻结快照。任务书的验收点分别见 `pipeline/docs/施工任务书_v3.md:72,77,87-92`，而当前实现的指标/快照查询是全局的（`pipeline/voc_analytics/resolve.py:76-81`；`pipeline/scripts/run_generate.py:133-149`）。建议增加行为测试：新品调用 `resolve_one` 时断言 `l1_candidates` 未被调用；所有守恒/分母断言必须带本轮 `run_id`/OPP2 过滤并从实际快照计算。

## 三、缺失与冲突（任务书 vs 规格）

1. **直接冲突：快照 `source` 枚举。** 任务书要求 `CHECK (source IN ('fact','root'))`，并把 029 的来源改名为 `root`（`pipeline/docs/施工任务书_v3.md:52-55`）；规格冻结的是 `('fact','inherited')`，且 `assignment_source` 同样使用 `inherited`（`pipeline/docs/架构规格_v3.md:250-256,270-279`）。029 只收敛“从帖子/视频采集”的继承范围，并没有改变归属来源枚举（`pipeline/sql/029_inherit_from_root_only.sql:49-62`）。应以规格为准，保留 `inherited`；若业主确实要 `root`，必须先修规格并同时修所有函数、测试和报表。

2. **规格要求但任务书没有明确迁移/动作：** `scope_source`、只追加 `voc_opp_id_map`、`ix_opp_bucket` 重建、停止 v3 的 `n_eff/scope` 写入、`denominator_scope` 的 schema 变更，以及 shadow 所需的 `prompt_ver/identity_version` 选择口径（规格 `pipeline/docs/架构规格_v3.md:343-357,614-627`；当前 schema 没有这些列，`pipeline/sql/001_schema.sql:85-126,178-197`）。任务书只在 run_generate 行中提到 denominator_scope 写进 031，却没有在 031 的文件交付条目中写出 ALTER/约束（`pipeline/docs/施工任务书_v3.md:52-58,77`）。

3. **规格要求但任务书漏列的代码/DDL消费者：** `execute.py` 的 opp_id/lineage 重键与归属复制（规格 `pipeline/docs/架构规格_v3.md:487-492`，任务书仅在必读清单提到它，`pipeline/docs/施工任务书_v3.md:33`）；`scripts/voc_backup.sh` 对普通兼容视图改备份具体 MV（规格 `pipeline/docs/架构规格_v3.md:320-338,487-489`）；`system/app/queries.py` 的 `n_eff/scope` 引用及归属查询、`system/mcp_app/docs/field_contracts.md`（规格 `pipeline/docs/架构规格_v3.md:496-498`）；`stages/stage1.py` 的热门 SPU 粗切路径（规格 `pipeline/docs/架构规格_v3.md:480-483`）。这些不补，交付物不能覆盖完整消费面。

4. **任务书新增但规格没有同等硬约束的要求：** “同一 run_id 二次写入被拒绝”（`pipeline/docs/施工任务书_v3.md:91`）比规格明确写出的不可 UPDATE/DELETE 与指纹校验（`pipeline/docs/架构规格_v3.md:282-287`）更强；“快照 base_total 非零”（`pipeline/docs/施工任务书_v3.md:77`）也未在规格的守恒式中作为普适条件。请明确这是业主新增门槛，还是应改写为“本轮 v3 有效机会点的分母与冻结 SPU 池一致”。

5. **任务书把 030 描述成修改历史迁移，但没有给出可执行的替代对象。** 015 的一次性 CTE 事实见 `pipeline/sql/015_classification_contract.sql:73-112`；规格本身已要求先建函数、再做当前库回填和测试（`pipeline/docs/架构规格_v3.md:523-531`）。任务书应把“不能修改历史文件”和“必须重算当前库”拆成两个明确交付动作，而不是写成 `CREATE OR REPLACE`。

## 四、对已落地改动的兼容性（§13.4 四项逐项结论）

1. **029 继承收敛：条件兼容，当前任务书会破坏来源语义。** 029 的实际改动只让继承从帖子/视频产生（`pipeline/sql/029_inherit_from_root_only.sql:49-62`），与 v3 的归属快照可以叠加；但任务书把快照枚举改成 `root`，会把“继承来源”与“原帖采集范围”混成一个字段，破坏规格的 `assignment_source='inherited'` 契约。结论：保留 029，不改其函数；先修枚举和快照单一读口后才能放行。

2. **G5 诉求降级：逻辑仍成立，但依赖快照保留事实/继承区分。** 当前降级只在无事实 `spu`、仅有 `spu_inherited` 时清空继承值（`pipeline/voc_analytics/routing.py:88-114`），029 不改变这个判断。v3 若先做 SPU 扇出再丢失来源，或把 `root` 当作事实，诉求会错误进入老品；任务书没有规定降级发生在快照展开前，也没有规定来源投影。因此结论是“可兼容但未闭合”，必须用快照的 `source=fact|inherited` 在 G5 前保留原始事实/继承口径，并回归覆盖仅继承诉求、仅事实诉求和两者冲突。

3. **落库并行化：老品可保持安全，新品必须先分流。** 现有并行安全性依赖“桶键就是 core_tag，L1 只在同一 core_tag 召回”（`pipeline/voc_analytics/pipeline.py:366-380`；`pipeline/voc_analytics/resolve.py:23-35`）。v3 老品把两者都换成同一 canonical SPU 后仍成立；新品全部 `core_tag=NULL` 时，只有 `resolve_one` 在进入 L1 前按 `opp_type` 直接 create 才安全。若漏掉该分流，所有新品会落入 NULL 候选区，跨桶并发的去重语义和 `l1_empty` 统计都会改变。任务书虽写了分流（`pipeline/docs/施工任务书_v3.md:72-73`），但最低测试没有明确断言 worker 不调用新品 L1，需补行为测试。

4. **服务商质量报告：不被 v3 破坏，且不应被 v3 代替。** 报告只读原帖/视频 SPU 覆盖、评论组缺口和 parent_id 可解析性（`pipeline/scripts/vendor_quality_report.py:33-80`），与 v3 归属快照正交；任务书“不要从正文提取 SPU”的边界与报告说明一致（`pipeline/docs/施工任务书_v3.md:120-122`；`pipeline/scripts/vendor_quality_report.py:15-17`）。结论：无需改报告，v3 只能消费其上游字段并记录缺陷，不得在 routing 或快照中增加正文型号提取作为隐式补救。

## 五、放行结论

**仍需修改。** 任务书当前不能交付唯一确定的施工结果：快照没有一次性写入和单一读口，归属列没有贯穿落库/MERGE，032 会让现有收尾刷新直接失败，战略空状态缺 schema/query 支撑，且 shadow 回滚缺少 ID 映射；另有 `source` 枚举与规格直接冲突。上述问题中至少前 1-6 项会在施工或首次收尾时中途失败，不能靠验收阶段补一句 SQL 解决。补齐迁移对象、快照生命周期、所有消费者和行为验收，并按规格修正 `inherited` 枚举后，才具备放行条件。

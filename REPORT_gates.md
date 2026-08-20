# 社媒信号管道重构交付报告

日期：2026-08-18

## 结论

社媒证据入池已收敛为 G1 品牌白名单→G2 标签门→G3 官号门→G4 统一价值门→G5 SPU 2×2 路由。电商入口保持原契约；`channel` 已从应用数据契约与生成身份中删除。迁移仅写入文件，未执行。

离线验证结果：`197 passed, 1 xfailed, 19 subtests passed`；`python3 -m compileall -q voc_analytics scripts tests` 通过。`test_grounding.py` 的 C5 strict xfail 保留。

## 五道门落点

### G1 / G1b：品牌白名单

- `pipeline/voc_analytics/config.py:75-89`：新增全库标定的 `OWN_BRANDS = ("VIJIM", "宙比", "小隼")`；同时把 `宙比/小隼` 补入 `BRAND_OWN_ALIASES`，修复自家品牌被主体检查当成第三方的 live bug。
- `pipeline/voc_analytics/db.py:185-214`：纯函数等价实现 G1/G1b/G2/G3，用于无库单测。
- `pipeline/voc_analytics/db.py:217-255`：SQL 共享条件构造器；G1 不维护竞品词表，任一非本品 brands 值即拒绝；G1b 检查同组帖子/视频。
- `pipeline/voc_analytics/db.py:301-356`：生成池社媒分支调用同一组条件。

### G2：标签门

- `pipeline/voc_analytics/config.py:81-82`：固定保留标签与一票否决标签。
- `pipeline/voc_analytics/db.py:238-241`：先排除 drop tags，再强制至少命中一个 keep tag。
- `pipeline/voc_analytics/db.py:258-298`：对账 SQL 按 G1→G2→G3 顺序生成互斥计数，与生成池共用条件文本。

### G3：官号门

- `pipeline/voc_analytics/config.py:83-87`：`OFFICIAL_AUTHOR_PATTERN` 由 `BRAND_OWN_ALIASES` 转义派生，不维护第二份口径。
- `pipeline/voc_analytics/db.py:242-245`：作者名为 NULL 或不匹配官方别名才通过；同处保证 snippet/content 至少有一个可判文本。

### G4：统一价值门

- `pipeline/voc_analytics/prompts.py:8-9,68-112`：提示词进入统一版本体系；任务书的判定语义、8 条硬规则、全部正反例和 claim 契约保留。
- `pipeline/voc_analytics/stages/value_gate.py:40-71`：逐消息选文本与父帖标题；仅评论/回复读同组帖子/视频的 `message_title`，不读父帖正文。
- `pipeline/voc_analytics/stages/value_gate.py:74-83`：空 claim，或命中 `某款|某个|待发布` 且剩余无具体名词，收敛为「诉求过泛」。
- `pipeline/voc_analytics/stages/value_gate.py:105-125,148-280`：按 `message_id` 去重判定并展开回全部证据行；当首票低于 0.8 或首票为「产品缺陷」时补两票取多数；使用 `llm.parallel_map`；Fatal 失败熔断，单消息非致命失败按行记账。
- `pipeline/voc_analytics/db.py:125-145`：只读当前 `prompt_ver` 的缓存，新判定按 `message_id` upsert。
- `pipeline/voc_analytics/pipeline.py:139-179`：旧 `intent_gate` 已被替换，G4 为社媒结构门之后的唯一价值判定门。

### G5：SPU 归属 2×2

- `pipeline/voc_analytics/classification.py:17-30`：`_has_spu` 扩为 `spu ∪ spu_inherited`，两列保持相同的数组类型检查。
- `pipeline/voc_analytics/routing.py:88-107`：实现诉求/缺陷 × 有/无 SPU 四路；无 SPU 缺陷落「缺陷不可归属」终态。
- `pipeline/voc_analytics/routing.py:17-22,110-149`：`LifecycleBucket` 只余 `(opp_type, topic)`；老品以 `e.tag` 进桶，新品以预聚类 id 进桶，无 channel 分桶。
- `pipeline/voc_analytics/pipeline.py:181-226`：只有新品创新行进既有预聚类接口；G4 若泄漏空 claim，作为闭合异常报错，不另造隐形终态。`stages/precluster.py` 未修改。

## 迁移与入库

- `pipeline/sql/023_social_thread_columns.sql:5-30`：六列全部使用 `ADD COLUMN IF NOT EXISTS`，新建符合约束的 `voc_social_gate`。
- `pipeline/sql/023_social_thread_columns.sql:35-99`：幂等 SPU 继承函数。当同组事实 SPU 去重并集恰为 1 时，只写空事实行的 `spu_inherited`；无值/冲突/后续补到事实 SPU 时清除过期推断，永不回写 `spu`。
- `pipeline/sql/023_social_thread_columns.sql:101-106`：writer 可执行回填、读写机器缓存；human/app 读角色与 reader 只读。
- `pipeline/sql/024_drop_channel.sql:1-72`：写明必须先发布代码且与全量重跑同批执行；先按依赖顺序拆视图，再删除 channel 列，最后按最新契约重建 board/inbox/safety 视图。
- `pipeline/sql/025_spu_scope.sql:7-121`：`voc_spu` 容器纳入社媒 `spu ∪ spu_inherited`；电商聚合仍只读 `src_line='电商'` 的事实 `spu`；仅社媒 SPU 的电商属性为 NULL，`has_ec=false`。`voc_spu_issue` 未重定义。
- `pipeline/voc_analytics/clean.py:250-298`：社媒五个帖子线索列按任务书映射，电商显式为 NULL。
- `pipeline/voc_analytics/ingest.py:90-99`：每个入库窗口在 message/evidence 写完后执行一次幂等继承回填并记录变更数。

## channel 删除与收尾

- `pipeline/voc_analytics/pipeline.py:22-42`：新老机会的身份域统一为 `(opp_type, core_tag, problem_mode)`，开启 v3 身份版本。
- `pipeline/voc_analytics/resolve.py:20-36`：L1 候选只用 `core_tag + opp_type`；交叉来源挂载同时读 `spu_inherited`（`resolve.py:149-165`）。
- `pipeline/voc_analytics/pipeline.py:818-864`：挂载后权威重算使用事实/继承 SPU 联合口径。
- `pipeline/voc_analytics/pipeline.py:398-635`：社媒结构门恒等式、G4/G5 终态恒等式、消息缓存/LLM 恒等式和预聚类/Stage1 恒等式同时闭合；任一静默缺口都使整轮失败。
- `pipeline/voc_analytics/pipeline.py:663-684`：收尾调用 `SELECT voc_refresh_opp_nn()`，记录刷新行数和双端悬空数，悬空非 0 报错。
- `pipeline/scripts/run_generate.py:65-150`：手工收尾路径在挂载、快照、SPU 刷新后刷新 NN，校验通过后才改变 PM 可见性。
- `pipeline/voc_analytics/definitions.py:318-349`：Dagster 新增 `opportunity_neighbors` 资产，`release_to_pm` 显式依赖它。

## 主动补强及原因

- 生成池与结构门账本共用 SQL 条件构造器，防止两份 G1–G3 条件日后漂移。
- SPU 回填不只写入，也在组内冲突、组 id 缺失或事实 SPU 后到时清除旧推断，保证重跑真正幂等。
- 预聚类若再看到空 claim 立即报错；「诉求过泛」只能在 G4 统一收敛，不让预聚类产生第二个隐形丢失口。
- `pipeline/scripts/smoke.py:198-246` 的诊断路径补齐 G4/G5 与预聚类；`pipeline/tests/m35_stability.py:28-36` 则明确限定电商，避免诊断脚本绕过新契约。
- 手工 finalize 的 PM 放行移到 NN 悬空校验之后，与 Dagster 资产顺序一致，避免「收尾失败但已放行」。

## 测试改写说明

- `test_generation_pool_contract.py`：旧社媒四分支口径被删除，改为 G1/G1b/G2/G3 纯函数与共享 SQL 契约测试；仍锁定电商入口不变。
- `test_intent_gate_recall.py`：旧 intent gate 已不存在，整体改为 G4 流程测试；保留德语/越南语必须送达 LLM 的多语种召回断言，并覆盖全部任务书正反例、投票、缓存、空泛 claim、父帖标题与单消息失败。
- `test_lifecycle_routing.py`：删除 channel 分桶断言，改为 G5 2×2 四路和跨来源同标签融合。
- `test_opportunity_identity.py` / `test_resolve_lifecycle_keys.py`：身份与 L1 候选不再接受 channel，改验统一三元身份与生命周期隔离。
- `test_generation_reconciliation.py` / `test_precluster_reconciliation.py`：改验新的社媒终态恒等式、缓存消息恒等式和「空 claim 不得成为预聚类终态」。
- `test_grounding.py`：由于修复本品别名，将宙比/小隼改为本品断言；C5 xfail 原样保留。
- `test_clean_fields.py` / `test_classification.py`：增加五列映射、电商 NULL、入库回填顺序和 `spu_inherited` 类型/路由测试。
- `test_finalize_refreshes_spu_layer.py`：增加 NN 刷新记账、悬空报错及“校验后才放行”的契约。
- `test_gate_migrations.py`：新增三份迁移的离线静态契约。

## 风险与部署注意

1. 本任务严格未连库、未执行 SQL、未调用 LLM/embedding，因此 PostgreSQL 的锁时间、实数据计数与性能需在正式发布流程中另做只读/执行验收。
2. `024` 会重建依赖视图并删列，必须遵守文件头的顺序：先上配套代码，再与全量重跑同批迁移。
3. `voc_social_gate` 的主键是 `message_id`，符合指定 DDL；因此新 prompt 结果会覆盖旧版本，而非保留多版历史。如回滚 prompt，该消息会因版本不匹配再判。
4. 缓存按 `message_id + prompt_ver` 命中语义使同版本增量不重判；若上游会就地改写同一 `message_id` 的正文，需通过提升 prompt 版本或清理该缓存才会重判。
5. 「诉求过泛」中的“无具体名词”为保守文本启发式，已有空 claim/指定占位词用例，但仍建议用 `generic_claim_rows` 与原声抽样监控误拦/漏拦。
6. 同组如异常出现多个帖子/视频根行，父帖标题按 `message_id` 稳定选第一个；本实现不会把多个父帖正文或标题混入 prompt，但该异常应在上游质量监控中暴露。
7. G3 按要求使用作者名子串正则；若普通用户昵称恰好包含短别名，可能产生少量误拦，应通过 `g3_official_rows` 与原声区抽样观察，不在本次擅自改口径。

## 硬性边界确认

- 未使用 SSH，未连库，未执行迁移，未调用任何模型/向量接口，未部署或重跑。
- 未执行 git 操作。
- 修改范围只在 `pipeline/**` 与本报告。
- `pipeline/voc_analytics/stages/precluster.py` 和 `pipeline/voc_analytics/llm.py` 未修改。
- `GROUNDING_ENFORCE` 仍默认 False；`MIN_EVIDENCE`、`GENERATION_MAX_FAILED_RATIO`、`PRECLUSTER_*` 默认值未改。

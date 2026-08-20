# VOC 管道五道门重构与上线计划：对抗性评审

## 结论

**当前应暂停在步骤 3；不得应用 025、026、024，也不得清空机会层重跑。** 这不是因为五道门的主干思路不可用，而是当前实现存在会在本次上线中直接触发的跨层断裂：重灌不是权威替换、G4 缓存不随输入失效、双生命周期会并发判同一消息、`MIN_EVIDENCE=2` 没有程序闸、026 把同一线程/甚至同一消息的多行当独立证据、产品页仍有六处只认事实 SPU、清库脚本既可能被人工表外键阻断也可能留下半代数据。

本评审为纯静态只读审计。按任务边界未连接生产库、未执行 SQL、未调用模型、未运行测试、未使用 Git；因此凡是需要生产数据才能决定是否已触发的事项，均明确给出上线前必须执行的只读断言，而不假称已经验证了生产状态。

建议的发布原则是：先修事实层与判定快照，完成一份可审计的影子代，验证完整性和旧 ID 映射，原子切换可见代，最后才删除 `channel`。不能把“同一个运维批次”当成数据库原子性。

## 阻断级（上 024 / 全量重跑前必须解决）

### B01. 当前“全量重灌”不是权威快照替换，会保留幽灵证据和已从上游消失的消息

- **文件:行号**：`pipeline/voc_analytics/ingest.py:60-94`；`pipeline/voc_analytics/db.py:88-117`；`pipeline/voc_analytics/config.py:88-99`；`pipeline/voc_analytics/yunting.py:130-162`。
- **具体失败场景**：消息 M 上次导出两个标签，生成 `seq=0/1`；云听本次修订为一个标签或空标签。`save_evidence()` 只按 `(message_id,seq)` upsert，不删除本次已不存在的 seq，所以旧 `seq=1` 永久留在生成池；若数组重排，已有 `(message_id,seq)` 关系还会静默换语义。另一个确定场景是 M 的品牌由本品修正为纯竞品后不再满足 `SOCIAL_FILTER`，本轮根本不返回 M；纯 upsert 会继续保留旧本品行并让它通过 G1。backfill 模式某片 `matched=0` 还只记 warning，`run_ingest` 可把该周记成 success。
- **为什么现有测试没抓到**：`pipeline/tests/test_clean_fields.py:194-202` 只静态检查三次调用的先后；没有 2→1、1→0、数组重排或“本轮 manifest 缺失旧 ID”的二次入库测试。现有脚本也不核对每周/来源的 matched、exported、解析行、唯一 ID 与库内触达集合。
- **建议修法**：所有切片先落 staging；整窗完整成功后，按本轮 message manifest 在同一事务做消息/证据集合替换，包括显式删除 stale seq 和处理空证据集；对未触达的旧消息做 tombstone/quarantine，而不是默认保留。步骤 3 完成门必须包含每周×来源 manifest、零片显式豁免、总量与题设 26,407/约 25k 的差异解释。修复后需要重做本次重灌，不能把当前 upsert 结果直接晋级。

### B02. `spu` 与 `spu_inherited` 互斥只靠一次函数调用，不是所有代码路径都成立的不变式

- **文件:行号**：`pipeline/voc_analytics/ingest.py:90-94`；`pipeline/voc_analytics/db.py:52-74,88-122`；`pipeline/voc_analytics/clean.py:289-297`；`pipeline/sql/023_social_thread_columns.sql:65-95`；`pipeline/sql/026_spu_layer_inherited.sql:18-20,39-45,87-91`。
- **具体失败场景**：旧评论为 `spu=[]、spu_inherited=[A]`；本次重灌把事实 SPU 改成 `[B]`。`save_messages` 已独立提交，随后证据保存、进程或 backfill 失败，数据库长期留下事实 B + 陈旧继承 A。026 的 `DISTINCT` 只能消掉 A+A，不能消掉 A+B，因此同一证据被归到两个产品、`n_eff` 也被抬高。并发回填也没有组锁：一个事务按旧的单一 A 计算时，另一个事务可加入 B 并完成冲突清理，前者随后仍把 A 写回。
- **为什么现有测试没抓到**：`test_clean_fields.py:194-202` 只证明调用顺序，不证明事务原子性；`test_gate_migrations.py:11-27` 只搜函数和字符串，没有失败注入、并发、事实改写或不变式断言。
- **建议修法**：消息权威替换、证据替换和受影响线程回填放进同一事务；事实 SPU upsert 同语句清掉继承；按 canonical thread key 加事务级锁。增加 DB 约束/触发器：非社媒不得有继承，同一行有效事实与继承不得同时非空。025/026 的读取也应采用“有效事实优先，否则 inherited”，不能无条件拼接。上线门先做 `fact_nonempty AND inherited_nonempty = 0`、非社媒 inherited=0、冲突组 inherited=0 的只读断言。

### B03. G1 白名单与 G4 v2 的核心第三方配件例外互相不可达，反向又对未知品牌真空放行

- **文件:行号**：`pipeline/voc_analytics/db.py:168-214,225-237`；`pipeline/voc_analytics/prompts.py:69,82-88,111-113`；`pipeline/tests/test_generation_pool_contract.py:21-46`。
- **具体失败场景**：`brands=['VIJIM','大疆']`，正文“优篮子能不能给 DJI Action 5 做个散热器？”——G1 因任一非本品元素直接拒绝，永远到不了 v2 明文规定的第三方设备配件诉求。反向输入 `brands=[]`、根缺失、正文“Pocket 4 的配件什么时候出？”会因 `NOT EXISTS` 真空通过；G4 提示又预设“这条提及本品牌”，无法证明请求的供给方是 Ulanzi，而不是 SmallRig 或泛问。
- **为什么现有测试没抓到**：`test_generation_pool_contract.py:31-46` 分别把“混牌拒绝、空数组通过”锁成结构门契约；G4 测试用 fake responder 直接回预设结果，没有把结构门与 v2 第三方例外串起来。
- **建议修法**：云听 `brands` 不能同时承担“内容出现过的品牌”和“诉求供给方/主体”。至少拆成三态：本品主体、第三方适配对象、未知；本品+第三方的明确“请本品牌提供配件”必须进入 G4，纯第三方或未知供给方进入隔离终态。上线前先量化 `[本品+第三方] ∩ 明确向本品牌提要求`，并用成对样本锁住供给方差异。

### B04. G1b、父标题和 SPU 继承共用一个不完整线程键；缺根、NULL 组均 fail-open，跨平台同 ID 会串组

- **文件:行号**：`pipeline/voc_analytics/db.py:194-201,229-237,315-324`；`pipeline/sql/023_social_thread_columns.sql:16-18,48-77,80-95`；`pipeline/voc_analytics/clean.py:292-296`。
- **具体失败场景**：评论 `message_group_id=NULL` 或根帖未导入/类型变成非“帖子、视频”，SQL 的根行 `NOT EXISTS` 为真，评论自己 brands 干净就通过整组竞品内容；Python 纯函数却以 `None == None` 把所有 NULL 组根相互匹配，与生产 SQL 不等价。另一个场景是 TikTok 与 Instagram 都有 `group_id='123'`：一边竞品根会误杀另一边本品评论；父标题可串帖；023 还可能让一边继承另一边 SPU，或把两个真实单 SPU 线程误合成冲突组。
- **为什么现有测试没抓到**：`test_generation_pool_contract.py:40-46` 只有正常非空、不同 group ID；`test_intent_gate_recall.py:218-234` 只确认缺根时标题为“（无）”，没有缺根终态、NULL SQL 语义、跨平台碰撞或多根测试。
- **建议修法**：评论/回复没有规范线程键、没有同平台同来源唯一根、或根品牌未知时进入显式“上下文缺失/品牌未知”终态。把 `(src_line, platform, message_group_id)` 固化为 canonical thread key，并优先解析可靠 `parent_id -> root message_id`；G1b、父上下文、继承必须共用它。若坚持当前键，必须在应用 025 前用生产只读查询证明 group ID 跨平台全局唯一、每组根唯一且建立持续约束，不能靠口头假设。

### B05. 本次重灌的五个线程字段没有 header/填充率/边界根/实际生成域完整性闸门

- **文件:行号**：`pipeline/voc_analytics/ingest.py:11-20,60-99`；`pipeline/voc_analytics/clean.py:250-297`；`pipeline/voc_analytics/config.py:89,94-99,180-181`；`pipeline/voc_analytics/yunting.py:76-81`；`pipeline/scripts/run_ingest.py:43-63`；`pipeline/scripts/run_generate.py:172-205`；`pipeline/scripts/rerun_both.sh:215-225,293-301`。
- **具体失败场景**：导出列名变成 `消息组ID `（尾空格）或缺列，`read_xlsx` 不规范化/校验 header，五个 `row.get` 全取到 None；消息数、证据数和周状态仍可成功，G1b、父标题、G3、继承同时失效。当前只重灌 W08..W33 还会漏掉 W07 根帖：W08 评论有新 group 字段，窗外根仍没有新列，结果等同缺根。更大范围的确定冲突是重跑使用 `--full-history`、事实保留 24 个月；任何 W08 以前的社媒行都会参加生成，但未被这次 26 周补五列。社媒抽取还在单行层下推本品品牌过滤；若云听不自动展开整组，竞品/无品牌根会先被过滤，只留下命中本品的评论，G1b 系统性查不到根。
- **为什么现有测试没抓到**：`test_clean_fields.py:109-147` 只构造完全匹配的 dict；没有 XLSX header 漂移、字段填充率、`message_type` 词表、根覆盖率或跨窗口根补齐测试。
- **建议修法**：每个社媒 blob 在写库前规范化并校验五列存在、无重复；`message_type` 必须过枚举；按来源/周记录五列填充率、评论有根率、组多根率、NULL group 数并设阻断阈值。命中评论后必须按 group/root ID 补抓根行，不受品牌与 publish window 过滤。重灌范围要覆盖实际 `generation_pool(None,None)` 的全历史域，或把重跑显式限制为同一 26 周；清库前必须断言实际生成池中五列未回填行和缺根评论均为 0。

### B06. G4 缓存身份不含正文和父上下文，本次同 ID 重灌会复用过期结论

- **文件:行号**：`pipeline/sql/023_social_thread_columns.sql:22-33`；`pipeline/voc_analytics/db.py:125-145`；`pipeline/voc_analytics/stages/value_gate.py:156-184,211-229`；`pipeline/voc_analytics/db.py:88-113`；`pipeline/scripts/rerun_both.sh:274-286`。
- **具体失败场景**：M 的正文从“有没有黑色款”改成“什么时候出黑色款”，或评论不变但根标题从第三方产品纠正为 ULANZI 产品；实际 prompt 输入已经改变，缓存仍只以 `message_id + prompt_ver` 命中旧 cls。清层脚本不清 `voc_social_gate`。因此只要重灌期间或之前已经产生 v2 cache，该行就已满足错误复用条件；是否已有这类生产行需只读审计确认。
- **为什么现有测试没抓到**：`test_intent_gate_recall.py:182-201` 只测试完全相同输入的缓存命中，没有正文、snippet、根标题、group/platform 或投票策略变化后的失效用例。
- **建议修法**：缓存主身份至少为 `(message_id, decision_ver, input_hash)`；hash 覆盖最终渲染后的正文/聚合片段、选中的根与父上下文、平台、message type/group，以及模型和投票策略 fingerprint。当前上线要冻结事实后清理/升版并重判所有 v2 结果；仅加 content hash 不够，因为父帖变化也会改答案。

### B07. `rerun_both` 会让两个生命周期对同一整池 G4 冷缓存双跑并争写同一主键

- **文件:行号**：`pipeline/scripts/rerun_both.sh:293-303`；`pipeline/voc_analytics/pipeline.py:139-184`；`pipeline/voc_analytics/stages/value_gate.py:160-173,196-229`。
- **具体失败场景**：existing 与 innovation 同时启动；`opp_types` 过滤发生在 G4 之后，两边都先读取全部社媒缓存、判全部消息。冷缓存下会重复约 8k 次判定并 last-writer-wins。若同一无 SPU 消息一边判“诉求”、另一边判“缺陷”，两个进程按各自内存结论路由，最终缓存只保留最后写者，生成结果与审计缓存不可复现，且两个独立对账都可为 complete。
- **为什么现有测试没抓到**：`test_rerun_both_contract.py:11-42` 只静态检查进程监督和 finalize 顺序；G4 测试均为单进程，无 cache claim/并发测试。
- **建议修法**：G4/G5 先由一个进程对冻结事实物化不可变判定快照，两个生命周期只读同一 run snapshot；或本次直接单进程 `lifecycle=both`。长期需要 DB 原子 claim/lease，确保同一 input hash 只判一次并返回同一 judgment。

### B08. SPU 继承会把“第三方设备新品需求”确定性改路由成某个老品迭代

- **文件:行号**：`pipeline/sql/023_social_thread_columns.sql:48-77`；`pipeline/voc_analytics/classification.py:17-30,67-72`；`pipeline/voc_analytics/routing.py:88-107`；`pipeline/tests/test_lifecycle_routing.py:33-64`。
- **具体失败场景**：帖子挂本品 SPU-A，评论“能不能给 Luna 做个散热器”。v2 会把它判为诉求缺口；线程事实并集恰为 A，评论继承 A；G5 只看“是否有 SPU”，于是确定进入 A 的老品迭代。业务语义却是为另一设备开发新配件，应为新品创新或至少待归属，不是改 A。
- **为什么现有测试没抓到**：G5 测试只验证“请求+任意 SPU→老品”和“继承 SPU 被认可”，没有验证 inherited SPU 与 claim 主体是否一致；G4 和 G5 分层测试让该组合缺口不可见。
- **建议修法**：继承不是无条件事实替身。G4/G5 之间增加“claim 是否指向根产品”的归属判定；明确新产品/第三方适配件诉求不得继承根 SPU，转新品或待归属。至少为“改根产品能力”与“为第三方设备另做配件”建立成对端到端测试。

### B09. `MIN_EVIDENCE=2` 只存在于 prompt，老品单证据没有任何程序闸；失败批还会批量制造 singleton

- **文件:行号**：`pipeline/voc_analytics/config.py:143-145`；`pipeline/voc_analytics/stages/stage1.py:74-97,139-163,289-336`；`pipeline/voc_analytics/pipeline.py:289-344,688-795,969-975`。
- **具体失败场景**：一个老品桶只有一条证据，LLM 返回 `evidence_idx=[1]`，parser 接受、图算法保留单节点、pipeline 正常生成并落库 `evi_total=1`。更严重的是 300 条/6 批中 1 批 50 条失败（16.7%，低于容忍阈值）：失败 result 被删除，但 `members=set(range(n))` 仍包含这 50 条，它们没有边，会各自成为“未命名模式”singleton。
- **为什么现有测试没抓到**：`test_lifecycle_prompt_routing.py:16-38` 本身用一条老品证据并接受输出；`test_stage1_accounting.py:81-111` 正好构造 6 批坏 1 批，却只断言有 group/失败批计数，不核对坏批成员归宿和 group size。
- **建议修法**：在 Stage1 合并后、持久化前两处都按明确的独立证据单位硬验 `MIN_EVIDENCE`；不足者进入可检索的 subthreshold 终态，不得 attach opportunity。失败批成员必须进入显式 failed/unclassified，而不是回流图。这里还必须先定义“独立证据”是 distinct message，还是 distinct thread；不能继续以数组 seq 当独立用户声音。

### B10. 026 的 `HAVING count(*) >= 2` 统计 evidence 行，不是独立消息/线程，会凭空成卡并扭曲 `n_eff`

- **文件:行号**：`pipeline/sql/026_spu_layer_inherited.sql:25-56,75-101`；`pipeline/sql/023_social_thread_columns.sql:48-77`。
- **具体失败场景**：同一条评论因两个标签产生 `seq=0/1`，两行都继承 A 并挂同一机会，只有一个用户/一条消息却直接满足 `count(*)=2` 成卡。根帖 A 下两条跟随性评论也会成卡；50 条同线程评论计 50。若 A 在一个热帖 50 行，B/C 各 1 行，现公式为 `52²/(50²+1²+1²)=2704/2502≈1.081`，判“单品”；按三个独立归属上下文各 1 票应为 3，判“品线级”。
- **为什么现有测试没抓到**：仓库没有任何 026 测试；`test_gate_migrations.py:1-53` 只覆盖 023–025，历史测试也没有同 message 多 seq 或同 thread 多评论夹具。
- **建议修法**：拆开三个量：raw voice 数、独立消息数、独立归属上下文数。成卡至少按 distinct message；对 inherited 归属和 n_eff，应按 `COALESCE(canonical_thread_key,message_id)` 每 SPU/机会/线程封顶一次，或要求多个线程交叉支持。原始 50 条仍可显示为热度，但不能同时当 50 个独立 SPU 归属证据。

### B11. 026 修了两个生产定义，但六个产品卡消费者仍只认事实 SPU

- **文件:行号**：`pipeline/sql/026_spu_layer_inherited.sql:25-56,79-101`；`system/app/queries.py:534-541,731-751,918-938,975-996,1028-1043,1181-1199`。
- **具体失败场景**：两条仅 `spu_inherited=[A]` 的评论让 026 生成 `(A,O,evi_count=2)`；看板 recent CTE、问题聚合、问题详情和 `ISSUE_VOICES` 都以 `msg.spu` 过滤，页面会显示卡片计数 2，却下钻 0 条原声、近期数 0、taxonomy 为空；发布后的 inherited-only 新反馈也永不触发完成态复活。
- **为什么现有测试没抓到**：`system/tests/test_query_metrics.py:96-101` 只锁发布时间字段；`:189-197` 只检查 raw voice 查询已经纳入 inherited，没有覆盖“已成问题卡”这六条路径。route smoke 使用互不关联的假结果。
- **建议修法**：建立单一 `effective_message_spu` view/helper，语义采用经约束的“事实优先，否则 inherited”，所有列表、详情、原声、复活和 025/026 共用；补 inherited-only 从关系层到页面的端到端 fixture。026 不能在这些消费者统一前上线。

### B12. “单证据在产品检索合并页一定出现”没有闭环，已挂机会但未成 SPU 卡的信号会两头消失

- **文件:行号**：`pipeline/sql/026_spu_layer_inherited.sql:47-56`；`system/app/queries.py:542-560,877-901`。
- **具体失败场景**：机会 O 共两条证据，一条挂 A、一条挂 B。O 在机会层总数为 2，但 `(A,O)`、`(B,O)` 各只有 1，均不生成 `voc_spu_issue`；raw voice 又只要 `message_id` 出现在任意 `voc_opp_evidence` 就全局排除。最终 A/B 产品页都显示 issue=0、raw=0。B09 中错误生成的 singleton 也会同样消失。
- **为什么现有测试没抓到**：没有 pipeline→026→产品页跨层测试；`test_query_metrics.py:189-197` 反而把“任一已使用消息都排除”锁成当前契约，没有按目标 SPU/seq/可见卡判断。
- **建议修法**：产品层增加 subthreshold signal feed；或 raw voice 仅排除已经由目标 `(spu,opp)` 可见问题卡承载的具体 `(message_id,seq)`，不能按 message 进入任何 opportunity 就排除。上线验收应构造 A/B 各一条、同消息多 seq、单条未成卡三种 fixture。

### B13. 产品页 raw voice 可绕过 G1–G3、当前 prompt 版本以及“诉求过泛”终态

- **文件:行号**：`pipeline/voc_analytics/stages/value_gate.py:220-229,255-262`；`system/app/queries.py:542-560,877-901`。
- **具体失败场景**：M 的 G4 原始输出是诉求，claim=`需要某款待发布新品`；pipeline 在 `is_generic_claim` 后终止、不入生成池，但 cache 仍保存正类，产品页直接 join cache 就展示它。另一场景是旧 cache 正类，但重灌后 M 的 brands/content_type/author 已命中 G1/G2/G3；生成池拒绝，页面仍展示旧正类。查询也不限制 `prompt_ver`。
- **为什么现有测试没抓到**：`system/tests/test_query_metrics.py:189-197` 只断言 cls 正类和 NOT EXISTS；没有结构门、generic terminal、decision version 或 input hash 的约束。
- **建议修法**：持久化一次权威的最终 eligibility/terminal snapshot，而不是让页面从原始 G4 cls 重新猜资格；所有展示只读当前输入指纹匹配、已通过 G1–G5 的状态。迁移前清除失效 cache 并用结构拒绝/泛化 claim fixture 验证页面为 0。

### B14. “电商入口保持原契约”已被代码破坏：生成池漏掉 `NOT e.low_conf`

- **文件:行号**：`pipeline/voc_analytics/db.py:301-356`，尤其 `:333-340`；`pipeline/voc_analytics/clean.py:227-245`；`pipeline/sql/015_classification_contract.sql:167-180`；`pipeline/tests/acceptance.py:44-46`；`pipeline/tests/test_generation_pool_contract.py:78-113`。
- **具体失败场景**：5 星评论被云听打成负面产品标签，clean 标记 `low_conf=True`；当前电商 WHERE 仍将其送入老品生成。历史活指标与 acceptance 明确把这类行剔除，导致机会证据/卡片包含它，而 weekly metrics 和 usable pool 分母不含它，口径相互矛盾。
- **为什么现有测试没抓到**：generation pool 测试只搜产品、负面、snippet、SPU 四个条件，恰好没有断言 `NOT e.low_conf`；没有固定电商 fixture 对比重构前后结果集。
- **建议修法**：恢复 `AND NOT e.low_conf`（若业务想改变，必须作为显式电商口径变更重标，而不能宣称零影响）；增加 low_conf 正反数据测试和电商独立守恒账。全量重跑前对旧/新电商池做 exact message/seq diff，差异必须逐类解释。

### B15. 五门对账的分母由同一过滤后 CTE 构造，能在静默丢行、空池和部分产出时“全绿”

- **文件:行号**：`pipeline/voc_analytics/db.py:244-245,258-298,328-354`；`pipeline/voc_analytics/pipeline.py:398-570`；`pipeline/scripts/rerun_both.sh:214-246`。
- **具体失败场景**：`content=NULL、snippet=NULL、content_zh='希望出白色版'`，虽然 G4 取文支持 translation fallback，该行在候选分母前被删；无 evidence 或 `publish_time=NULL` 的消息也同时从结构计数和 pool 消失。四分区仍由同一 candidates CTE 导出，恒等式必成立。更危险的是所有计数为 0 时 `complete=True`，preflight 只打印 `POOL_ROWS` 不拒 0，脚本可清空机会层后“成功生成 0 条”。电商也没有候选/终态守恒；社媒正常即可掩盖电商全丢。
- **为什么现有测试没抓到**：`test_generation_reconciliation.py:139-187` 直接注入已经裁剪好的整数；`test_generation_pool_contract.py:78-134` 只做 SQL 字符串检查。没有 raw ingress→终态、content_zh-only、无 evidence、NULL time、全零或电商独立账测试。
- **建议修法**：从消息/导出 manifest 定义五门之前的原始 universe，LEFT JOIN evidence，并显式统计无 evidence、无可判文本、无发布时间、上下文缺失等预门终态。候选池非空、两来源最小量级和分来源守恒必须是清库前硬门；结构 count 与 generation pool 应在同一快照/事务读取。`llm_votes>=0` 之类恒真条件应换成 attempts/success/failure 对账。

### B16. 026 创建新口径物化视图后不立即 refresh/self-check，步骤 4 成功也会留下半套口径

- **文件:行号**：`pipeline/sql/026_spu_layer_inherited.sql:21-124`；对照 `pipeline/sql/018_n_eff_source_alignment.sql:79-111`；`pipeline/tests/test_gate_migrations.py:1-53`。
- **具体失败场景**：应用 025+026 后，`CREATE MATERIALIZED VIEW voc_spu_issue` 已立即按继承生成卡片，但 026 只替换 `voc_refresh_spu_layer()`，没有调用。若步骤 5 的长重跑失败或迟迟未执行，现存 opportunity 的 `n_eff/scope` 仍停在 018 的事实-only 结果，产品卡与战略分档长期矛盾。025、026、024 又是三个独立事务，后一个失败不能回滚前一个。
- **为什么现有测试没抓到**：026 完全没有迁移测试；`test_finalize_refreshes_spu_layer.py:60-65` 只证明未来成功 finalize 会调函数，不证明迁移提交时一致。026 也没搬回 018 的三条断言。
- **建议修法**：026 事务内立即执行 refresh，并复用/加强 018 自检（含 inherited-only expected set、scope NULL 配对、阈值）；025+026 应作为一个可回滚的 additive 发布单元。长重跑失败不能成为依赖未来修复半口径的恢复策略。

### B17. v3 身份没有解决 98% churn，并可用 exact ID 推翻 L3 的 `different`

- **文件:行号**：`pipeline/voc_analytics/resolve.py:23-35,76-107`；`pipeline/voc_analytics/pipeline.py:13-42,748-767,892-960`；`pipeline/scripts/rerun_both.sh:274-286`；`pipeline/sql/024_drop_channel.sql:7-12`；`pipeline/tests/test_opportunity_identity.py:29-76`。
- **具体失败场景**：同一证据两轮生成“按键无法回弹”与“按键回弹失效”，v3 hash 不做同义归一，产生两个 ID；即使文本完全相同，material 中 `version=3` 也让 v2→v3 首次 digest 必变。反向碰撞也会误并：摄影灯与麦克风都生成“按键故障/按键无法响应”时，title/原声能证明是不同产品；即使 L3 已判 different 并返回 create，二者 v3 ID 相同，exact-ID 分支仍强制 attach，推翻语义裁决。若旧两卡只因 channel 不同而分开且各有人工状态，024 先删 channel/清层后再映射，还丢掉解释多对一冲突的唯一结构字段。
- **为什么现有测试没抓到**：identity 测试只喂固定字面和标点/NFKC 等价；`m35_stability.py:2-10` 明说不测真实周次 opp_id。没有 L3-different+same-hash、真实同证据双跑、v2→v3 golden mapping、人工状态/墓碑/合并链连续性测试。
- **建议修法**：canonical 用不可变 surrogate/registry，LLM 文本与语义簇只作版本化匹配特征；身份域若不含产品/品类，就必须给 L3 different 分配独立 opaque ID，exact hash 不能推翻语义裁决。024 前影子生成并永久保存 identity preimage、old→new alias 与 1↔N 冲突清单，人工裁决后才能切换。上线门应报告跨周 attach 率、exact-ID 保持率、未映射旧 ID 数和所有人工引用迁移结果。

### B18. 清层重跑既可能被人工历史外键直接阻断，也可能在失败后留下半代活数据

- **文件:行号**：`pipeline/scripts/rerun_both.sh:274-286,293-303,364-390`；`pipeline/sql/001_schema.sql:225-247`；`pipeline/sql/009_spu_layer.sql:132-162`；`pipeline/voc_analytics/pipeline.py:874-998`。
- **具体失败场景**：任一 `voc_opportunity_manual`、`voc_status_log`、`voc_spu_issue_manual`、`voc_spu_issue_log` 行都通过非级联 FK 锚定 opportunity，`DELETE FROM voc_opportunity` 会失败，脚本无法开始。若人为清这些表来放行，脚本还会 truncate proposal/lineage/snapshot，墓碑、合并链、复活基线和审计丢失。即使无人工行，清库后机会逐条事务提交；第 301 条失败时线上保留 300 条半代新卡，旧层已没、派生层/NN 因 finalize 未跑仍可能引用旧 ID。
- **为什么现有测试没抓到**：`test_rerun_both_contract.py:11-42` 只检查 shell 监督；`test_persistence_accounting.py:21-54` 甚至明确接受“第一条已提交、第二条失败”。没有失败后线上可见代不变、人工 FK、历史 replay 或回滚演练测试。
- **建议修法**：按 `run_id/generation_id` 在影子层完整生成、finalize、对账和映射，最后单事务切 active pointer/rename；旧代留作回退。人工层、alias、lineage、snapshot 是永久资产，不得通过清空来重建。应用 024 前必须输出四张 FK 表和 proposal/lineage/snapshot 的引用清单及迁移结果。

### B19. 024 自身可事务回滚，但“024 + 多小时重跑同批”并不原子，且回滚不能恢复旧 channel 值

- **文件:行号**：`pipeline/sql/024_drop_channel.sql:1-74`；`pipeline/scripts/rerun_both.sh:274-390`；`system/app/queries.py:563-570,635-637,862-894`。
- **具体失败场景**：024 先提交删列，随后清层或模型/refresh 失败；数据库不能把数小时后的失败回滚到 024 前。简单 `ADD COLUMN channel` 也只能得到全 NULL，旧值及其身份解释只能从备份恢复。另一个版本偏斜是当前系统查询使用 025 才提供的 `voc_spu.has_ec`：先部署新 app、后上 025 会令 `/iter` 报缺列；让旧 app 活到 024 后，又可能访问已删 channel。
- **为什么现有测试没抓到**：`test_gate_migrations.py:30-42` 只检查三个仓内视图的 drop/recreate 文本；没有 app×schema 版本矩阵、失败恢复或备份 restore drill。仓内直接视图依赖闭包不等于生产外部 BI 依赖闭包。
- **建议修法**：先上 additive 025/026 并保持旧 app 兼容；部署新 app 后在真库只读 smoke；影子重跑和 ID 映射验收完成后切 active 代；最后单独执行 024。删除前持久化 channel preimage/旧→新映射并演练恢复。生产 catalog 中所有依赖与外部消费者必须由上线方只读盘点。

### B20. 新到的安全信号可被普通 canonical 吞掉，`safety_flag` 不会随证据回算

- **文件:行号**：`pipeline/voc_analytics/resolve.py:76-107,120-170`；`pipeline/voc_analytics/pipeline.py:818-871,874-956,993-994`。
- **具体失败场景**：库内已有普通“电池过热”机会 `safety=false`；新批证据触发 `safety=true`，L3 判 same。resolver 只检查候选是否安全，不检查 incoming safety，自动 attach 到普通卡；`recount()` 只重算计数/类型，不 OR `safety_flag` 或安全证据 ID，结果仍不进 `safety_watch`、也不加安全排序分。cross-source merge 同样读取候选 safety 后未使用。
- **为什么现有测试没抓到**：现有 safety 测试覆盖“已有安全候选不自动合并”，没有 incoming-safe/candidate-normal 的反向组合，也没有 cross-source safety 矩阵或 attach 后安全字段回算。
- **建议修法**：任一侧 safety=true 都禁止自动 attach/cross-source merge，转人工；或建立证据级安全事实并在 recount 中权威 OR 回算 flag 与 evidence IDs。加入双向 2×2 测试以及“安全证据附到普通卡后仍进入 safety_watch”的端到端契约。

## 重要（可在上述阻断解除后的首个修复窗口处理）

### I01. G4 上下文不足，存在“模型输入完全相同但正确答案不同”的不可判碰撞

- **文件:行号**：`pipeline/voc_analytics/db.py:307-326`；`pipeline/voc_analytics/stages/value_gate.py:40-71,175-184`。
- **具体失败场景**：两条根帖正文都为“用了三次就坏了”，标题分别是 ULANZI 与竞品；根帖自身一律不喂标题，prompt 完全相同。评论“我的也断了”只拿到根标题“新品实测”，而两个根正文分别是 ULANZI/竞品，输入仍相同。嵌套回复也不读直系父评论。
- **为什么现有测试没抓到**：`test_intent_gate_recall.py:218-234` 反而锁定根帖标题返回“（无）”；没有根正文、父评论或不可区分成对样本。
- **建议修法**：根帖传自身标题；评论/回复传有界根标题+根正文+必要父链，并把选中的上下文 ID 和正文纳入 cache hash。对上下文缺失保守隔离，不把未知主体强行当本品。

### I02. G4 投票不是可解释的多数机制，任务书对 0.0 样本的描述与代码不相容

- **文件:行号**：`pipeline/voc_analytics/config.py:147-151`；`pipeline/voc_analytics/stages/value_gate.py:105-125,186-204`；`pipeline/voc_analytics/prompts.py:124-127`。
- **具体失败场景**：fresh 首票只要 confidence<0.8，不论 cls 都会补票，因此 46 条 confidence=0.0 若真走这段代码必全部触发，和“只有缺陷补票”不能同时为真。三票若分别为诉求/缺陷/无价值，没有多数，代码按首次出现顺序选首票。高置信误放则永不复核；同 prompt、同模型、近确定性参数的三票也未证明有独立纠错价值。
- **为什么现有测试没抓到**：`test_intent_gate_recall.py:149-179` 只 mock 2:1 和三次同值；没有 1:1:1、0.0 无价值、真实翻转率/纠错率。prompt 也没有定义 confidence 是“所选类概率”还是“有价值概率”。
- **建议修法**：先追溯 150 样本是否 cache hit/是否绕开 `apply_value_gate`，重新记录 first cls/conf、触发原因和逐票结果；定义 confidence 语义。若保留，1:1:1 必须显式不确定/追加独立裁决，不能选首票。以标定集衡量增量纠错；无收益就删除低置信补票，只保留有证据的高风险复核策略。

### I03. v2 新规则没有混合意图优先级，第三方“要求”边界仍可把竞品/不可控诉求放进来

- **文件:行号**：`pipeline/voc_analytics/prompts.py:69-96,101,111-127`；`pipeline/voc_analytics/stages/value_gate.py:17-24,74-83`。
- **具体失败场景**：“我有 MA66，PK-11 根本装不上，能兼容吗？”同时命中购前问句和 2b 缺陷；“有没有白色款？没有的话希望出一个”同时命中存在性否决和明确诉求；“优篮子能不能催索尼修 A7M4 自动关机”形式上是对本品牌提要求，却不是本品牌可交付机会。模型若归一 claim 为“需要新品/需要配件”，`is_generic_claim` 因不含三种 placeholder 还会放行空泛吸附器。
- **为什么现有测试没抓到**：G4 测试的 responder 直接按用例 expected 回答，没有真实解析 v2；用例缺 Luna、2b、存在性、报名与混合意图，generic 只测空串和已知占位词。
- **建议修法**：规定覆盖顺序：已拥有并发生问题的缺陷优先；购前/报名只在没有独立诉求或缺陷时否决；第三方例外仅限本品牌可控制的影像配件交付物且供给方明确。所有 claim 都剥通用词后要求具体实体/能力，不以三个 placeholder 为前置。

### I04. G3 不是官号识别：漏掉 JOBY/FALCAM，又会误杀含品牌名的普通用户；同一别名缺口还污染 grounding

- **文件:行号**：`pipeline/voc_analytics/config.py:75-87`；`pipeline/voc_analytics/db.py:242-243`；`pipeline/voc_analytics/stages/validate.py:468-485`；`pipeline/tests/test_grounding.py:210-233`。
- **具体失败场景**：`JOBY Official`、`FALCAM Official` 不命中当前 aliases，可进入 G4；普通用户名“ULANZI器材玩家”“小隼用户交流群”因任意品牌子串被 G3 拒绝。后续 grounding 甚至把正文 `FALCAM电池…` 当第三方并拒绝本品牌标题。
- **为什么现有测试没抓到**：G3 测试遍历的正是同一份不完整 aliases；grounding 测试 `test_grounding.py:210-226` 还显式把 FALCAM 固化为第三方，`:229-233` 的本品表同样漏英文名。
- **建议修法**：建立 Ulanzi/Joby/Falcam canonical brand map，存储值、正文别名、官号身份分别派生；官号优先使用稳定 account ID/认证字段，不用品牌子串。反转错误 FALCAM 测试并增加三品牌中英文对称契约及普通昵称负例。

### I05. 同一消息挂多 SPU 时，所有 evidence seq 被笛卡尔复制给所有产品

- **文件:行号**：`pipeline/sql/009_spu_layer.sql:65-73,98-123,233-254`；`pipeline/sql/026_spu_layer_inherited.sql:25-56,79-101`。
- **具体失败场景**：消息 `spu=[A,B,C]`，seq0 的原声实际只谈 A；SQL 给 A/B/C 各计 1。两条此类消息会让 B/C 也错误成卡，单条就让该机会的 `n_eff` 朝 3 拉高。
- **为什么现有测试没抓到**：没有 evidence→SPU 细粒度归属 fixture；历史注释把“多 SPU 各计一次”当既定口径，没有验证各 seq 的语义主体。
- **建议修法**：增加 evidence 级 SPU 归属；无法区分的多 SPU 证据标 ambiguous，不参加产品成卡/n_eff，或进入人工确认。至少不能用 message 数组无条件扩散到每个 seq。

### I06. 上游事实/G1–G3/继承变化不会撤销已经挂上的机会证据

- **文件:行号**：`pipeline/voc_analytics/db.py:88-117`；`pipeline/sql/023_social_thread_columns.sql:65-95`；`pipeline/voc_analytics/pipeline.py:969-975,1001-1011`。
- **具体失败场景**：W1 评论继承 A、判缺陷并建老品机会；W2 同组晚到事实 B，继承被清空，按当前 G5 应变成“缺陷不可归属”。系统只 upsert 新关系、不因资格下降 detach，旧机会仍为老品且保留证据；026 刷新后产品卡可能消失，但机会列表仍存在。content_type 从体验改评测、brands 改竞品也同理。
- **为什么现有测试没抓到**：测试只覆盖 attach 后 recount，没有 pass→drop、单 SPU→冲突、输入 hash 变化后的反向失效时序。
- **建议修法**：入库/继承重算输出 changed message IDs，对所有相关 `voc_opp_evidence` 重新求当前资格并事务性 detach/recount，保留审计；或让关系绑定 eligibility version，查询只认当前版本。补两类降级时序测试。

### I07. 025 的社媒-only SPU 容器没有产品名/SKU/消息数，正常入口又默认隐藏 raw-only 产品

- **文件:行号**：`pipeline/sql/025_spu_scope.sql:20-114`；`system/app/routes/board.py:64-79`；`system/app/routes/search.py:15-23`；`system/app/queries.py:563-570,666-670`；`system/tests/test_product_search.py:33-40,131-171`。
- **具体失败场景**：仅社媒存在 SPU-A。025 的 product_names/skus/message_count 全来自电商 CTE，因此按名称/SKU搜不到、详情显示消息 0。侧栏 `/iter` 默认 `tracked`，open issue=0 的 raw-only SPU 被过滤；搜索表单保留该 filter。只有用户先切“全部”或走没有正常导航的旧 `/search` 才看到。
- **为什么现有测试没抓到**：产品搜索测试伪造了生产不可能的 `has_ec=false` 但 product_names 非空、message_count=8；另一些测试反而锁住默认 tracked 行为，没有业务要求“从正常入口一定可发现”的测试。
- **建议修法**：为 social-only 容器提供可追溯的名称/SKU/消息数回退，或明确 UI 只显示 SPU 码；搜索时强制 all/包含 raw signal，并在正常导航暴露入口。用真实 025 结果形状做 route 集成测试。

### I08. 026 改统计口径会让旧“不考虑”基线发生假复活

- **文件:行号**：`pipeline/sql/009_spu_layer.sql:167-193`；`pipeline/sql/026_spu_layer_inherited.sql:25-56`；`system/app/queries.py:1169-1178`。
- **具体失败场景**：PM 拒绝时 baseline=2（旧事实-only）；026 一次性纳入 4 条早于拒绝决定、此前已存在的继承评论，current=6，立即满足 `>=3*baseline`，系统声称“新反馈复议”，实际没有一条决定后的新增声音。
- **为什么现有测试没抓到**：现有测试只验 3 倍算术，没有 scope version、decided_at 或迁移前后历史证据夹具。
- **建议修法**：baseline 绑定 count_scope_version；迁移时重基准或只计算决定时间之后的独立消息/线程。应用 026 前列出所有会被瞬时触发的 manual rows，由业务确认。

### I09. 025/026 未固定执行角色和 schema，错误部署身份可在迁移后才暴露 refresh 权限问题

- **文件:行号**：`pipeline/sql/025_spu_scope.sql:3-123`；`pipeline/sql/026_spu_layer_inherited.sql:21-73`；对照 `pipeline/sql/015_classification_contract.sql:9-16`。
- **具体失败场景**：以 postgres/superuser 重建 MV，owner 变成该用户；`CREATE OR REPLACE FUNCTION` 可能保留原 owner `voc_admin`，SECURITY DEFINER 函数以后 refresh 新 owner MV 时失败。非默认 search_path 也可能让未限定 DROP/CREATE 落到错误 schema；026 不主动 refresh，迁移时还不暴露。
- **为什么现有测试没抓到**：迁移测试只读字符串，不在非默认角色/search_path 下执行；任务边界又禁止本评审连接验收库。
- **建议修法**：加入 `SET LOCAL search_path=public,pg_temp`、`current_user='voc_admin'` fail-fast 守卫和全限定名；事务内真实 refresh/self-check。上线 runbook 固定执行身份并记录对象 owner 验证。

### I10. 单组构建失败被允许整轮 complete，却没有持久化失败证据清单

- **文件:行号**：`pipeline/voc_analytics/pipeline.py:318-361,398-570`；`pipeline/tests/test_grounding.py:302-324`。
- **具体失败场景**：100 个 group 中 2 个 Stage2/3/4 校验失败，代码丢弃对象并将 `failed_groups=2`，`complete` 明确允许 `gf>0`；失败 group 的 message/seq、错误类型和可恢复终态没有落库，用户只能看到 98 个机会且 run=complete。
- **为什么现有测试没抓到**：grounding 测试明确接受 `complete=True`；对账只比较 group 数相加，没要求失败成员可追踪/可重试。
- **建议修法**：若业务允许部分质量失败，状态应为 `complete_with_rejections`，把每个失败 group 的 evidence IDs、阶段、错误和 retry key 持久化；发布闸门设数量/比例，并保证失败证据仍可从产品检索层发现。否则 fail-stop。

### I11. 数组 parser 会因原声内逗号造成 seq 错配，监控却不检查 snippets 长度

- **文件:行号**：`pipeline/voc_analytics/clean.py:76-87,200-226`。
- **具体失败场景**：tags=`[电池, 防水]`、sentiments=`[负面, 负面]`、snippets=`[电池续航短, 但充电快, 下雨进水]`。裸逗号切成 3 段，防水标签拿到“但充电快”，“下雨进水”丢失；misaligned 只比较 tag 与 sentiment 长度，仍为 0。
- **为什么现有测试没抓到**：`test_clean_fields.py:179-187` 与 `test_null_sentinel.py:48-65` 只覆盖无内嵌逗号/字面 null，没有 snippets 长度或转义格式测试。
- **建议修法**：优先要求上游提供 JSON/可转义数组；在此之前三数组长度任一不等就阻断，不能继续错配。空槽 parser 必须保留位置。

### I12. identity normalizer 会把有语义的数字分隔符抹成同一键

- **文件:行号**：`pipeline/voc_analytics/pipeline.py:13-42,917-924`；`pipeline/tests/test_opportunity_identity.py:52-59`。
- **具体失败场景**：同 core tag 下，“接口规格为 1/4 英寸”与“接口规格为 1-4 英寸”分别表示四分之一和范围，却都被 `/`、`-` 折成空格，得到同一 ID；exact-ID 冲突校验也使用同一归一化材料，抓不到并会强制 attach。
- **为什么现有测试没抓到**：测试只把斜杠/连字符等价当正例，没有数字、负号、型号等必须保留的反例。
- **建议修法**：在数字/型号 token 内保留 `/`、`-` 和负号语义，保存 exact identity preimage；加入尺寸、温度、型号范围对抗样本。

### I13. `cross_source_merge` 实际是双向复制证据，不是 canonical merge；补证后还漏更改周次/快照

- **文件:行号**：`pipeline/voc_analytics/resolve.py:110-171`；`pipeline/voc_analytics/pipeline.py:1001-1011`；`pipeline/scripts/run_generate.py:65-125`。
- **具体失败场景**：电商 A 与社媒 B 是同一语义但已成为两个 canonical。finalize 开始时把两者都装入 `created`；处理 A 时复制 B 的证据给 A，随后处理 B 又复制 A 的原证据给 B，源 opportunity/关系从不删除、不置 `merged_into`。结果两卡都变成双源并共享同批证据，SPU 卡和排名双计。若只有 A 获得本周 cross-source 新证据，`attach_evidence()` 只写关系并 recount，不更新 `last_week`；snapshot 又只选 `last_week=本周`，因此本周计数已变但没有本周快照。
- **为什么现有测试没抓到**：仓库没有 cross-source A↔B 行为测试；`test_finalize_refreshes_spu_layer.py:33-57` 只静态验证调用顺序，不验 canonical 数、证据全局归属、last_week 或 snapshot 集合。
- **建议修法**：L3 same 应执行事务级 canonical merge/alias，或至少只按稳定方向单向 attach 并 tombstone 源；建立证据全局归属/对称去重不变式。cross-source 事务应更新 `last_week=max(last_week, evidence week)`，snapshot 以本轮 touched opp IDs 为准。补 A↔B、旧卡本周补证和重试幂等测试。

## 建议

### S01. 将 G4 最终判定快照设计成可审计数据，而不只是覆盖式 cache

- **文件:行号**：`pipeline/sql/023_social_thread_columns.sql:22-33`；`pipeline/voc_analytics/stages/value_gate.py:105-125,220-229`。
- **具体失败场景**：三票为两票诉求 `.95`、一票无价值 `1.0`，最终只保存 `.95/votes=3`；每票 cls/reason/confidence/seed 和触发原因丢失，与三票一致无法区分。
- **为什么现有测试没抓到**：只断言最终 cls/votes，不校验逐票审计与策略版本。
- **建议修法**：保存 immutable decision run、input hash、模型/策略版本、首票、逐票详情、vote share、最终 terminal 和 route；覆盖式表只作当前指针。

### S02. 统一 SQL/Python 的“可判文本”和数组 NULL 语义

- **文件:行号**：`pipeline/voc_analytics/db.py:168-182,225-245`；`pipeline/voc_analytics/stages/value_gate.py:36-46`。
- **具体失败场景**：`content='\t'` 时 Python `strip()` 判空，PostgreSQL 默认 `btrim()` 只裁普通空格而判有文本，最终进入 G4 后单消息失败；`brands=[NULL]` 在 SQL/Python 都被忽略，与“所有元素均可证明在白名单”不一致。
- **为什么现有测试没抓到**：没有 tab/newline-only、SQL NULL 数组元素或 `content_zh` 统一 helper 测试。
- **建议修法**：定义一个权威 normalization 规格并在 SQL/Python 共享；未知数组元素显式终态，不用三值逻辑真空通过。

### S03. 修复未被 pytest 收集的 fail-stop 测试，并增加 026 行为测试而非字符串测试

- **文件:行号**：`pipeline/tests/test_llm_fail_stop.py:374-405`；`pipeline/tests/test_gate_migrations.py:1-53`。
- **具体失败场景**：`test_parallel_map_only_trips_global_circuit_on_fatal` 位于 `if __name__ == '__main__'` 且在 `unittest.main()` 之后，pytest 导入时不定义，脚本执行通常也到不了；未来普通 LLMError 错误触发全局熔断仍可能测试全绿。026 则完全没测试。
- **为什么现有测试没抓到**：测试收集本身没有数量断言；迁移测试只覆盖到 025。
- **建议修法**：把函数移回测试类/模块顶层，CI 固定 collection 数；为 026 增加数据库验收 fixture：同值去重、B+A 互斥、同消息多 seq、同线程多评论、inherited-only UI、迁移即时 refresh。

### S04. 清理仍宣称 channel 存在的活文档，避免回滚/运维按旧身份域操作

- **文件:行号**：`pipeline/README.md:126`；`system/README.md:5`；`pipeline/docs/rework_plan.html:180`；`system/docs/voc_model_spec.html:99,114,458`；`pipeline/docs/运维交接.md:5`。
- **具体失败场景**：故障处理中运维按文档认为新品 identity 仍含 channel、存量不重键，实际 v3 已删除该维度并计划清层，错误判断某个新旧 ID 是否应合并或恢复。
- **为什么现有测试没抓到**：runtime 测试只要求代码/查询不含 channel，不校验运维文档与当前 schema/identity version。
- **建议修法**：发布前同步架构、模型规格、运维与回滚文档，明确 v3 preimage、alias、active generation 和 024 前后版本矩阵。

## 已验证无误

以下结论均为我实际逐文件静态核对的结果；未用它们抵消上述阻断项。

1. **G5 2×2 的纯函数主表正确。** `pipeline/voc_analytics/routing.py:88-107` 与 `classification.py:17-30,67-72` 实现了：诉求+SPU→老品、诉求+无SPU→新品、缺陷+SPU→老品、缺陷+无SPU→“缺陷不可归属”；`pipeline/tests/test_lifecycle_routing.py:33-72` 覆盖四格及 inherited SPU。缺口在“继承是否语义上属于该 claim”，不是四格 if/else 本身。

2. **在已经进入 candidates、参数非 NULL、字段规范的前提下，G1→G2→G3 的四个 SQL 分区互斥且完备。** `db.py:273-290` 中 G1 是布尔 `NOT EXISTS`、G2 对数组 `COALESCE`、G3 显式处理 NULL author，因此候选内部不会因 SQL UNKNOWN 漏到四个 filter 之外。问题在分母之前以及 G1 的业务语义。

3. **G2 的结构实现与声明顺序一致。** `db.py:238-241` 同时要求至少命中一个 keep 且不与 drop 相交，drop 一票否决；纯函数 `db.py:203-207` 对规范数组等价。未发现 G2 被 G3/G4 越过的代码路径。

4. **026 对它声称修的两个活定义，修改位置是对的。** 历史覆盖链是 015 覆盖 009 的 `voc_spu_issue`、018 覆盖 009 的 refresh；026 同时重建当前 issue 与 refresh，并在两处引入事实/继承。它保留了 old/确定过滤、`HAVING >=2`、018 的 scope 阈值。问题是计数单位、不变式、即时 refresh 和下游消费者没有同步。

5. **026 对“同一个 SPU 值机械重复”去重有效。** `SELECT DISTINCT (opp_id,message_id,seq,spu)` 会消除数组内重复、事实 A+继承 A、仅首尾空格不同的 A；NULL/空白 token 被过滤。它不能防事实 B+陈旧继承 A，也不能把同线程的不同消息折为一个独立归属。

6. **025 的正常电商聚合没有被 inherited 放大。** `pipeline/sql/025_spu_scope.sql:20-98` 的 base_message 明确只取 `src_line='电商'` 且只 `unnest(m.spu)`，与 009 的电商消息数、星级、正负证据口径一致；social-only 仅扩容器。电商回归缺陷位于生成池遗漏 low_conf，以及全流程没有电商守恒。

7. **024 对仓库内三张直接视图的依赖拆建顺序正确。** `024:7-9` 先 safety/inbox 后 board，`:13-72` 以当前列重建并恢复 grants；对 `pipeline/` 与 `system/` 的 runtime 搜索未发现当前代码仍读取 opportunity.channel。历史迁移和多份文档仍包含 channel，且在禁止连库的边界下无法证明生产外部 BI 无依赖，所以不能据此直接执行 024。

8. **v2 版本号本身已生效。** `prompts.py:8-9` 使用 `social-value-gate-v2`，`db.py:125-136` 会让 v1 cache miss；本次风险是同 v2 下输入变化仍命中。

9. **正常、单进程、稳定事实下，023 回填函数不会回写事实 SPU。** 它只更新 `spu_inherited`，单一事实并集时给空事实行继承，无组/冲突/已有事实时清继承；函数一次成功执行后的稳定快照语义正确。B02 指出的是三事务、并发与缺少 DB 约束破坏这个前提。

10. **G4 的基础校验与 2:1 计票本身正确。** live 新票未知 cls、普通非数字和越界 confidence 会失败；三票有 2:1 时按多数类返回，阈值使用严格 `<0.8`。未验证的是 confidence 语义、票间独立性和 1:1:1 处理。

11. **对已经进入对账分母的 G4/G5 行，终态算术能发现少计。** `pipeline.py:519-535` 会要求 G4 无价值、泛化、缺陷不可归属、老品、新品、失败之和等于 G4 输入，且失败必须为 0。它无法发现分母前丢行、全零或电商缺失，也允许部分 build group 失败。

12. **仓库没有可复现“v2 误放约 3.1%、漏拦 0/30”的脱敏 gold set、逐样本标签或混淆矩阵。** 我逐读 `pipeline/tests/` 后确认，现有 G4 测试主要是 mock plumbing，不能独立重算该结论。因此本评审不否定这组线下数字，但也不能把它当作已经被回归测试锁住的上线证据。

## 上线前最小硬门

只有下面条件全部满足，才应重新讨论 025/026/024：

1. 事实层以 staging+manifest 完成权威替换；26 周两来源、五字段、根闭包、零片、stale seq、absent IDs 均对账。
2. DB 证明事实/继承互斥、非社媒无继承、冲突组无继承；线程键已命名空间化或以生产数据+持续约束证明全局唯一。
3. G1 mixed/unknown 政策与 v2 第三方配件例外达成一份可执行、端到端测试的统一口径。
4. G4 以 input hash+decision version 重新物化一次，两个生命周期只读同一冻结快照。
5. 老品最小独立证据数由代码硬校验；产品页能够展示未成卡信号。
6. 026 改为独立消息/线程计数，所有六个消费者统一 effective SPU，并在迁移事务内 refresh+self-check。
7. 电商池与重构前做 exact diff，恢复 low_conf 过滤并有独立守恒账。
8. 影子代完整生成、finalize、页面 smoke、人工历史/alias 迁移、ID churn 报告和失败回滚演练通过；线上可见代在此之前保持不变。
9. 024 最后执行；删除前保存 channel preimage、生产依赖清单和可演练恢复物。

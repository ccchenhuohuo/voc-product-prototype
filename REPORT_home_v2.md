# 首页 v2 重构自查报告

## 结论与测试基线

本次已把首页原六区块整体替换为四模块，并删除视觉稿早期的「管道运行」甘特形态。首页请求路径只读 PostgreSQL；云听总量由离线探针写入缓存，页面不会调用云听或 LLM。

- 修改前 system 基线：127 passed。
- 修改后 system：144 passed；唯一提示是现有 `fastapi.testclient` 的 StarletteDeprecationWarning，不影响结果。
- 修改前 pipeline 基线：167 passed、1 xfailed、19 subtests。
- 修改后 pipeline：167 passed、1 xfailed、19 subtests，原样保持。
- 本次没有连接数据库、执行 SQL、运行探针、调用云听/LLM、部署或执行 git 操作。

## 四模块数据来源

### 01 源数据三管道

- `HOME2_SOURCES`：按 `voc_message.src_line` 统计电商、社媒实际接入量；客服通过来源定义表 `LEFT JOIN` 得到真实的 0。
- 同一查询按 `query_task_type` 读取 `voc_source_probe` 的 `matched_count`、窗口和探测时间。缓存缺行时 `available_count` 保持 `NULL`，viewmodel 显示「未探测」，不转成 0。
- `pipeline/sql/022_source_probe_cache.sql` 创建缓存表并授予 `voc_writer` 写权限、`voc_human`/`voc_reader` 读权限，附权限自检。
- `pipeline/scripts/probe_source_totals.py` 对 COMMENT、SOCIAL、SERVICE 各创建一次 `total=1` 任务，仅取任务元数据中的 `matched_count` 后 upsert。默认窗口以库内最新发布时间所在周为末周向前覆盖 26 个 ISO 周，并以执行时点为窗口终点；可用 `--start`/`--end` 覆盖。

### 02 数据流转

- `HOME2_FLOW_SOCIAL`：先用有序 `CASE` 按「用户咨询 > 用户使用体验 > 产品评测 > 竞品拉踩 > 其他 > 二手转让 > 产品种草广告」唯一归因；没有命中规则的消息归入「无标签」。标签定义的输出顺序独立按视觉稿排列，不改变归因优先级。
- `HOME2_FLOW_EC`：先 `DISTINCT ON (message_id) ... ORDER BY message_id, seq` 取每条消息最小 `seq` 证据的 `tag`；没有证据或空标签归入「无标签」。非空主标签按消息量动态取 Top 6，其余动态合并成「其余 N 类」。模块说明中的标签体系总类数来自电商全部证据的非空 `tag` 去重数；「其余 N 类」则来自主标签归因后的剩余类数，两个口径刻意分开。
- 两条查询都先把 `voc_opp_evidence` 去重成 `message_id × opp_id`，再按消息 `bool_or` 压成是否进入老品/新品，避免一条消息的多证据、多同生命周期机会点重复计流。
- 第一段总量条与第二段桑基共用这两份消息级结果；第二段只保留 `iter + inno > 0` 的标签流，不混入未进入部分。

纯内存验收桩复现任务书核对数并断言：社媒标签和等于 24,519，电商标签和等于 26,407；老品 3,802、新品 2,386、进入合计 6,188；电商到新品为 0；桑基标签流量和仍为 6,188。业务快照只存在测试桩中，未进入 Python 运行时代码、模板或 JS。

### 03 状态分布

- `HOME2_STATUS` 的老品分支直接锚定 `voc_spu_issue`，左连 `voc_spu_issue_manual`。
- 新品分支直接锚定 `voc_opportunity WHERE opp_type='新品创新'`，左连 `voc_opportunity_manual`。
- 两个分支都使用 `COALESCE(m.status, '考虑中')`，与既有业务页默认口径一致；五个状态通过定义表补齐 0 行。
- 同一查询的机器态分支对全库 `voc_opportunity` 分别统计 `NOT backlog`、`needs_review`、`safety_flag`、`backlog`。这些指标与人工五状态正交，不互相扣减。

### 04 数据新鲜度与分布

- `HOME2_FRESHNESS`：读取最新发布时间、全库覆盖 ISO 周数、`voc_spu` 数、SPU 问题卡数、有问题卡的 SPU 数；同时检查 `voc_spu_issue` 的机会点引用与 `voc_opp_nn` 两端引用，把两类缓存行合成悬空分子和分母。占比超过 5% 时 viewmodel 才输出告警条。
- 同一查询读取最近一次带 `metrics.reconciliation` 的 `voc_run_log`，动态提供抬头的 run/week、账本闭合布尔与账本字段数；没有记录时明确显示未记录，不伪造视觉稿快照。
- `HOME2_WEEKLY`：以库内最新发布时间所在周为末周，`generate_series` 补齐连续 26 个 ISO 周，按社媒/电商分别计数。断周数由 viewmodel 根据查询行计算。
- `HOME2_DIST`：接受白名单校验后的源和维度参数，归一空语种/平台为「未标注」，动态取 Top 8 +「其余」。电商×语种与客服任意维度由 viewmodel 输出任务书指定空态，不画误导性的空环或 100%「未标注」环。

## 几何与交互

- 桑基沿用视觉稿的 `980×430` viewBox、三列 x 坐标 `0/232/782`、标签流出 x=470、节点宽 13、PAD=14、来源间距 26、标签间距 9、标签最小行距 `LBL=14`。
- 所有桑基节点、带路径 `d`、标签 y、引线路径，周柱 x/y/宽高，以及环形图累计角度与 arc `d` 都在 `normalize_home_v2` / `normalize_home_dist` 中算完。模板只输出字段。
- 标签按原中点顺序下推；偏离原位超过 2px 时输出引线。测试覆盖最小间距、三列 viewBox 边界和引线触发。
- 主动修正一处视觉稿几何问题：原稿对极细标签节点直接 `max(flow_height, 2)`，并把放大后的高度同时用于流带，导致来源列凭空增加几像素流量。本实现仍给节点 2px 可见高度，但流带保持线性 `value × scale`。因此来源带、标签流量、生命周期带三者高度和严格相等。视觉形态不变，守恒可测试。
- 总量条、桑基带、状态带、周柱使用同一委托式 `.tip`；桑基悬停压低其他带，环形图例悬停压低其他弧段。tooltip 内容在写入 `innerHTML` 前转义，仅放行 `<br>`/`<b>` 标记。
- 仓库原 `htmx.min.js` 是只支持 `hx-post` 的本地兼容层。为让任务书要求的 `hx-get` 真正工作，本次在该既有 runtime 中补了 `hx-get` 点击和 `htmx:configRequest` 参数钩子；业务 JS 没有自写 fetch。非法筛选统一回落社媒×语种。
- `prefers-reduced-motion` 继续由全局 CSS 关闭 transition/animation。视觉稿的阴影用 token 化 `drop-shadow` 复现，以保留现有前端契约中不使用 `box-shadow` 的约束。

## 删除与主动调整

- 删除 `HOME_EVIDENCE_FUNNEL`、`HOME_EVIDENCE_PER_OPP`、`HOME_SIMILARITY`、`HOME_ISSUE_STATUS`、`HOME_COVERAGE`、`HOME_FRESHNESS`。
- 删除 `normalize_home_dashboard`、六个旧首页 normalizer、旧首页专用辅助函数和机会点 URL 辅助。
- 删除模板中的旧六区块和最近运行表，没有实现视觉稿早期的「05 管道运行」。
- 删除不再被模板引用的 `.home-section/.home-chart/.home-fill/.home-panel/...` 旧 CSS；新增样式仍在 `app.css`，没有新建 CSS/JS 文件或引入库。
- `_tokens.css` 的裸 `:root`、系统暗色块、显式暗色块都补齐 `--iter/--inno/--idle/--s1..--s5`，并同时补齐页面实际使用的 wash/shadow token。
- 更新 `system/scripts/check_sql.py` 的查询注册表，使七条 HOME2 查询及两个分布参数继续受占位符数量测试约束。
- `tests/test_home_dashboard.py` 已从旧六区块 viewmodel 测试改成 HOME2 SQL、迁移、探针、token、旧代码删除与本地 htmx runtime 契约测试；新增 `tests/test_home_v2.py` 覆盖任务书列出的守恒、状态、几何、空态与路由组合。

## 尚存风险与验收建议

1. 按硬性边界，本次没有对真库执行迁移、SQL、`EXPLAIN` 或查询；SQL 的列名、类型与执行计划仍需验收环境执行 022 后用 `system/scripts/check_sql.py` 核对。尤其两条消息级关系 CTE 需要观察真库计划是否在请求预算内。
2. 首页在部署新代码前必须先执行 022；若表尚不存在，`HOME2_SOURCES` 会按 PostgreSQL 正常行为报表不存在。迁移已执行但表为空时则按要求显示「未探测」。
3. 探针为了同时满足“每源一次 `total=1`”和“不下载文件”，调用了 `yunting._create_task/_await_task` 内部原语，并复用了 `export_slice` 的 `meta['matched']` 字段契约；没有修改禁止改动的 `yunting.py`。若这些内部函数未来改名，探针需同步。验收时应先显式传窗口跑一次，确认 SERVICE 无过滤是否正是期望口径。
4. 流转查询允许同一消息分别在老品和新品布尔位上为真，以便暴露分类/挂靠异常，不会静默“修正”数据库。当前契约和测试基准要求两者互斥；若真库守恒不闭合，应检查关系数据或生命周期分类，不应在 viewmodel 截断。
5. 悬空占比的分母定义为 `voc_spu_issue` 行数加 `voc_opp_nn` 缓存行数；两处各有一端失效时按“一条缓存行”计一次。任务书没有进一步规定两类缓存的加权方式，这是当前最直接、可解释的合并口径，验收方若有既定账本分母需再对齐。
6. 视觉 QA 目前基于服务端渲染测试和几何边界测试，未连接真库打开浏览器核对长标签、极端 Top 8 文案与实际侧栏宽度。建议验收连真库后分别检查浅色、深色、跟随系统，以及窄屏下桑基横向滚动和六种分布组合。

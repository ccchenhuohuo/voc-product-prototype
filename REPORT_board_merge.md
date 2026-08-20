# 看板合并与 channel 退场适配报告

## 结论

- `/iter` 已成为唯一 SPU 列表页：同一实体全集、同一套筛选、同一套排序、同一张模板。
- `/search` 不再渲染页面，固定以 HTTP 301 跳到 `/iter?filter=all`，并保留可映射的旧查询参数。
- 列表与 SPU 详情已接入 `voc_spu.has_ec`、`voc_message.spu_inherited` 和 `voc_social_gate`。
- `app/queries.py`、新品创新路由/模板、首页运行时模板与查询均无 `channel` 引用。
- 导航已变为四项，老品迭代显示“产品 N · 问题跟踪 M”。
- 未改 `pipeline/**`、`app/auth/**`、`tests/test_auth.py`，未部署、未重启、未连库、未执行 SQL、未调用 LLM，也未做 git 操作。

## 产品检索能力合并映射

| 原 `/search` 能力 | 合并后的落点 | 说明 |
|---|---|---|
| 关键词检索 | `/iter` 顶部搜索框；`app/routes/board.py` | 继续覆盖 SPU、产品名、SKU；保留输入法合成期间不误提交的现有前端行为。 |
| 品类 facet | `/iter` 标签筛选模块第一行 | 仍显示全量 SPU 品类计数；与三级树放在同一筛选面板。 |
| 体验域/子域/叶子三级树 | `/iter` 标签筛选模块 | 保留逐级校验、父级联动、折叠展开和路径清除；关联同时识别 `spu` 与 `spu_inherited`。 |
| 五列服务器排序 | `/iter` 表头 | `SORT_KEYS` 与参数校验集中到 `app/routes/spu_table.py`，不再有 board/search 两份实现。 |
| 全部 SPU 视野 | `/iter?filter=all` | 包含无问题卡 SPU 和社媒-only SPU；也是 `/search` 重定向的默认目标。 |
| 无待办产品仍可见 | `/iter?filter=all` 的“无信号”行 | `group_board` 不再丢弃没有问题条目的 SPU。 |
| `/search` 入口 | `app/routes/search.py` 的 301 兼容入口 | 仅映射 `q/category/domain/sub/leaf/sort/dir`，丢弃未知参数，并显式补 `filter=all`。旧产品检索模板已删除。 |
| 导航入口 | 并入“老品迭代” | “产品检索”导航项已删除。 |

## `/iter` 新口径

- 状态预设：无参数或非法值均为 `tracked`（有跟踪中问题）；另有 `revived`（曾复活）和 `all`（全部）。
- `BOARD_SPUS*` 以 `voc_spu` 为实体全集，状态仅作为同一 SQL 的过滤参数；标签层级只决定是否追加对应的 `EXISTS` 条件。
- 筛选元数据随主 SPU 查询以 JSON 返回。默认页仍只做一次 SPU 查询和一次问题查询，避免为筛选器额外增加数据库往返，也保持认证测试既有数据库桩不变。
- `has_ec=false` 的行显示“仅社媒信号”；类目、定级、负面占比、负面证据、均星等电商指标统一显示“—”，不会伪装成 0 或空字符串。
- 问题层继续来自 `voc_spu_issue`；原声层计数来自 social gate；两者都没有时显示“无信号”。

## 原声区查询口径

列表计数和详情完整列表均满足：

1. 目标 SPU 命中 `voc_message.spu` 或 `voc_message.spu_inherited`；
2. `voc_social_gate.cls IN ('诉求缺口', '产品缺陷')`；
3. `NOT EXISTS (SELECT 1 FROM voc_opp_evidence WHERE message_id = ...)`。

列表仅显示“原声 N”徽标；详情页展示全部命中项。诉求缺口优先展示 `claim`，产品缺陷展示截断内容，并显示分类徽标、平台和时间。

## channel 引用清除清单

| 范围 | 处理 |
|---|---|
| `BOARD_INNOVATIONS` | 删除 `o.channel` 投影；列表按 `core_tag` 表达预聚类簇，并按 `evi_total`、`rank_score`、互动数组织。 |
| `INNOVATION_DETAIL` | 删除 `o.channel` 投影。 |
| `app/queries.py` 其余查询 | 全文复核，无 channel SELECT/WHERE/GROUP BY 残留。 |
| `innovation.py` 与新品模板 | 当前基线已无 channel 筛选/徽标/分组；继续保持单一需求缺口池。将“对标品牌”改成事实含义更准确的“涉及品牌”。 |
| 首页查询 | 当前数据流查询已按 `opp_type` 分到老品迭代/新品创新，不依赖 channel；补充注释和页面口径，明确第一段保留标签池/未进入原料，进入后仅按机会点类型分流。 |
| 运行时 UI | `app/templates` 内无“竞品对标”“产品体验”或 channel 文案。 |
| 导航与计数 | `SHELL_COUNTS` 删除 `search`，新增 `products`；`iter` 保留为有现存问题卡的 SPU 数。 |

说明：`system/README.md` 和 `system/docs/voc_model_spec.html` 是历史模型说明，不属于运行时 UI 或查询字符串，本任务未重写；其中仍可能描述旧 schema，建议由管道重构任务统一更新，避免两个并行任务互相覆盖文档口径。

## 主动改动与理由

- 保留轻量 `search.py`：任务要求 `/search` 301，同时硬性禁止修改仍导入该模块的 `test_auth.py`；因此删除旧页面逻辑和模板，但保留兼容重定向 router。
- 新增 `spu_table.py`：把排序白名单和参数校验集中到唯一来源。
- 调整 `group_board`：合并页必须展示无问题 SPU，故为其补空 `issues` 和空 `top_issue`，不再提前过滤。
- 将品类/三级标签元数据并入 `BOARD_SPUS*`：减少默认页查询次数，并让筛选、实体结果共享同一关键词/品类口径。
- 给 `scripts/check_sql.py` 增加 `--static`：在禁止连库的开发约束下仍能检查公共 SQL 是否全部登记、参数占位符数量是否一致；该模式明确不替代真库 `EXPLAIN`。

## 废弃测试的等价改写

| 原测试/断言 | 改写后的等价契约 |
|---|---|
| `test_product_search.py`：`/search` 直接渲染、关键词参数、品类参数、无待办 SPU | 改为断言 `/search` 301 参数映射；关键词/品类进入 `BOARD_SPUS`；默认 tracked 与 `filter=all` 数量差；社媒-only 和无信号行；四项导航双数。 |
| 双页面排序参数化 `('/iter', '/search')` | 改为唯一 `/iter` 页面五列升/降序与三态循环；另加单一 `SORT_KEYS`/`sort_state` 来源契约。 |
| 旧队列“全部/待复议/我负责的”页签 | 改为“有跟踪中问题（默认）/曾复活/全部”三条真实链接，不再保留无功能占位。 |
| `BOARD_SPUS_REVIVED` 独立 SQL | 改为同一 `BOARD_SPUS` 的 `status_filter='revived'` 参数，并断言与排序参数互相保留。 |
| `/search` 上的三级标签树、折叠、品类首行、查询选择 | 全部迁到 `/iter` 断言；按 domain/sub/leaf 选择 `BOARD_SPUS_BY_*`，无标签时不发第二次 SPU 查询。 |
| `/search` 与 `/iter` 的复活高亮 | 改为唯一 `/iter` 的 `has_revived_issue` 高亮契约。 |
| route smoke 中 `/search == 200 + 产品检索` | 改为 `301 + /iter?filter=all`。 |
| `group_board` 必须排除无问题 SPU | 反向改为保留空问题 SPU，并验证空问题/空头号问题。 |
| 查询指标测试中的 `SEARCH_SPUS*`、`SEARCH_TAG_FACETS`、`BOARD_SPUS_REVIVED` | 改为合并后的 `BOARD_SPUS*`、内嵌标签元数据和统一状态参数；同步更新公共 SQL 注册表。 |
| SPU 详情桩只返回问题列表 | 增加 `SPU_RAW_VOICES` 桩与详情原声渲染断言。 |
| 侧栏桩返回 `search` | 非认证测试改为返回 `products`；`test_auth.py` 按硬性边界完全未动，模板对缺失新键安全回退为 0。 |

## 验证结果

- 基线（改动前）：`cd system && python3 -m pytest tests/` → **144 passed**。
- 最终：`cd system && python3 -m pytest tests/` → **144 passed, 1 warning**。
- SQL 静态卫生：`cd system && python3 scripts/check_sql.py --static` → **27 / 27 通过，0 失败，0 未验**。
- 残留审计：`app/queries.py`、`app/routes`、`app/templates` 内无 `channel`；运行时模板内无“竞品对标/产品体验”；无 `SEARCH_SPUS*`、`BOARD_SPUS_REVIVED`、`product-search.html` 残留。

唯一 warning 是环境已有的 Starlette `TestClient`/`httpx` 弃用提示，与本任务无关。

## 风险与验收方待做

1. 按硬性边界没有连接 PostgreSQL，也没有执行 `scripts/check_sql.py` 的真库 `EXPLAIN` 模式。部署验收方应在目标 schema 就绪后运行 `python3 scripts/check_sql.py`，重点检查内嵌 facet JSON、`spu || spu_inherited` 展开和 social gate 反连接的计划。
2. `BOARD_SPUS*` 为减少往返，把筛选元数据和原声计数并入主查询；数据量扩大后应关注 `voc_opp_evidence(message_id)`、`voc_social_gate(message_id, cls)` 以及消息 SPU 数组展开的成本。
3. “未被机会点引用”按任务书给出的严格 `NOT EXISTS voc_opp_evidence` 实现；即使证据指向后续失效/合并机会点，也仍视为已经引用。如业务希望只排除“有效机会点”，需另行明确并增加 opportunity 连接条件。
4. 目标 schema 的 `has_ec` 被视为非空 boolean。viewmodel 仅为旧测试桩/缺键提供 `True` 回退，生产语义仍以视图字段为准。
5. 历史 README/模型规格仍含旧 channel 说明；本次为避免与并行管道重构冲突没有改动，需由 schema 负责人统一收口。

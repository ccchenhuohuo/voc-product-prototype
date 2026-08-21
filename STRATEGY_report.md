# 战略视图交付报告

日期：2026-08-21

## 1. 12 项交付物对照

1. `pipeline/sql/034_strategy_axis.sql`：新增轴、成员、判定缓存三表，约束分区不变量，DDL/索引/约束可重跑，并补齐 `voc_writer`、`voc_human`、`voc_reader` 授权。
2. `pipeline/voc_analytics/strategy.py`：实现代次冻结、pgvector top-K 召回、MAX_PAIRS 保险丝、贪心 leader 惰性判定、策略版本缓存、命名回退、聚合指标与分区事务替换。
3. `pipeline/voc_analytics/prompts.py`：新增 `STRATEGY_AXIS_SAME`、`STRATEGY_THEME_SAME`、`STRATEGY_AXIS_NAME` 三个模板，并写入任务书要求的误合反例与红线。
4. `pipeline/voc_analytics/config.py`：新增 `STRATEGY_GENERATION` 及 cosine、TOPK、MAX_PAIRS、准入、top SPU 六项冻结参数。
5. `pipeline/scripts/run_strategy.py`：新增独立 CLI，支持 `--type {通病,诉求,all}`、`--generation`、`--dry-run`、`--run-id`，路径按脚本位置解析。
6. `pipeline/scripts/run_generate.py`：在两条 `_finalize()` 成功路径后挂战略重算，新增 `--skip-strategy`；战略失败写独立台账且不改变主管线退出码。
7. `system/app/queries.py`：新增 `STRATEGY_AXES`、`STRATEGY_AXIS`、`STRATEGY_AXIS_MEMBERS`，首页/侧栏 strategy 计数改读 `voc_strategy_axis`，删除 `STRATEGY_OPPORTUNITIES`。
8. `system/app/routes/strategy.py`、`system/app/routes/home.py`：重写战略总览/详情路由，当前代次只读；首页预取新 strategy 计数。
9. `system/app/templates/strategy.html`、`system/app/templates/strategy_axis.html`：实现双 tab、通病套叠扩散条、诉求声量条、轴详情、SPU 分布与逐成员状态；未加入固定描述性散文。
10. `pipeline/tests/test_strategy_axis.py`、`system/tests/test_routes_smoke.py`：落实 15 条冻结测试及系统 smoke；smoke 实际请求 `/issue/{spu}/{opp_id}` 与 `/inno/{opp_id}` 并断言非 404。同步更新 `system/tests/test_query_metrics.py` 与 `pipeline/tests/test_manual_chain_contract.py` 的替换/挂钩契约。
11. `pipeline/docs/运维交接.md`：新增战略层运行、代次同步、缓存失效、台账等式与 MAX_PAIRS 处置说明。
12. `STRATEGY_report.md`：本报告，包含逐项对照、主动改动、测试映射与离线验收结果。

配套更新：`system/scripts/check_sql.py` 已用三个新查询替换已退役查询的静态参数登记，避免离线 SQL 体检出现陈旧映射。

## 2. 主动改动、实现选择与原因

### 按任务书主动改动

- 将战略轴从旧 `scope/n_eff` 机会点读路彻底切到独立派生表，原因是战略视图已冻结为公司级聚合层，旧 scope 口径整体退役。
- 在 `all` 模式先召回两类并合计检查 MAX_PAIRS，再开始任何判定缓存写入，原因是保险丝中止必须不改变缓存或轴分区。
- 保留 Stage1 母本的惰性 leader 判定，台账分别记录召回对与实际评估对，原因是冻结决议 12 禁止预判全部召回对。
- 判定成功结果按类型批量 `INSERT ... ON CONFLICT DO NOTHING`，原因是保持缓存只需 `SELECT, INSERT` 权限，同时避免每对新建一次数据库连接。
- 在战略重算前核对 `voc_spu_issue` 的 v2/v3 实际指向，原因是代次不一致时必须失败，不能用全零老品权重清空正确分区。
- 主管线 LLM 用量在战略钩子前冻结，战略用量写独立台账，原因是同进程调用不能把战略成本重复计入 generate/finalize。
- 路由 smoke 跟随模板生成的真实成员链接发请求，原因是字符串断言无法发现 `/issue`、`/inno` 前缀错误。

### 任务书未写死的最小实现选择

- LLM 返回的 `summary` 超过 60 字时落 `NULL`，不截断半句话；轴名仍按任务书重试/回退。
- 首页当前结构只有单一战略导航读数，因此实现总轴数，不增加任务书所说的可选“两类副行”。
- 套叠条复用现有 CSS token 与表格类，在战略模板内做最小内联几何编码；没有扩展全局样式系统。
- `dry-run` 读取既有缓存但不写判定缓存、轴表或 run log，仍完成命名以打印预计轴列表。

### 偏离任务书或冻结决议

无。14 条冻结决议均按原文实现；迁移 030–033、035、G1–G5、Stage1–4、路由 2×2 表与归属契约语义均未改动。

## 3. 15 条测试与实际函数名

均位于 `pipeline/tests/test_strategy_axis.py`：

1. 贪心确定性：`test_01_greedy_is_deterministic_and_axis_id_is_stable`
2. 非传递性：`test_02_greedy_leader_is_non_transitive`
3. 缺陷/诉求红线：`test_03_defect_and_enhancement_redline_stays_split`
4. 判定缓存：`test_04_second_run_uses_lazy_verdict_cache_only_for_evaluated_pairs`
5. mode 变更使缓存失效：`test_05_problem_mode_change_invalidates_related_cache_key`
6. MAX_PAIRS 超限：`test_06_max_pairs_aborts_without_any_table_write_and_cli_is_nonzero`
7. 准入：`test_07_singletons_and_one_spu_problem_clusters_are_not_admitted`
8. n_eff 90/10 = 1.22：`test_08_n_eff_uses_weighted_90_10_distribution_and_equals_1_22`
9. 事务性：`test_09_failure_inside_second_partition_insert_rolls_back_old_rows_exactly`
10. 命名回退：`test_10_generic_name_twice_falls_back_to_leader_problem_mode`
11. 代次隔离：`test_11_generation_filter_excludes_shadow_generation_members`
12. 分区替换：`test_12_single_type_replacement_preserves_other_partition_row_for_row`
13. 判定失败不落缓存：`test_13_judge_failure_is_not_cached_as_false_verdict`
14. 策略版本失效：`test_14_prompt_change_changes_judge_policy_and_misses_old_cache`
15. 授权契约：`test_15_strategy_migration_grants_all_three_roles_without_cache_read_leak`

系统侧补充 smoke：`test_strategy_two_tabs_axis_detail_404_and_empty_state`、`test_strategy_member_links_are_followed_and_resolve_non_404`、`test_home_strategy_count_reads_derived_axis_table`。主管线非阻断挂钩另由 `test_strategy_hook_runs_after_finalize_and_failure_is_non_blocking` 覆盖。

## 4. 离线验收结果

- Pipeline：`pytest -o addopts='' -q` → **261 passed, 1 xfailed, 23 subtests passed**。
- System：`pytest -o addopts='' -q` → **160 passed**；仅有既存 Starlette/httpx 弃用警告。
- Python 语法：对本次新增/修改的管线、脚本、系统路由、查询与测试文件执行 `python -m py_compile` → **通过**。
- SQL 静态登记：`python scripts/check_sql.py --static` → **通过 59 / 失败 0 / 未验 0**。
- 所有战略测试均使用 fake/monkeypatch；验收过程未连接数据库、未执行 SQL、未联网、未调用真实 LLM/embedding。

## 5. 离线无法验证

- `034_strategy_axis.sql` 在真实 PostgreSQL 上的首次应用、二次重跑、三角色 ACL 与约束行为；按边界仅做文本契约和静态检查，迁移未执行。
- 生产 `voc_spu_issue` 当前究竟指向 v2 还是 v3，以及真实数据上的全轴 `n_spu/n_eff/evi_total` 零差异对账。
- pgvector 在生产数据量与索引统计下的查询计划、实际召回对数、延迟和 MAX_PAIRS 余量。
- 真实模型对轴同义性、红线反例和命名质量的表现，以及真实 token/费用台账。
- 真实数据库事务故障注入、主管线 finalize 后的实际台账行与线上页面视觉效果；离线已分别用内存回滚测试、FastAPI smoke 与模板渲染覆盖代码契约。

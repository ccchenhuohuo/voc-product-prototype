# 路线 B 退役与手工链补齐报告

日期：2026-08-19

## 结论

Dagster 运行路线已从现行代码、依赖、数据库初始化和 smoke 入口中退役；历史部署脚本保留在 `pipeline/scripts/retired/`，首行 `exit 1` 阻断执行。手工生成链已接入生成前提案执行钩子和收尾 REVIVE 检测钩子，两者均打印结果并写入 `voc_run_log.metrics.finalize`。

本次只改仓库文件；未 SSH、未连数据库、未执行 SQL、未调用 LLM/向量接口、未部署、未重启服务、未执行迁移，也未使用 git。

## 接入点与顺序

| 能力 | 接入点 | 账本与输出 |
|---|---|---|
| PM 提案落地 | `pipeline/scripts/run_generate.py:66` 定义 `_execute_proposals()`；`:70` 仅在 `VOC_AUTO_MERGE=1` 时调用 `execute.run_auto()`；`:73` 无条件调用 `execute.run()` | `:76` 写 `finalize.proposal_execution`；`:77` 打印统计 |
| 生成前顺序 | `pipeline/scripts/run_generate.py:227` 调提案钩子，`:228` 才调用 `pipeline.generate_opportunities()` | 合并先改变去重版图，新证据随后生成/挂靠 |
| REVIVE 检测 | `pipeline/scripts/run_generate.py:81` 定义 `_check_revive()`；`:150` 在快照 SQL 完成后调用 | `:84` 写 `finalize.revive_proposals`；`:85` 打印新增数；`:167` 写完成态账本；`:170` 加入收尾返回值 |
| 放行顺序 | `pipeline/scripts/run_generate.py:150` REVIVE，`:154` SPU 刷新，`:158` 最近邻刷新/悬空校验，`:162` PM 放行 | REVIVE 明确位于快照后、放行前 |
| 全量入口 | `pipeline/scripts/rerun_both.sh:287` 清库，`:306` 说明钩子语义，`:311`/`:315` 通过 `run_generate.py` 生成，`:393` 通过 `--finalize-only` 收尾 | 提案钩子只会出现在清库之后；REVIVE 由阶段二覆盖 |

`VOC_AUTO_MERGE` 未设置或非 `1` 时不会调用 `run_auto()`，默认关闭语义未改变。

## 删除与归档

- 删除 `pipeline/voc_analytics/definitions.py`，资产图、job、schedule 和 asset checks 不再可加载。
- `pipeline/scripts/m5_deploy_dagster.sh` 移至 `pipeline/scripts/retired/m5_deploy_dagster.sh:1`；首行 `exit 1` 先于 shebang 和所有复制/systemd 命令。
- `pipeline/pyproject.toml:4-9` 改为手工链描述，移除 `dagster`、`dagster-webserver`、`dagster-postgres`。
- `pipeline/scripts/m0_deploy_pg.sh:47` 起不再创建 `voc_dagster` 元数据库。
- `pipeline/scripts/smoke.py` 删除 `--dagster` 参数和资产物化分支，只保留手工脚本探针。
- 运行代码、测试与 SQL 现行注释中的调度器专属表述改为手工管线语义；退役脚本和运维历史段保留名称以保证审计可读。

## 文档修改

- `pipeline/README.md:3` 将手工链写为唯一生产入口；`:74-75` 更新代码结构；`:88-106` 保持迁移编号 `001-027`；`:138-150` 改为手工 runbook；`:172-181` 说明提案、REVIVE、账本和默认开关。
- `pipeline/docs/运维交接.md:14-16` 更新系统组成；`:50` 记录 m5 归档守卫；`:100-120` 改为手工跑批；`:179-187` 说明资产 checks 退役后的人工阻断/监控；`:320` 收口密钥副本风险；`:353-355` 更新验收清单。
- `system/README.md:5` 删除旧业务子类型列的现行 schema 陈述。
- `system/docs/voc_model_spec.html:99` 修正来源口径；`:114` 修正 OPP2 hash/L1；`:425` 改为手工收尾刷新派生层；`:458` 从 schema 移除旧业务子类型；迁移链说明更新到 `027`。
- 同步清理 `pipeline/docs/data_architecture.html`、`rework_plan.html`、`pipeline_redesign.html` 中同一旧字段的 hash、L1 和 schema 残留。

## 测试与静态验证

- 新增 `pipeline/tests/test_manual_chain_contract.py:42-129`：monkeypatch 记录默认/显式自动融合调用顺序、提案早于生成、REVIVE 位于快照后放行前、全量入口顺序和退役守卫。
- 因删除 `definitions.py`，没有现有 pytest 失效；`test_finalize_refreshes_spu_layer.py` 只把历史描述改为已退役调度路径，原等价断言继续覆盖手工收尾。
- `cd pipeline && python3 -m pytest tests/`：`215 passed, 1 xfailed, 19 subtests passed`。
- `cd system && python3 -m pytest tests/`：`152 passed`（1 条既有 Starlette/httpx 弃用警告）。
- `python3 -m py_compile`、`bash -n scripts/rerun_both.sh`、`bash -n scripts/m0_deploy_pg.sh` 已通过。

## 主动改动与理由

- 同步移除 `m0_deploy_pg.sh` 的元数据库创建，否则即使依赖和资产图已删，新库初始化仍会制造退役栈遗留。
- 删除 smoke 的调度分支，而非仅标注，因为定义模块和依赖已经退役，保留参数只会形成必然失败的入口。
- 运维文档把已删除的 blocking checks 明确标成不再自动执行，防止值班人员误以为仍有阈值阻断。
- 扩展清理三个 `pipeline/docs/*.html` 的旧业务子类型口径，避免同一仓库继续同时发布两套 hash/L1/schema 契约。

## 风险与验收方待做

1. `pipeline/voc_analytics/execute.py:109-113` 实际只统计和查询 `MERGE/SPLIT`；schema 虽允许 `REVIVE/REWRITE`（`pipeline/sql/001_schema.sql:152`），现执行器并不消费这两类 accepted 提案。此次按任务书接入了既有 `execute.run(week)`，但不能把它解读为四种 op_type 均已落地；REVIVE/REWRITE 的状态变更、权限和 lineage 契约需另行定稿。
2. `pipeline/voc_analytics/lifecycle.py:106-134` 实际检测的是状态为“不考虑”的墓碑，按快照基准 3 倍创建 REVIVE 提案；它不检查“已完成”、`release_date` 或上市后新反馈。因此此次接线恢复的是该函数现有语义，不是完成态上市后复活语义。
3. `rerun_both.sh:294` 会在全量清理中截断 `voc_proposal`/`voc_opp_lineage`。所以清库后的提案钩子位置满足顺序约束，但清理前已有 accepted 提案不会保留到该钩子；全量重建是否应备份/恢复人工裁决需验收方明确，不能直接移动钩子到清库前。
4. `rerun_both.sh` 启动两个生成进程，两者都会调用提案钩子；现有执行器用 proposal advisory lock 和 lineage 幂等检查防重复。需在受控环境验证并发统计符合预期。
5. 由于硬性边界禁止修改其他根目录文件，根 `README.md` 仍含现行 Dagster 入口/服务陈述；`AUDIT_full.md`、`REPORT_gates.md`、`REVIEW_voc_mcp_prd.md` 是历史审计材料，保留原文。根 README 需另行授权修正。
6. 验收方需在生产重跑结束后核对 `voc_run_log.metrics.finalize.proposal_execution`、`revive_proposals`、日志打印、accepted 提案 lineage、REVIVE pending 数和 PM 放行顺序；本次按禁令未触碰生产。

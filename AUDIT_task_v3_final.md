## 一、上轮 7 条阻断项的闭合判定

| 编号 | 已闭合 / 未闭合 | 一句话依据 |
|---|---|---|
| 1 | 已闭合 | 已新增单进程 `voc_prepare_assign_snapshot`、按同一 `RUN_ID` 在并行进程前调用、advisory lock、指纹记录和唯一快照读口（`pipeline/docs/施工任务书_v3.md:68-98`）。 |
| 2 | 已闭合 | `save_opportunity()` 与 `execute._merge()` 均被明确要求投影 `assigned_spu` / `assignment_source` / `assign_run_id`（`pipeline/docs/施工任务书_v3.md:131-132`）。 |
| 3 | 已闭合 | 已明确 015 的 CTE 不是可替换对象，改为 030 建函数并执行可重跑的当前库重分类 SQL，且不改历史 015 文件（`pipeline/docs/施工任务书_v3.md:45-56`）。 |
| 4 | 未闭合 | 双 MV、兼容视图和刷新函数的方案已写出，但刷新指令写成不存在的 `voc_spu_v2`/`_v3`，而定义对象是 `voc_spu_issue_v2`/`voc_spu_issue_v3`；按字面收尾会直接报对象不存在（`pipeline/docs/施工任务书_v3.md:100-110`；`pipeline/docs/架构规格_v3.md:325-334`）。 |
| 5 | 已闭合 | 已要求 031 增加 `scope_source`，v3 行写 `'v3-未计算'`，并同步战略查询、路由、模板及停止 v3 的 `n_eff/scope` 写入（`pipeline/docs/施工任务书_v3.md:64,129,135`）。 |
| 6 | 已闭合 | 已补 `voc_opp_id_map`、旧引用盘点脚本、47 条 proposal 处置、OPP2 命名空间及灰度期禁用清库路径（`pipeline/docs/施工任务书_v3.md:65-66,155-184`）。 |
| 7 | 已闭合 | 已将所有产品页、原声和质量查询明确列为 `oe.assigned_spu` 连接改造对象，并要求 v3 路径不再从消息数组推导 SPU（`pipeline/docs/施工任务书_v3.md:85-96,134`）。 |

## 二、订正引入的新阻断项

1. **快照重复写入语义互相矛盾。** 033 要求同一 `run_id` 已有行时幂等返回、不报错（`pipeline/docs/施工任务书_v3.md:68-75`），但最低测试又要求“同一 run_id 二次写入被拒绝”（`pipeline/docs/施工任务书_v3.md:139-149`）。两种合理实现分别允许重试或使重试失败，验收结果和故障恢复路径不同，必须统一。

2. **shadow 收尾没有限定 v3 代次。** 规格要求 v2/v3 同表并存、互不覆盖（`pipeline/docs/架构规格_v3.md:614-624`），但现有 finalize 的机会点选择和 snapshot 写入仍是全表/按 `last_week` 选择（`pipeline/scripts/run_generate.py:89-96,133-149`）；任务书虽要求改 run_generate，却没有给这两个选择加 OPP2/明确代次过滤（`pipeline/docs/施工任务书_v3.md:126,177-184`）。照此施工会把 v2 行带入 v3 收尾，产生旧代 proposal/snapshot 改写，破坏 shadow 隔离。

## 三、放行结论

**仍需修改。** 上轮第 4 条仍未闭合：刷新普通兼容视图时按任务书字面会调用不存在的 MV，满足“施工中途直接报错”。此外，订正新增的快照重复写入矛盾会使实现和验收无法唯一确定，shadow 收尾未限定代次会让 v2/v3 同表产生不符合规格的跨代写入；这两项分别满足“指令不唯一导致返工”和“产出与架构规格冲突”，不是健壮性建议。

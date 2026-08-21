# v3 守恒自检修复交付报告

## 结论

三处阻断与同源探针已按任务书修复。生成前的路由门、收尾投影门和终态等式门
现在都基于物理键集合而非同源计数；离线全量单元测试结果为
`245 passed, 1 xfailed, 23 subtests passed`。

本交付只写迁移文件，未执行 SQL，未连接数据库，也未调用 LLM、embedding、网络、
SSH 或 Git。

## 主动改了什么、为什么改

### A. 路由守恒门

- 在 `db.py` 增加当前 run 的物理快照键读取接口。
- `generate_opportunities()` 不再从 `old_rows` 反推 F；F 直接取
  `voc_assign_snapshot` 的 `(message_id, seq, assigned_spu)` 物理行。
- R 从 `route_classified_evidence()` 实际产生的老品桶行独立取得。
- 同时校验行数、两侧重复键及
  `snapshot EXCEPT routed` / `routed EXCEPT snapshot`；失败信息包含 F、R、
  两侧差集计数与每侧最多 10 个样例键。
- 单独运行新品生命周期时明确记为“不适用”；共用 run_id 的老品进程负责证明
  快照守恒，避免把新品进程的空老品范围误判成漏路由。

原因：旧实现的 F 与 R 来自同一份 Python 列表，无法发现快照 JOIN 漏行或
SQL/Python 门径漂移。

### B. 投影双向检查

- `validate_assignment_projection()` 物理读取当前 run 的快照键、OPP2 老品关系键和
  终态键。
- 显式计算 `snapshot EXCEPT relation` 与 `relation EXCEPT snapshot`。
- 快照中缺少关系的键只有在终态账存在同键时才合法；既无关系又无终态时立即失败。
- 保留原有 assignment source、老品/新品列契约和跨 SPU 检查。

原因：旧实现只拦 `projected_distinct_rows > snapshot_rows`，关系漏投影会被放行。

### C. 可对账终态账与损耗门

- 新增 `pipeline/sql/035_terminal_ledger.sql`：
  - 保留全部 `voc_unclassified_evidence` 旧行；旧行的 `run_id`、`assigned_spu`
    保持 `NULL`。
  - 增加 `terminal_id` 技术主键，解除旧 `(message_id, seq, week)` 主键对 SPU
    fan-out 的折叠。
  - v3 新行按 `(run_id, message_id, seq, assigned_spu)` 唯一。
  - 扩充终态原因：`unclassified`、`vote_dropped`、`truncated`、
    `generation_failed`、`grounding_rejected`。
  - 加入管理员守卫、事务、`SET LOCAL search_path`、可重跑保护、自检及
    `voc_writer` / `voc_human` / `voc_reader` 显式授权。
- 在 `db.py` 增加按扇出键写终态、读取终态和同 run 重试清账接口；无 SPU 的
  legacy 写入继续按旧三列幂等。
- 管线逐键记录全部允许继续的老品终态出口：Stage1 unclassified、
  vote_dropped、范围 truncated、生成 group 失败、grounding 拒绝。
- 收尾计算：
  - `P_unique` = 当前 run OPP2 老品关系的 distinct 扇出键；
  - `D` = 当前 run 终态账的 distinct 扇出键；
  - 强制 `F = P_unique + D`，并检查关系/终态互斥、终态不超快照。
- `config.py` 新增 `VOC_TERMINAL_LOSS_MAX_RATIO`，默认 `0.05`；该阈值明确与
  STRATEGY 及生成失败容忍策略无关，`D/F` 超标整轮失败。

原因：旧终态表缺 run/SPU 粒度，无法计算替代等式；group 失败也只有汇总计数。

### D. 只读探针

- 探针的 F 改为物理读取当前 run 的 `voc_assign_snapshot`。
- R 改为从实际老品分桶结果取得。
- 输出双向差集、两侧样例、重复数，并在不守恒时返回非零退出码。
- 保持只读，不增加任何写库调用。

原因：旧探针的 F/R 同源，`✓ R = F` 恒成立。

### 测试

新增离线注入测试覆盖：

1. 快照 N 行、路由 N−1 行；
2. 路由多出快照不存在的一行；
3. 行数相同但键集合不同；
4. 关系缺键且终态无记录；
5. 关系缺键但终态闭合且 `D/F` 合规；
6. `D/F` 超过 0.05；
7. 同一事实扇出两个 SPU 后产生两条终态；
8. 探针在等数不等集注入下输出双向非空差集。

所有新增测试均使用 fake 数据与既有离线导入夹具，不连接数据库、不调用模型或
embedding。另补充 035 静态迁移契约，并更新既有并行持久化测试的快照/终态假件。

### 文档

仅订正 `pipeline/docs/架构规格_v3.md` §5 及直接交叉引用：删除不成立的
`P >= F`，改为物理快照/路由双向差集、`F = P_unique + D` 和独立损耗率门；
同时将 2026-08-19 同源探针的“守恒成立”结论标为作废待复测。

## 偏离任务书

无设计或范围偏离。

实现上选择在原 `voc_unclassified_evidence` 表内增加技术主键，而不是新建第二张
终态表；这是为了同时满足“存量不丢失”和 fan-out 不折叠。旧数据未删除或改写
业务字段。为保证同一 run_id 失败后可重试，老品生成开始前只清除该 run 自己的
部分终态行，NULL-run 存量及其他 run 均不受影响。

## 验证记录

- `python -m py_compile`：通过。
- `python -m pytest pipeline/tests/test_*.py --tb=short`：
  `245 passed, 1 xfailed, 23 subtests passed in 0.38s`。
- 未运行迁移、验收 SQL、数据库测试或任何联网测试。

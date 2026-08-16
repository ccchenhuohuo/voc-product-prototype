# VOC 机会看板系统

`system/` 是面向产品经理的服务端渲染机会看板，读取 `pipeline/` 生成的 VOC PostgreSQL 数据，并把人工状态写回独立的 manual 表。应用采用 FastAPI + Jinja2 + 仓库内置的 HTMX-compatible 精简运行时；运行期不调用 LLM，也不依赖 npm 或外网。数据库结构与权限须达到 `pipeline/sql/` 当前最终等效结构，应用本身不执行迁移。注意，`011` 末尾的自检要求 `voc_spu_issue` 已有条目，`012` 的自检要求已有未合并的新品创新机会；`013` 只适用于符合已核实 645 条分布的既有 012 存量库，当前 `001` 已建立新来源约束的空库不执行它。完全空库不能不经数据准备就机械执行整组脚本。

`voc_message.src_line` 与 `voc_opportunity.src_line` 均保存「电商 / 社媒」，表示消息或机会的发现来源。`voc_opportunity.channel` 仍保存「需求缺口 / 竞品对标」，不是消息来源。

## 安装与启动

需要 Python 3.11 或更高版本，以及可访问现有 VOC PostgreSQL 的运行环境：

```bash
cd system
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

ASGI import string 是 `app.main:app`，上述命令监听 `127.0.0.1:8000`。仓库没有内置 systemd、Docker Compose 或其他进程托管配置，生产进程的服务名须由运维环境确认。

八个用户可见的 GET 入口是：`/` 重定向 `/iter`，老品迭代队列 `/iter`，SPU 详情 `/spu/{spu}`，问题原声 `/issue/{spu}/{opp_id}`，新品共享池 `/inno`，新品详情 `/inno/{opp_id}`，战略视图 `/strategy`，产品检索 `/search`。问题条目和新品创新分别通过 `/issue/{spu}/{opp_id}/status`、`/inno/{opp_id}/status` 提交状态。另有已注册的局部模板路由 `/spu/{spu}/issues`，但当前模板、静态脚本与测试都没有调用它，是否作为预留接口保留需后续确认。老品队列只展示当前有问题条目的 SPU；产品检索覆盖 `voc_spu` 中的全部 SPU。

复活检测可独立执行：

```bash
python scripts/check_revive.py --dry-run
python scripts/check_revive.py
```

`--dry-run` 会读取数据库并只打印判定结果，不写库；不带该选项时会把符合条件的 SPU 问题条目改回「考虑中」，并记录复活原因。可用 `--actor` 指定写入 `updated_by` 的执行者，默认值为 `voc-revive-check`。本项目未接入调度器，若需周期执行，须由外部调度配置调用该脚本。

查询 SQL 的真库规划检查是另一个运维入口：

```bash
python scripts/check_sql.py
```

它会对 `app/queries.py` 中登记的查询执行不带 `ANALYZE` 的 `EXPLAIN`；需要真实数据库连接，不属于离线测试。

## 环境变量

应用和两个运维脚本只使用 human 身份，以下五项必须设置：

- `VOC_PG_HOST`
- `VOC_PG_PORT`
- `VOC_PG_DB`
- `VOC_PG_HUMAN_USER`
- `VOC_PG_HUMAN_PASSWORD`

Web 应用启动时会读取 `system/.env`；`scripts/check_revive.py` 与 `scripts/check_sql.py` 不加载该文件，运行前必须把以上变量导出到进程环境。

可选的 `VOC_APP_USER` 仅供 Web 状态更新写入 `updated_by`，未设置时使用 `voc_human`。它不是数据库密码，也不改变数据库角色；复活脚本使用前述 `--actor`，不读取该变量。

## 权限边界

应用只读机器表/物化视图：`voc_spu`、`voc_spu_issue`、`voc_opportunity`、`voc_opp_evidence`、`voc_evidence`、`voc_message`。

应用只写两张人工表：

- `voc_spu_issue_manual`：SPU 卡内问题条目的人工状态。
- `voc_opportunity_manual`：新品创新卡的人工状态。

两张 manual 表的合法状态由数据库 `CHECK` 约束定义；触发器负责校验「不考虑」理由并写审计日志，SPU 问题触发器还维护 `baseline_evi_count` 与 `closed_at`。应用不写物化视图、不复制这些数据库规则，只把数据库错误转成可读提示。

人工判断会被后续的锁定保护与裁决逻辑读取；专用墓碑抑制函数 `check_tombstone()` 当前只有定义、尚未接入调用链，不能视为已生效。`system/` 不主动触发 `pipeline/` 的抽取、生成、调度或部署流程。

## 测试

```bash
cd system
python3 -m pytest tests/
python3 -m compileall app scripts tests
```

也可从仓库根目录运行 `python3 -m pytest system/tests/`。测试覆盖纯函数、mock 路由、模板与静态资源契约；前端契约在本机有 Node.js 时还会启动 Node 子进程，没有时跳过。测试不连接数据库、不执行 SQL，也不会留下测试数据。

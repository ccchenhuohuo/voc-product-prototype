# VOC 产品机会系统

把电商评论与社媒内容中的用户原声，转成可追溯、可认领、可闭环的产品机会。

仓库包含两个已经实现的 Python 子项目；二者不通过 HTTP 互调，只通过同一个 PostgreSQL 数据库交接：

| 目录 | 职责 | 运行入口 |
|---|---|---|
| [`pipeline/`](pipeline/) | 从云听 CEM 抽取、清洗、生成并消解机会点，刷新 SPU 展开层；以 Dagster 资产图编排 | `voc_analytics.definitions` |
| [`system/`](system/) | 以 FastAPI + Jinja2 + HTMX 展示老品迭代、新品创新与战略视图，并记录 PM 决策 | `app.main:app` |

代码和旧文档中的历史名称「线A / 线B」分别等价于「电商 / 社媒」。事实层 `voc_message.src_line` 使用「电商 / 社媒」，机会层 `voc_opportunity.src_line` 与相关代码路径仍保留「线A / 线B」。

## 数据如何流动

```text
云听 CEM（电商、社媒）
        │
        ▼
pipeline / Dagster
  抽取与清洗 → 机会生成与消解 → SPU 展开 → 提案/快照/放行
        │  voc_writer 写机器层
        ▼
PostgreSQL（voc）
  事实：voc_message / voc_evidence
  机会：voc_opportunity / voc_opp_evidence
  展开：voc_spu / voc_spu_issue（物化视图）
        │  voc_human 读取
        ▼
system / FastAPI
  服务端渲染看板
        │  PM 操作只回写人工层
        └─ voc_spu_issue_manual / voc_opportunity_manual
```

数据库触发器负责状态约束、审计和锁保护。看板代码只写上述两张人工表；`voc_human` 角色还可裁决 `voc_proposal` 的有限字段，但不能修改机会点、事实层等机器产出。管道不会覆盖人工表或已锁定条目的语义字段，但证据、计数、排序和最近周等统计字段仍会随新数据更新。完整权限与约束以顺序应用后的 `001`–`012` 迁移为准。

看板要求连接一个已经达到 [`pipeline/sql/`](pipeline/sql/) 中 `001`–`012` 最终结构的业务库；业务表没有数据时页面可以为空，要看到实际内容则还需已有管道产物。仓库目前没有可验证的空库完整初始化路径：`m0_deploy_pg.sh` 只执行 `001`–`004`，而 `011`、`012` 尾部的自检依赖已有机会点或 SPU 条目的业务键，空库批量执行会在自检处失败。不要把现有 SQL 文件当成可无条件一把执行的迁移 runner；应由 DBA/运维按 [`运维交接.md`](pipeline/docs/运维交接.md) 在对应数据就绪后应用并核验。数据库结构就绪后，管道进程和看板进程可以独立运行。

## 准备开发环境

两个项目都要求 Python 3.11 或更高版本。可在仓库根目录共用一个虚拟环境：

```bash
python3 --version  # 必须为 3.11+
python3 -m venv .venv
. .venv/bin/activate

# 只运行两个项目
python -m pip install -e ./pipeline -e ./system

# 开发和测试；与上一条二选一
python -m pip install -e './pipeline[test]' -e './system[test]'
```

也可以分别进入 `pipeline/`、`system/` 创建虚拟环境：运行时安装 `pip install -e .`，需要测试时安装 `pip install -e '.[test]'`。

## 配置环境变量

### pipeline

管道 Python 模块及普通 Python 脚本只读取进程环境，不会自动加载 `.env`。`rerun_both.sh` 会在切换到其固定工作目录后主动读取 `./.env`，生产 systemd 则通过 `EnvironmentFile` 注入；除此之外，若把变量放在 `pipeline/.env`，启动前需显式导入：

```bash
cd pipeline
set -a
. ./.env
set +a
```

核心变量：

| 变量 | 必需性 | 默认值 / 用途 |
|---|---|---|
| `VOC_PG_HOST` | 可选 | `127.0.0.1` |
| `VOC_PG_PORT` | 可选 | `5434` |
| `VOC_PG_DB` | 可选 | `voc` |
| `VOC_PG_USER` | 可选 | `voc_writer`；管道机器角色 |
| `VOC_PG_PASSWORD` | 使用密码认证的数据库时必需 | 默认为空 |
| `YUNTING_MCP_KEY` | 执行抽取时必需 | 云听 API Key |
| `BAILIAN_API_KEY` | 执行生成、向量或消解阶段时必需 | 百炼 API Key |

可选调优变量及代码默认值：

| 变量 | 默认值 |
|---|---|
| `YUNTING_MCP_URL` | `https://ytcem.cn/mcp` |
| `VOC_PROJECT_ID` | `bd7496c9a5a6410eab699bcc2276566a` |
| `BAILIAN_EMBEDDING_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1`；同时作为 embedding 与 chat API base |
| `VOC_EMBED_MODEL` | `text-embedding-v4` |
| `VOC_CHAT_MODEL` | `qwen-plus` |
| `VOC_LLM_CONCURRENCY` | `64` |
| `VOC_AUTO_MERGE` | 默认关闭；只有显式设为 `1` 才开启 |

`VOC_AUTO_MERGE=1` 会让周度资产实际执行自动融合，不应作为普通本地默认配置。

生产 Dagster 元数据库使用以下变量；这些是部署脚本生成的口径，本地 `dagster dev` 不要求复用：

| 变量 | 部署值 / 用途 |
|---|---|
| `VOC_DAGSTER_PG_USER` | `voc_admin` |
| `VOC_DAGSTER_PG_PASSWORD` | Dagster 元数据库口令 |
| `VOC_DAGSTER_PG_HOST` | `127.0.0.1` |
| `VOC_DAGSTER_PG_PORT` | `5434` |
| `VOC_DAGSTER_PG_DB` | `voc_dagster` |
| `DAGSTER_HOME` | `/opt/dagster/voc-analytics-production` |

具体生成方式见 [`m5_deploy_dagster.sh`](pipeline/scripts/m5_deploy_dagster.sh) 和 [`运维交接.md`](pipeline/docs/运维交接.md)。

### system

看板启动时会自动读取 `system/.env`。以下五项没有默认值，缺少任意一项都会拒绝建立数据库连接：

| 变量 | 用途 |
|---|---|
| `VOC_PG_HOST` | PostgreSQL 主机 |
| `VOC_PG_PORT` | PostgreSQL 端口；当前部署口径为 `5434` |
| `VOC_PG_DB` | 业务库名；当前为 `voc` |
| `VOC_PG_HUMAN_USER` | 人工角色；通常为 `voc_human` |
| `VOC_PG_HUMAN_PASSWORD` | 人工角色口令 |

可选的 `VOC_APP_USER` 用作状态更新的 `updated_by`，默认 `voc_human`；它不是数据库账号或密码。

`system/scripts/check_revive.py` 和 `system/scripts/check_sql.py` 不会自动加载 `system/.env`。运行这些 CLI 前需先用 shell 导入环境变量；两者都会访问真实数据库，不能当作离线自检。

### 云听 MCP 配置

根目录的 `.mcp.json` 只供本地 MCP 客户端使用，不会向管道注入 `YUNTING_MCP_KEY`。配置结构见 [`.mcp.json.example`](.mcp.json.example)；复制为 `.mcp.json` 后把占位符换成本地密钥。`.mcp.json` 和所有 `.env` 均已被忽略，不要把真实密钥写进其他文件。

## 启动

### 启动管道开发实例

完成依赖安装并把管道变量导入当前 shell 后：

```bash
cd pipeline
python -m dagster dev -m voc_analytics.definitions \
  --host 127.0.0.1 --port 3002
```

这会启动本地 Dagster Webserver 和 daemon；仅应在 `3002` 空闲的开发机使用，在已运行生产 Webserver 的主机上需另选端口。Dagster 中的 `voc_weekly_schedule` 虽定义为每周一 `02:00`（`Asia/Shanghai`），但当前默认是停用状态，生成的运行请求也没有绑定分区键。修复分区绑定前应保持停用，并显式选择目标分区运行；不能把“服务已启动”视为“上一周分区会自动正确运行”。加载界面不会代替数据库迁移，物化资产会访问配置的数据库、云听或百炼服务，执行前先确认环境指向。

生产部署不是 `dagster dev`，而是三个独立的 systemd 单元；详见下方服务表和运维交接文档。`run_ingest.py`、`run_generate.py` 等手工数据脚本只用于经确认的回填或调试，例行生产调度以 Dagster 资产图为准；`scripts/` 中另有部署、备份、恢复及破坏性的机会层重跑入口，使用前必须阅读各文件头的前置条件与副作用。

### 启动机会看板

确认五个必需数据库变量已经写入 `system/.env` 或导入当前 shell：

```bash
cd system
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

浏览器访问 `http://127.0.0.1:8000/`；根路径会跳转到老品迭代队列 `/iter`。看板运行时不调用 LLM，不依赖 npm 或外网。

## 服务名与端口

下列生产口径来自仓库内的部署脚本：

| 组件 | 名称 | 监听地址 |
|---|---|---|
| PostgreSQL | Docker 容器 `voc-postgres`，业务库 `voc` | `127.0.0.1:5434`（容器内 `5432`） |
| Dagster code location | `voc-analytics-grpc.service` | `127.0.0.1:4003` |
| Dagster daemon | `voc-analytics-dagster-daemon.service` | 无 HTTP 端口 |
| Dagster Webserver | `voc-analytics-dagster-webserver.service` | `127.0.0.1:3002` |
| 机会看板（本地入口） | Uvicorn / `app.main:app` | `127.0.0.1:8000` |

仓库目前没有看板的 systemd、Docker 或 Compose 部署定义，因此不能从代码确认其生产服务名；上表的 `8000` 是仓库已记录的本地启动口径。

## 测试与静态自检

安装两个项目的 `[test]` extras 后，可直接从仓库根目录发现并运行两边的测试：

```bash
python3 -m pytest pipeline/tests/ system/tests/
python3 -m compileall pipeline system
```

与本仓库基线验收完全一致的分项目命令是：

```bash
(cd system && python3 -m pytest tests/)
(cd pipeline && python3 tests/test_clean_fields.py)
```

这些是离线测试，覆盖 mock 数据库、纯函数、模板与前端契约，不连接数据库；部分前端契约测试会在本机有 Node.js 时调用它。不要把其他验收脚本或 `system/scripts/check_sql.py` 混入离线测试命令。

## 进一步阅读

- 管道结构、手工入口与约束：[`pipeline/README.md`](pipeline/README.md)
- 当前管道实现规格：[`pipeline/docs/pipeline_redesign.html`](pipeline/docs/pipeline_redesign.html)
- 下一阶段重构计划：[`pipeline/docs/rework_plan.html`](pipeline/docs/rework_plan.html)
- 生产服务与排障：[`pipeline/docs/运维交接.md`](pipeline/docs/运维交接.md)
- 看板入口与权限边界：[`system/README.md`](system/README.md)
- 模型语义与交互规格：[`system/docs/voc_model_spec.html`](system/docs/voc_model_spec.html)
- 定稿验收原型：[`system/design/prototype.html`](system/design/prototype.html)

# VOC 机会看板系统

`system/` 是面向产品经理的服务端渲染机会看板，读取 `pipeline/` 生成的 VOC PostgreSQL 数据，并把人工状态写回独立的 manual 表。应用采用 FastAPI + Jinja2 + 仓库内置的 HTMX-compatible 精简运行时；运行期不调用 LLM，也不依赖 npm 或外网。数据库结构与权限须达到 `pipeline/sql/` 当前最终等效结构，应用本身不执行迁移。注意，`011` 末尾的自检要求 `voc_spu_issue` 已有条目，`012` 的自检要求已有未合并的新品创新机会；`013` 只适用于符合已核实 645 条分布的既有 012 存量库，当前 `001` 已建立新来源约束的空库不执行它。完全空库不能不经数据准备就机械执行整组脚本。

`voc_message.src_line` 与 `voc_opportunity.src_line` 均保存「电商 / 社媒」，表示消息或机会的发现来源。机会点不再保留旧业务子类型列；需求缺口、竞品对标等业务语义由现行内容与分类字段承载。

## 安装与启动

需要 Python 3.11 或更高版本，以及可访问现有 VOC PostgreSQL 的运行环境：

```bash
cd system
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
uvicorn app.main:app --host 127.0.0.1 --port 8000 \
  --proxy-headers --forwarded-allow-ips=127.0.0.1
```

ASGI import string 是 `app.main:app`，上述命令监听 `127.0.0.1:8000`。`--proxy-headers --forwarded-allow-ips=127.0.0.1` 只信任本机反代的转发头，使 HTTPS 入口下 `url_for()` 产生正确的 `https://` 静态资源地址。仓库没有内置 systemd、Docker Compose 或其他进程托管配置，生产进程的服务名须由运维环境确认。

八个用户可见的 GET 入口是：首页 `/`，老品迭代队列 `/iter`，SPU 详情 `/spu/{spu}`，问题原声 `/issue/{spu}/{opp_id}`，新品共享池 `/inno`，新品详情 `/inno/{opp_id}`，战略视图 `/strategy`，产品检索 `/search`。问题条目和新品创新分别通过 `/issue/{spu}/{opp_id}/status`、`/inno/{opp_id}/status` 提交状态。另有已注册的局部模板路由 `/spu/{spu}/issues`，但当前模板、静态脚本与测试都没有调用它，是否作为预留接口保留需后续确认。老品队列只展示当前有问题条目的 SPU；产品检索覆盖 `voc_spu` 中的全部 SPU。

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

Web 应用启动时会读取 `system/.env`；`scripts/check_revive.py` 与 `scripts/check_sql.py` 不加载该文件，运行前必须把以上变量导出到进程环境。完整格式见 `.env.example`。

可选的 `VOC_APP_USER` 仅在 Web 请求取不到会话身份时作为 `updated_by` 降级值，未设置时使用 `voc_human`。正常经过全局登录门的路径不会使用它。它不是数据库密码，也不改变数据库角色；复活脚本使用前述 `--actor`，不读取该变量。

## 登录与授权

Web 应用使用飞书企业自建应用登录，并用 14 天有效的服务端签名 `voc_session` cookie 保存 `open_id`、姓名和签发时间。飞书 access token 只用于登录回调中紧接着的用户信息请求，不落盘也不写入 cookie。除 `/auth/`、`/static/` 和不查数据库的 `/healthz` 外，所有路由都必须先登录。

认证配置如下：

- `VOC_FEISHU_APP_ID`：必填，飞书企业自建应用 App ID。
- `VOC_FEISHU_APP_SECRET`：必填密钥，只在服务端换取 token。
- `VOC_SESSION_SECRET`：必填密钥，用于签名会话与 OAuth state；缺失时应用启动立即失败，不会自动生成。
- `VOC_PUBLIC_BASE_URLS`：必填，逗号分隔的公开站点根地址，例如 `https://voc.ulanzi.com`。它同时是 OAuth 回调 origin 和非 GET 请求 `Origin` 的白名单。
- `VOC_ALLOWED_OPEN_IDS`：必须声明，逗号分隔的可读飞书 `open_id`；空值表示拒绝所有人。
- `VOC_WRITER_OPEN_IDS`：必须声明，逗号分隔的可写飞书 `open_id`；空值表示无人可写。写名单不自动包含读权限，需要写入状态的人必须同时出现在两个名单中。
- `VOC_COOKIE_SECURE`：可选，默认 `true`；只有本地 HTTP 开发时才设为 `false`。

名单一律用 `open_id` 维护，不按姓名匹配；姓名会变更且可能重名，`open_id` 才是账号锚点。首次登录只能证明身份，不会自动授予查看或写入权限。修改名单后需重启 Web 进程以重读配置。

飞书开放平台中必须为 `VOC_PUBLIC_BASE_URLS` 的每个根地址登记完整回调 URL：`<根地址>/auth/feishu/callback`，例如 `https://voc.ulanzi.com/auth/feishu/callback`。应用会根据本次请求的受信反代头选择白名单中的 origin；伪造的 Host 不会成为回调地址。

两个状态 POST 写入的 `updated_by` 现为 `姓名(open_id后8位)`，例如 `张三(ced16385)`，使重名用户在 `voc_status_log` / `voc_spu_issue_log` 中仍可区分。这只是给现有 SQL 传入操作人，应用不新建会话表或执行迁移。

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

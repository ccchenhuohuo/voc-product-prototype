# VOC 机会看板 · 公网发布

把看板从「tailnet 内裸奔」变成「带飞书登录的公网服务」的全部材料。

## 链路

```
浏览器 → 公司边界设备 voc.ulanzi.com:<伟杰分配的端口> → 172.16.100.122:8081 (nginx)
       → 127.0.0.1:8090 (uvicorn)
```

两个已确认的约束：

- **这台机器没有公网 IP**，只有内网 `172.16.100.122` 和 Tailscale 地址。公网入口必须由伟杰在
  边界设备上做端口映射，我们自己在机器上怎么配都出不去。
- **443 用不了**（伟杰 2026-08-18 明确答复），会分配一个非标准端口。所以对外地址形如
  `https://voc.ulanzi.com:12345`。**这个端口一旦定死就不能改**——飞书开放平台登记的登录回调
  地址里带着它，改端口登录会直接挂。

> 同机的 `ipms`（`ip.vijimfund.cn`，走 Cloudflare 隧道）是 peter 自己买的域名和隧道，
> **不是公司的接入方式**，别拿它当网络方案的参照。它唯一的参照价值是那份飞书 OAuth 的
> 代码实现——同租户下已跑通，坑都踩过。

---

## 上线顺序（硬约束，不可调换）

```
1. 飞书应用建好                      ✅ 2026-08-18 已完成，本文档第一节
2. 登录门合入并在 tailnet 上验收通过    ✅ 代码与测试已完成，待部署到服务器
3. systemd + nginx 装上，127.0.0.1:8081 自测通过（线 D，本文档第二节）
4. 才让伟杰把端口映射打开              （线 A，等端口与 TLS 答复）
```

**绝不能先开公网再补登录。**中间哪怕只裸奔一小时，客户原声、竞品对标和产品规划决策就在公网上了。

---

## 零、待伟杰确认（卡着线 C 和线 D）

| 问题 | 影响 | 状态 |
|---|---|---|
| 外网端口号是多少 | 飞书回调地址、`VOC_PUBLIC_BASE_URLS` 都要带它 | 已问，待答 |
| 那个端口上有没有 TLS | 决定下面 A/B 哪个分支 | 已问，待答 |

**分支 A（首选）**：边界终止 https，我们收明文 HTTP，只需透传 `Host` 与 `X-Forwarded-Proto`。

**分支 B**：边界纯转发，向伟杰要一张 `*.ulanzi.com` 证书，在本机 nginx 上终止 https。

**不接受的第三种情况**：明文 HTTP 直接对公网。登录会话 cookie 在明文链路上可被旁路嗅探，
登录门形同虚设。若最终只能明文，就退回「仅 tailnet 访问」，不上公网。

---

## 一、飞书应用（线 C）—— 2026-08-18 已完成

企业自建应用 `VOC 机会看板` 已创建并发布，租户「唯迹科技股份」。

| 项 | 值 |
|---|---|
| App ID | `cli_aa09569d94f81bd1` |
| App Secret | **不记录在此**。在[凭证与基础信息](https://open.feishu.cn/app/cli_aa09569d94f81bd1/baseinfo)页复制，直接落到服务器 `/etc/voc-board/secrets.env`（0600） |
| 应用能力 | 网页应用 · 桌面端主页 `http://100.111.223.41:8090/` · 从主页跳转时在浏览器中打开 |
| 权限 | `contact:user.base:readonly`（用户身份，**免审权限**，已开通） |
| 重定向 URL | `http://100.111.223.41:8090/auth/feishu/callback`（tailnet 验收用） |
| 可用范围 | 部分成员 → 仅陈煜 |
| 对外共享 | 两项均「不允许」 |
| 版本 | 1.0.0 已发布，状态**已启用** |

### 发布免审核的门槛（重要）

> 可用范围仅包括应用所有者和其他 **10 名以内**成员时，发布无需审核。

原计划里「等企业管理员审核」被判为最长的外部等待，**在 ≤11 人范围内这条不成立**——
提交即自动通过。只有把可用范围扩到 11 人以上才会触发管理员审批。
所以加人时要有意识：第 11 个人是一道成本台阶。

### 还差的两步（都卡在伟杰给端口）

1. **补登记正式重定向 URL**：`<协议>://voc.ulanzi.com:<端口>/auth/feishu/callback`
   在「安全设置」里加，**必须精确匹配**含协议、域名、端口、路径，末尾不要多斜杠；
   换 token 时应用还要把同一个值原样再传一次，对不上飞书会直接拒。
   重定向 URL 属于安全设置，**改它不需要重新发版**。
2. **把桌面端主页改成正式地址并发新版本**：这一项属于版本配置，改了要重新发布
   （范围没超 11 人的话仍是免审，秒过）。

### 加人时怎么做

⚠️ **`open_id` 按「人 × 应用」隔离，不是「人」的全局 ID。**
同一个人在不同飞书应用下的 `open_id` 完全不同。
**绝对不要**用下面这条命令的输出往名单里填：

```bash
lark-cli contact +search-user --query "姓名" --as user   # ← 这是 lark-cli 自己那个应用的口径
```

2026-08-18 就是这么栽的：拿 lark-cli（应用 `cli_a94d6bb5…`）查到的 `ou_fca7590d…` 填进名单，
结果本人登录后被自己的看板拒之门外，日志上看 OAuth 全程正常、只在名单校验那步 403。
他在本应用下的真实值是 `ou_78be9337…`，两者毫无关系。
跨应用稳定的标识是 `union_id`，但本系统不用它。

**正确流程（四步，缺一不可）：**

1. 飞书开放平台 →「版本管理与发布」→ 创建新版本 → 可用范围加上这个人 → 发布
   （总人数不超过 11 就免审，秒过）
2. 让这个人访问 https://voc.ulanzi.com:28081 用飞书登录
3. 他会看到「尚未开通查看权限」页——**那个页面会显示他在本应用下的真实 open_id**，
   让他把这串值发过来。这页就是为这一步设计的
4. 把值写进服务器 `.env`：`VOC_ALLOWED_OPEN_IDS` 加读权限，
   `VOC_WRITER_OPEN_IDS` 加写权限（两者独立，只读的人不要进第二个），
   然后 `sudo systemctl restart voc-board`

姓名只用于显示。**不要按姓名做权限匹配**——重名会把两个人的操作记到同一个账号上。

**当前名单：**

| 人 | 部门 | 本应用 open_id | 读 | 写 |
|---|---|---|---|---|
| 陈煜 | 战略组 | `ou_78be93373e0128e8382e1fda1b584295` | ✅ | ✅ |
| 龙俊吉 | 品牌参谋部-战略组 | `ou_dd583bea6dc7814193a15bd54831c39c` | ✅ | ✖️ 只读 |

对照一下这两个人在 lark-cli 那个应用下的值，能直观看出 per-app 隔离：
陈煜是 `ou_fca7590d…`、龙俊吉是 `ou_7d0d52d1…`，与上表毫无关系。
**加人时务必核对：如果你填进名单的值和 `lark-cli contact +search-user` 输出一致，那一定是错的。**

---

## 二、服务器安装（线 D）

三个文件都在本目录。**装之前先看清楚这台 nginx 上还跑着别人的 ipms**，配错会把它一起带下去。

### 1. 密钥文件

```bash
sudo mkdir -p /etc/voc-board
sudo install -m 0600 -o root -g root /dev/null /etc/voc-board/secrets.env
sudo vi /etc/voc-board/secrets.env
```

内容（值自己填，**不要提交进仓库**）：

```ini
VOC_FEISHU_APP_ID=cli_aa09569d94f81bd1
VOC_FEISHU_APP_SECRET=       # 从飞书凭证页复制，不要经过任何中间文件
VOC_SESSION_SECRET=
VOC_PUBLIC_BASE_URLS=https://voc.ulanzi.com:<端口>
VOC_ALLOWED_OPEN_IDS=ou_fca7590dc3279cbc15fea6a5652f2abc
VOC_WRITER_OPEN_IDS=ou_fca7590dc3279cbc15fea6a5652f2abc
VOC_COOKIE_SECURE=true
```

tailnet 验收阶段先用 3.5 节那组临时值，公网就绪后再换成上面这组。

`VOC_PUBLIC_BASE_URLS` 里的协议和端口必须与飞书后台登记的回调地址完全一致。

`VOC_SESSION_SECRET` 这样生成：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**空名单等于拒绝所有人**，这是应用侧刻意的 fail-closed 设计。别把它误读成「还没配所以先放行」。

### 2. systemd

```bash
sudo install -m 0644 voc-board.service /etc/systemd/system/voc-board.service
sudo systemctl daemon-reload
sudo systemctl enable --now voc-board
systemctl status voc-board --no-pager
```

装完就可以不再用 `scripts/start.sh` 了——那个 `setsid` 裸进程的做法在服务器重启后不会自己
回来，2026-08-18 那次内存升级重启就得手动补命令。

### 3. nginx

```bash
sudo install -m 0644 nginx-conf.d-voc-http.conf /etc/nginx/conf.d/voc-http.conf
sudo install -m 0644 nginx-sites-voc-board.conf /etc/nginx/sites-available/voc-board
sudo ln -sfn /etc/nginx/sites-available/voc-board /etc/nginx/sites-enabled/voc-board
sudo nginx -t
sudo systemctl reload nginx
```

`nginx -t` 不通过就不要 reload。`conf.d/voc-http.conf` 里的变量特意加了 `voc_` 前缀，
就是为了不和 ipms 已有的 `$ipms_forwarded_proto` 撞名——撞名会让 nginx 直接起不来。

若走分支 B（本机终止 https），按 `nginx-sites-voc-board.conf` 里注释掉的三行改 `listen`。

### 3.5 tailnet 阶段验收的两个必踩坑

上线顺序第 2 步要在 tailnet 上先验收一轮。这时对外地址还是
`http://<tailscale 地址>:8090`，和最终的公网地址不同，必须临时把它也加进配置，
否则会遇到两个看起来毫无道理的现象：

```ini
# tailnet 验收期临时配置，公网上线前改回去
VOC_PUBLIC_BASE_URLS=http://<tailscale 地址>:8090
VOC_COOKIE_SECURE=false
```

- **不加进 `VOC_PUBLIC_BASE_URLS`：所有状态修改都返回 403。** 中间件对非 GET 请求校验
  `Origin` 头必须命中白名单，浏览器发的 `Origin` 是 tailnet 地址，对不上就拒。
  页面能正常浏览，只有写入挂掉，很容易误判成权限名单配错了。
- **不把 `VOC_COOKIE_SECURE` 置 false：登录完还是登录页。** tailnet 走的是 http，
  `Secure` cookie 浏览器根本不会回传，表现为「登录成功但会话立刻丢失」的死循环。

这个 tailnet 地址同样要登记进飞书后台的重定向 URL，否则跳转回来会被飞书拒绝。
**公网上线时记得把这两项改回正式值**，别把临时配置带上生产。

### 4. 本机自测（还没开公网）

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: voc.ulanzi.com' http://127.0.0.1:8081/healthz
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' -H 'Host: voc.ulanzi.com' http://127.0.0.1:8081/iter
```

预期：`/healthz` 返回 200；`/iter` 返回 302 且跳向 `/auth/login`。

**如果 `/iter` 返回 200，说明登录门没生效，立刻停下，不要通知伟杰开端口。**

### 5. 通知伟杰开端口映射

确认上一步无误后，告诉伟杰服务已就绪，内网回源地址 `172.16.100.122:8081`。

---

## 三、上线验收清单

公网通了以后逐条走，**任意一条不过就让伟杰把端口关掉回退**。

| # | 检查 | 预期 |
|---|---|---|
| 1 | 未登录访问对外地址的 `/iter` | 跳到登录页，看不到任何数据 |
| 2 | 用飞书账号登录 | 进入老品迭代队列 |
| 3 | 用不在 `VOC_ALLOWED_OPEN_IDS` 的账号登录 | 停在无权限页，且显示该账号自己的 open_id |
| 4 | 在读名单但不在写名单的人点状态按钮 | 403，有可读中文提示，不是静默失败 |
| 5 | 有写权限的人改一条状态 | 成功，页面局部刷新正常（HTMX 没碎） |
| 6 | 改完查审计表 | `changed_by` 是**真人姓名**，不是 `voc_human` |
| 7 | 浏览器控制台 | 无混合内容告警，样式与 htmx 正常加载 |
| 8 | 取 `/robots.txt` | 返回 `Disallow: /` |
| 9 | `sudo systemctl restart voc-board` 后 | 服务自己回来，已登录用户会话仍有效 |
| 10 | 重启整台服务器 | `voc-board` 自启，无需手工命令 |

第 6 条的查法：

```bash
psql -h 127.0.0.1 -p 5434 -U voc_reader -d voc \
  -c "SELECT opp_id, from_status, to_status, changed_by, changed_at
      FROM voc_status_log ORDER BY changed_at DESC LIMIT 5"
```

---

## 四、已知遗留

- **会话是无状态签名 cookie，没有服务端吊销。**人离职后从名单里删掉即可拒绝下次请求，
  但已签发的 cookie 在过期前仍然有效。内部系统可接受；若要即时吊销需引入会话表，
  那会碰到 `001`–`015` 的 SQL 迁移契约，届时得走 `016`。
- **没有告警。**服务挂了只有 systemd 会重启，没有任何地方会通知人。
- **访问日志只落本机**，没有集中收集与留存策略。
- **非标准端口对外**。端口写死在飞书回调里，日后若公司能放开 443，改动要同步三处：
  边界映射、`VOC_PUBLIC_BASE_URLS`、飞书后台重定向 URL。

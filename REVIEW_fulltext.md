# 片段→全文上下文改造代码评审

## 结论

当前改造不能放行：有 2 项阻断问题，另有 2 项重要问题。核心原因是新增全文只进入了 Stage1 的一份格式化函数，真正负责 Stage2 标题生成的格式化函数仍然只输出片段；同时 `msg_sentiment` 的写入代码已经上线于工作树，但 027 迁移未执行且没有启动前守护。

## 阻断

### 1. Stage2 标题生成仍然只读片段

**位置：** `pipeline/voc_analytics/stages/generate.py:11-18,38,70-75,175-179`；调用方 `pipeline/voc_analytics/pipeline.py:706-707`。

`stage1._fmt_items` 已读取 `full_text`，但 Stage2 调用的是 `generate.write_prototype`，该模块有另一份同名 `_fmt_items`，只取 `evidence_text/snippet/content`，完全忽略 `full_text`。同一个旧格式化函数还被 Stage4 `llm_review` 使用。

具体失败场景：输入 `evidence_text="正规大品牌的"`、`full_text="自拍杆的遥控器，快改进，连最基本的对焦功能都没有"` 时，直接调用 `stage1._fmt_items` 会输出 `【完整原文】...`，而 `generate._fmt_items` 的输出只有 `正规大品牌的`。因此 Stage2 仍可能根据 6 字片段生成原改造要解决的错误标题，52 例 A/B 的全文收益没有进入标题生成链路。现有 `pipeline/tests/test_fulltext_context.py:31-49` 只测 Stage1 helper，没有覆盖 `generate.write_prototype` 的实际提示词。

### 2. `msg_sentiment` 列未执行会直接阻断 ingest

**位置：** `pipeline/voc_analytics/clean.py:287-291`；`pipeline/voc_analytics/db.py:52-65`；`pipeline/voc_analytics/ingest.py:104-105`；迁移 `pipeline/sql/027_msg_sentiment_fulltext.sql:13-19`。

`to_message` 对每一条消息都加入 `msg_sentiment`，而 `_upsert` 按第一行字典键动态生成 `INSERT` 列名。当前数据库若未执行 027，第一次 `db.save_messages(all_msgs)` 就会因 `voc_message.msg_sentiment` 不存在抛出 `UndefinedColumn`，后续 `save_evidence` 也不会执行，整窗 ingest 失败。

027 的“必须先于代码上线”只存在于 SQL 文件头注释；仓库没有运行时 schema 检查或迁移器。`pipeline/scripts/m0_deploy_pg.sh:68-74` 的空库迁移列表不含 027，`pipeline/README.md:75` 仍把迁移说明为 001-017。题设已明确 027 未执行，因此这是当前上线顺序下的确定性失败，不是潜在兼容性问题。

## 重要

### 1. SQL 截 600，但 Stage1 实际只传 400 字

**位置：** `pipeline/voc_analytics/db.py:332-335`；`pipeline/voc_analytics/stages/stage1.py:134-142`。

生成池先把正文截到 600 字，但 `_fmt_items` 随后对 `full_text` 再做 `[:400]`。因此字符 401-600 永远不会进入 Stage1 提示词；即使修复了 Stage2 的 renderer，仍只会得到前 400 字。

具体失败场景：正文前 400 字是背景，决定性句子“连最基本的对焦功能都没有”从第 501 字开始时，Stage1 收到的全文没有这句，分组仍可能依据不完整上下文做错。若 400 是有意的输入上限，应把 SQL 上限统一为 400；若 600 是契约上限，则 renderer 的 400 截断违背了该契约。

### 2. 空白片段的空判不一致，问题页会显示空白并丢失全文展开

**位置：** `system/app/queries.py:1046,1053-1056`；`system/app/templates/issue-voices.html:24-30`。同样的未 trim `NULLIF` 也出现在 `system/app/queries.py:1118-1122`。

`voice_text` 使用 `NULLIF(e.snippet, '')`，而 `full_content` 使用 `NULLIF(btrim(e.snippet), '')`。当 `e.snippet='   '` 且 `msg.content='杆子有点粗，收纳不便'` 时，前者返回三个空格（Jinja 中仍为真值），后者判为空并不下发 `full_content`。模板因此优先渲染空白 `voice_text`，也没有 `<details>` 可展开正文，用户看不到原声。

## 建议

### 1. `seen_full` 只在一次 batch 调用内去重

**位置：** `pipeline/voc_analytics/stages/stage1.py:117-145,233-236,277-280`。

`seen_full` 是 `_fmt_items` 的局部集合；每个 50 行 batch、每个投票 permutation 都重新创建。具体地，51 行都来自同一 `message_id` 时，前 50 行和最后 1 行两个调用都会各带一次 400 字全文；开启三轮投票至少出现 6 份全文，而不是每条消息每轮 1 份。随机置换把同一消息的片段分到多个 batch 时，副本还会更多。

这不会丢上下文，但会产生确定的输入 token 和费用膨胀，并使函数注释中“同一条消息全文只随首个片段出现一次”只在单次调用内成立。可以在 bucket/round 层传递已见 message_id，或按 message_id 先组织 batch。

### 2. 片段是全文前缀时，正文被完整重复；电商有中译时也会额外带一份全文

**位置：** `pipeline/voc_analytics/stages/stage1.py:101-105,134-142`；`pipeline/voc_analytics/db.py:328-335`。

例如 `evidence_text="杆子有点粗"`、`full_text="杆子有点粗，收纳也不方便"`，当前渲染是“杆子有点粗【完整原文】杆子有点粗，收纳也不方便”，前缀重复一次。对电商行，若 `evidence_text` 已是完整 `content` 但 `content_zh` 存在且不同，SQL 的译文优先策略也会触发追加；这在中文提示词上可能是有意的，但会把同一事实以原文和译文各传一遍。可考虑只追加前缀之后的尾部，或显式区分“原文/译文”并按 token 预算决定是否保留。

## 已验证无误

- **生产口径下的判等基本成立。** 位置：`pipeline/voc_analytics/db.py:328-335`、`pipeline/voc_analytics/stages/stage1.py:101-145`。`generation_pool` 的 `btrim`、`_evidence_text` 的换行归一化和 `full.strip()` 使“片段就是全文”以及仅有尾部换行时不追加；片段是全文子串但不相等时会追加全文。`content_zh` 与 `content` 不同会按设计追加译文，属于上面记录的 token 代价，不是判等失效。
- **`ISSUE_VOICES` 的非空非等片段条件方向正确。** 位置：`system/app/queries.py:1053-1056`。`btrim(msg.content) IS DISTINCT FROM btrim(e.snippet)` 对“片段是正文子串但不等于正文”返回真，能下发全文；问题只在空白片段与 `voice_text` 使用了不同的空判。
- **高亮没有引入模板注入。** `system/app/web.py:25-28` 使用 Jinja2Templates 默认 HTML autoescape，两个模板的 `pre/hit/post/full_content` 都没有 `|safe`；本地环境中 `<script>alert(1)</script>` 渲染为 `&lt;script&gt;alert(1)&lt;/script&gt;`，`<mark>` 只是模板写出的标签。
- **创新页的正常非空片段路径是自洽的。** `system/app/routes/innovation.py:41-43` 以 `snippet` 定位，`system/app/templates/innovation-card.html:26-30` 也优先显示同一 `row.snippet`；只有空白/空片段才落入前述边界。

## 测试覆盖缺口

- `pipeline/tests/test_fulltext_context.py:31-49` 没有调用 `stages/generate.py`，因此漏掉了本评审第一个阻断问题。
- 没有覆盖跨 batch/三轮投票去重、正文超过 400 字、决定性信息落在 401-600 字、正文前缀重复、`content_zh` 与电商正文不同等场景。
- 没有对 027 做静态迁移契约测试，也没有测试“列不存在时 ingest 在 `save_messages` 处失败”或上线前 schema 守护。
- `system/tests/test_full_voice.py` 只测 SQL 字符串存在、viewmodel 分割和模板片段存在；没有执行查询级空白片段用例、渲染级 HTML 转义用例，也没有覆盖创新页 `snippet=''/'   '` 与 `content_zh != content` 的组合。

## 本次验证

- `system/tests/test_full_voice.py`：5 passed。
- `pipeline/tests/test_lifecycle_prompt_routing.py` 与 `pipeline/tests/test_stage1_accounting.py`：8 passed。
- `pipeline/tests/test_fulltext_context.py` 未能收集：当前环境缺少 `psycopg`（`ModuleNotFoundError`）；未安装依赖，未连接数据库，未执行 SQL，未调用 LLM。
- 另以纯函数输入对比验证了 Stage1 renderer 会追加全文，而 `stages/generate.py` renderer 不会；并验证了 51 行跨 batch 时全文副本会在两个调用中各出现一次。

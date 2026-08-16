"""PostgreSQL 访问层。所有写入走 ON CONFLICT DO UPDATE，保证重跑幂等。"""
from __future__ import annotations
import os
from contextlib import contextmanager
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import config as C


@contextmanager
def conn(autocommit: bool = False, role: str | None = None):
    """role='human' 时用 voc_human 连接——机器角色无权写人工表（§10.2 角色矩阵）。"""
    cfg = dict(C.PG)
    if role == "human":
        cfg["user"] = os.environ.get("VOC_PG_HUMAN_USER", "voc_human")
        cfg["password"] = os.environ.get("VOC_PG_HUMAN_PASSWORD", "")
    with psycopg.connect(**cfg, row_factory=dict_row, autocommit=autocommit) as c:
        yield c


def execute_as_human(sql: str, params: Sequence | None = None) -> int:
    with conn(role="human") as c:
        cur = c.execute(sql, params)
        c.commit()
        return cur.rowcount


def q(sql: str, params: Sequence | None = None) -> list[dict]:
    with conn() as c:
        return c.execute(sql, params).fetchall()


def q1(sql: str, params: Sequence | None = None) -> Any:
    r = q(sql, params)
    if not r:
        return None
    return next(iter(r[0].values()))


def execute(sql: str, params: Sequence | None = None) -> int:
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.rowcount


def _upsert(c, table: str, rows: list[dict], keys: Sequence[str],
            update_cols: Sequence[str] | None = None) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    upd = [x for x in (update_cols or cols) if x not in keys]
    placeholders = "(" + ",".join(["%s"] * len(cols)) + ")"
    sql = (f'INSERT INTO {table} ({",".join(cols)}) VALUES {placeholders} '
           f'ON CONFLICT ({",".join(keys)}) DO ' +
           (f'UPDATE SET {",".join(f"{c_}=EXCLUDED.{c_}" for c_ in upd)}' if upd else "NOTHING"))
    data = [tuple(Jsonb(r[c_]) if isinstance(r[c_], (dict,)) else r[c_] for c_ in cols)
            for r in rows]
    cur = c.cursor()
    cur.executemany(sql, data)
    return cur.rowcount


def upsert(table: str, rows: list[dict], keys: Sequence[str],
           update_cols: Sequence[str] | None = None, chunk: int = 1000) -> int:
    with conn() as c:
        n = upsert_in_transaction(c, table, rows, keys, update_cols, chunk)
        c.commit()
    return n


def upsert_in_transaction(c, table: str, rows: list[dict], keys: Sequence[str],
                          update_cols: Sequence[str] | None = None,
                          chunk: int = 1000) -> int:
    """在调用方已开启的事务内 upsert，不自行提交。"""
    n = 0
    for i in range(0, len(rows), chunk):
        n += _upsert(c, table, rows[i:i + chunk], keys, update_cols) or 0
    return n


# ---------------------------------------------------------------- 领域写入
def save_messages(rows: list[dict]) -> int:
    """message_id 是全局证据身份，不允许新来源抢占已有 ID。"""
    if not rows:
        return 0
    incoming: dict[str, str] = {}
    for row in rows:
        message_id = row.get("message_id")
        src_line = row.get("src_line")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError(f"消息缺少有效 message_id：{message_id!r}")
        if not isinstance(src_line, str) or not src_line:
            raise ValueError(f"{message_id} 缺少有效 src_line：{src_line!r}")
        previous = incoming.setdefault(message_id, src_line)
        if previous != src_line:
            raise ValueError(
                f"message_id 跨来源冲突：{message_id} -> {previous!r}/{src_line!r}")
    existing = q(
        "SELECT message_id, src_line FROM voc_message WHERE message_id = ANY(%s)",
        [sorted(incoming)])
    for row in existing:
        expected = incoming[row["message_id"]]
        if row["src_line"] != expected:
            raise ValueError(
                f"message_id 已属于其他来源：{row['message_id']} -> "
                f"{row['src_line']!r}，新值 {expected!r}")
    return upsert("voc_message", rows, ["message_id"])


def save_evidence(rows: list[dict]) -> int:
    return upsert("voc_evidence", rows, ["message_id", "seq"])


def save_run_log(ctx, stage: str, **kw) -> None:
    row = {"run_id": f"{ctx.run_id}:{stage}", "week": ctx.week, "stage": stage,
           "llm_calls": ctx.llm_calls, "llm_tokens": ctx.llm_tokens,
           "llm_failed_modes": ctx.llm_failed_modes,
           "metrics": Jsonb(ctx.metrics), "status": kw.pop("status", "success")}
    row.update(kw)
    upsert("voc_run_log", [row], ["run_id"])


def save_unclassified(pairs: Iterable[tuple[str, int]], week: str, reason: str) -> int:
    rows = [{"message_id": m, "seq": s, "week": week, "reason": reason} for m, s in pairs]
    return upsert("voc_unclassified_evidence", rows, ["message_id", "seq", "week"])


# ---------------------------------------------------------------- 领域查询
def generation_pool(week_start: str | None = None,
                    week_end: str | None = None) -> list[dict]:
    """统一证据生成池：先按内容取数，来源能力只负责执行 R3 入池约束。

    返回的每一行都有真实 ``(message_id, seq)``，不再把消息级社媒内容
    伪装成 ``seq=0``。产品体验与「用户使用体验」负面分支要求非空片段；
    需求/对标分支允许以正文补足片段，但仍锚定命中内容标签的真实证据行。
    """
    sql = """
      WITH route AS (
        SELECT %s::text[] AS experience_tags,
               %s::text[] AS comparison_tags,
               %s::text[] AS gap_tags
      )
      SELECT e.message_id, e.seq, e.tag, e.tag_raw, e.sentiment,
             e.snippet, e.tax_path, e.tax_stage, e.tax_domain,
             e.tax_sub, e.tax_leaf, e.is_product, e.low_conf,
             m.content, m.content_zh, m.category, m.star, m.country,
             m.product_name, m.platform, m.lang, m.interactions,
             m.brands, m.content_type, m.url, m.src_line, m.spu,
             m.prod_line,
             p.requires_spu AS source_requires_spu,
             COALESCE(NULLIF(btrim(e.snippet), ''),
                      NULLIF(btrim(m.content), '')) AS evidence_text,
             CASE
               WHEN branch.product_experience THEN '产品体验'
               WHEN branch.user_experience THEN '用户使用体验'
               WHEN branch.comparison THEN '竞品对标'
               ELSE '需求缺口'
             END AS content_branch,
             CASE
               WHEN branch.comparison THEN '竞品对标'
               WHEN branch.gap THEN '需求缺口'
               ELSE '产品体验'
             END AS channel
        FROM voc_evidence e
        JOIN voc_message m USING (message_id)
       JOIN voc_source_policy p USING (src_line)
       CROSS JOIN route r
       -- content_type 是消息级多值内容标签；e.tag 是证据的问题主题。
       -- 两者不可互代，否则「用户使用体验」仍然会被漏掉。
       CROSS JOIN LATERAL (
         SELECT
           -- 不再按 low_conf 过滤（改造项 #2「去掉星级过滤」）。
           -- low_conf 是「负面标签 + 4~5 星」的交叉校验，本意是挡误标，
           -- 但实测它挡掉 2,066 条产品体验证据（7,743 -> 9,809，+27%），
           -- 而高星用户同样会写真实缺陷（「很好用，就是卡扣有点松」）。
           -- 需求方定性：覆盖度优先，不设星级过滤。
           -- low_conf 标记本身仍保留在证据上，下游要降权或抽检随时可用。
           (e.is_product
            AND e.sentiment = '负面'
            AND NULLIF(btrim(e.snippet), '') IS NOT NULL) AS product_experience,
           (COALESCE(m.content_type, ARRAY[]::text[]) && r.experience_tags
            AND e.sentiment = '负面'
            AND NULLIF(btrim(e.snippet), '') IS NOT NULL) AS user_experience,
           (COALESCE(m.content_type, ARRAY[]::text[]) && r.comparison_tags
            AND COALESCE(NULLIF(btrim(e.snippet), ''),
                         NULLIF(btrim(m.content), '')) IS NOT NULL
            AND COALESCE(cardinality(m.brands), 0) >= 2) AS comparison,
           (COALESCE(m.content_type, ARRAY[]::text[]) && r.gap_tags
            AND COALESCE(NULLIF(btrim(e.snippet), ''),
                         NULLIF(btrim(m.content), '')) IS NOT NULL) AS gap
       ) branch
       WHERE (branch.product_experience OR branch.user_experience
              OR branch.comparison OR branch.gap)
         -- 对所有“保证挂 SPU”的来源统一执行 R3 约束；不比较来源名称。
         AND (NOT p.requires_spu OR COALESCE(cardinality(m.spu), 0) > 0)
    """
    params: list = [list(C.DEWATER_EXPERIENCE), list(C.DEWATER_COMP),
                    list(C.DEWATER_GAP)]
    if week_start:
        sql += " AND m.publish_time >= %s"
        params.append(week_start)
    if week_end:
        sql += " AND m.publish_time < %s"
        params.append(week_end)
    sql += " ORDER BY e.message_id ASC, e.seq ASC"
    return q(sql, params)


def low_conf_intersection() -> dict:
    """PRD §4.2 要求 M1 算出的交集：low_conf 与【电商】可生成池的关系。

    必须过滤 src_line='电商' —— 电商的定义就是电商评论。早期漏了这个条件，
    把社媒证据也算进池子，会把覆盖率分母虚高近一倍。可生成池还必须与
    统一池中“保证挂 SPU”来源的 R3 入池契约一致：未挂 SPU 的电商消息是数据缺陷。
    """
    return q("""
      WITH pool AS (
        SELECT e.low_conf
          FROM voc_evidence e JOIN voc_message m USING(message_id)
         WHERE e.is_product AND e.sentiment='负面' AND e.snippet IS NOT NULL
           AND m.src_line='电商'
           AND cardinality(m.spu) > 0)
      SELECT
        (SELECT count(*) FROM voc_evidence WHERE low_conf)  AS low_conf_total,
        (SELECT count(*) FROM pool WHERE low_conf)          AS low_conf_in_pool,
        (SELECT count(*) FROM pool WHERE NOT low_conf)      AS usable_pool
    """)[0]

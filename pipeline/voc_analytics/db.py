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
    n = 0
    with conn() as c:
        for i in range(0, len(rows), chunk):
            n += _upsert(c, table, rows[i:i + chunk], keys, update_cols) or 0
        c.commit()
    return n


# ---------------------------------------------------------------- 领域写入
def save_messages(rows: list[dict]) -> int:
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
def line_a_pool(week_start: str | None = None, week_end: str | None = None) -> list[dict]:
    """电商生成池：产品体验分支 + 负面 + 非误标 + 有片段（§4.3）"""
    sql = """
      SELECT e.message_id, e.seq, e.tag, e.snippet, e.tax_path,
             m.category, m.star, m.country, m.product_name, m.platform, m.lang
        FROM voc_evidence e JOIN voc_message m USING (message_id)
       WHERE e.is_product AND NOT e.low_conf
         AND e.sentiment = '负面' AND e.snippet IS NOT NULL
         AND m.src_line = '电商'
    """
    params: list = []
    if week_start:
        sql += " AND m.publish_time >= %s"; params.append(week_start)
    if week_end:
        sql += " AND m.publish_time < %s"; params.append(week_end)
    sql += " ORDER BY m.star ASC NULLS LAST, e.message_id ASC, e.seq ASC"
    return q(sql, params)


def line_b_pool(channel_types: Sequence[str], week_start: str | None = None,
                week_end: str | None = None, multi_brand_only: bool = False) -> list[dict]:
    sql = """
      SELECT m.message_id, m.content, m.content_zh, m.platform, m.interactions,
             m.brands, m.content_type, m.url, m.lang
        FROM voc_message m
       WHERE m.src_line = '社媒' AND m.content_type && %s
    """
    params: list = [list(channel_types)]
    if multi_brand_only:
        sql += " AND array_length(m.brands,1) >= 2"
    if week_start:
        sql += " AND m.publish_time >= %s"; params.append(week_start)
    if week_end:
        sql += " AND m.publish_time < %s"; params.append(week_end)
    sql += " ORDER BY m.message_id ASC"
    return q(sql, params)


def low_conf_intersection() -> dict:
    """PRD §4.2 要求 M1 算出的交集：low_conf 与【电商】可生成池的关系。

    必须过滤 src_line='电商' —— 电商的定义就是电商评论。早期漏了这个条件，
    把社媒证据也算进池子，会把覆盖率分母虚高近一倍。
    """
    return q("""
      WITH pool AS (
        SELECT e.low_conf
          FROM voc_evidence e JOIN voc_message m USING(message_id)
         WHERE e.is_product AND e.sentiment='负面' AND e.snippet IS NOT NULL
           AND m.src_line='电商')
      SELECT
        (SELECT count(*) FROM voc_evidence WHERE low_conf)  AS low_conf_total,
        (SELECT count(*) FROM pool WHERE low_conf)          AS low_conf_in_pool,
        (SELECT count(*) FROM pool WHERE NOT low_conf)      AS usable_pool
    """)[0]

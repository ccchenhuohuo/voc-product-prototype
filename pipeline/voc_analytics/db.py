"""PostgreSQL 访问层。所有写入走 ON CONFLICT DO UPDATE，保证重跑幂等。"""
from __future__ import annotations
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

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


def q(sql: str, params: Sequence | Mapping[str, object] | None = None) -> list[dict]:
    with conn() as c:
        return c.execute(sql, params).fetchall()


def q1(sql: str, params: Sequence | Mapping[str, object] | None = None) -> Any:
    r = q(sql, params)
    if not r:
        return None
    return next(iter(r[0].values()))


def execute(sql: str, params: Sequence | Mapping[str, object] | None = None) -> int:
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
def ensure_message_schema(columns: tuple[str, ...] = ("msg_sentiment",)) -> None:
    """入库前确认 voc_message 已具备代码要写的列。

    upsert 按字典键动态生成列名，缺列会在 save_messages 才抛
    UndefinedColumn——那时云听导出（约 25 分钟）已经白拉。放在
    ingest 窗口最前面 fail-fast，报错直接指向要补的迁移。"""
    rows = q(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'voc_message' AND column_name = ANY(%s)",
        [list(columns)])
    missing = set(columns) - {r["column_name"] for r in rows}
    if missing:
        raise RuntimeError(
            f"voc_message 缺列 {sorted(missing)}：先执行对应迁移"
            f"（msg_sentiment → 027）再入库；晚失败会浪费整窗云听导出")


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


def backfill_social_spu_inheritance() -> int:
    """入库窗口后触发 023 的幂等社媒 SPU 继承重算。"""
    return int(q1("SELECT voc_backfill_social_spu_inheritance()") or 0)


def load_social_gates(message_ids: Sequence[str], prompt_ver: str) -> list[dict]:
    """只读取当前 prompt 版本的 G4 缓存；旧版本自动重判。"""
    ids = sorted(set(message_ids))
    if not ids:
        return []
    return q(
        """SELECT message_id, cls, claim, confidence, votes, prompt_ver
             FROM voc_social_gate
            WHERE message_id = ANY(%s) AND prompt_ver = %s
            ORDER BY message_id""",
        [ids, prompt_ver],
    )


def save_social_gates(rows: list[dict]) -> int:
    """按 message_id 覆盖旧 prompt 结果，并刷新判定时间。"""
    stamped = [
        {**row, "judged_at": datetime.now(timezone.utc)}
        for row in rows
    ]
    return upsert("voc_social_gate", stamped, ["message_id"])


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
SOCIAL_G1_TERMINAL = "G1/G1b 竞品内容"
SOCIAL_G2_TERMINAL = "G2 去水排除"
SOCIAL_G3_TERMINAL = "G3 品牌自述"


def _array_field(row: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = row.get(key)
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} 必须是数组或 None")
    return tuple(str(item) for item in value if item is not None)


def has_judgeable_social_text(row: Mapping[str, object]) -> bool:
    """与社媒 SQL 的 snippet -> content 可判文本口径等价。"""
    return any(
        isinstance(row.get(key), str) and bool(str(row[key]).strip())
        for key in ("evidence_text", "snippet", "content")
    )


def social_structural_terminal(
    message: Mapping[str, object],
    group_rows: Iterable[Mapping[str, object]] = (),
) -> str | None:
    """G1/G1b/G2/G3 的纯函数等价实现；``None`` 表示结构门通过。"""
    own = set(C.OWN_BRANDS)
    if any(brand not in own for brand in _array_field(message, "brands")):
        return SOCIAL_G1_TERMINAL

    group_id = message.get("message_group_id")
    for parent in group_rows:
        if parent.get("message_group_id") != group_id:
            continue
        if parent.get("message_type") not in {"帖子", "视频"}:
            continue
        if any(brand not in own for brand in _array_field(parent, "brands")):
            return SOCIAL_G1_TERMINAL

    tags = set(_array_field(message, "content_type"))
    if tags.intersection(C.SOCIAL_DROP_TAGS) or not tags.intersection(
        C.SOCIAL_KEEP_TAGS
    ):
        return SOCIAL_G2_TERMINAL

    author = message.get("author_name")
    if isinstance(author, str) and re.search(
        C.OFFICIAL_AUTHOR_PATTERN, author, flags=re.IGNORECASE
    ):
        return SOCIAL_G3_TERMINAL
    return None


def social_gate_conditions(message_alias: str = "m", evidence_alias: str = "e") -> dict[str, str]:
    """G1--G3 SQL 条件的单一构造器，生成池与对账查询共用。"""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", message_alias):
        raise ValueError("非法的 message SQL 别名")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", evidence_alias):
        raise ValueError("非法的 evidence SQL 别名")
    m, e = message_alias, evidence_alias
    return {
        "g1": f"""NOT EXISTS (
                 SELECT 1 FROM unnest({m}.brands) b
                  WHERE b <> ALL(%(own_brands)s)
               )
               AND NOT EXISTS (
                 SELECT 1 FROM voc_message p
                  WHERE p.message_group_id = {m}.message_group_id
                    AND p.message_type IN ('帖子','视频')
                    AND EXISTS (
                      SELECT 1 FROM unnest(p.brands) b2
                       WHERE b2 <> ALL(%(own_brands)s)
                    )
               )""",
        "g2": f"""NOT (COALESCE({m}.content_type, '{{}}'::text[])
                           && %(drop_tags)s)
               AND COALESCE({m}.content_type, '{{}}'::text[])
                           && %(keep_tags)s""",
        "g3": f"""({m}.author_name IS NULL
                       OR {m}.author_name !~* %(official_pattern)s)""",
        "text": f"""COALESCE(NULLIF(btrim({e}.snippet), ''),
                                  NULLIF(btrim({m}.content), '')) IS NOT NULL""",
    }


def _social_gate_params() -> dict[str, object]:
    return {
        "own_brands": list(C.OWN_BRANDS),
        "drop_tags": list(C.SOCIAL_DROP_TAGS),
        "keep_tags": list(C.SOCIAL_KEEP_TAGS),
        "official_pattern": C.OFFICIAL_AUTHOR_PATTERN,
    }


def social_structural_gate_counts(
    week_start: str | None = None,
    week_end: str | None = None,
) -> dict:
    """按证据行返回 G1--G3 顺序互斥终态，为七终态账本提供前半段。"""
    conditions = social_gate_conditions()
    params = _social_gate_params()
    window = ""
    if week_start is not None:
        window += " AND m.publish_time >= %(week_start)s"
        params["week_start"] = week_start
    if week_end is not None:
        window += " AND m.publish_time < %(week_end)s"
        params["week_end"] = week_end
    rows = q(f"""
      WITH candidates AS (
        SELECT ({conditions['g1']}) AS g1_ok,
               ({conditions['g2']}) AS g2_ok,
               ({conditions['g3']}) AS g3_ok
          FROM voc_evidence e
          JOIN voc_message m USING (message_id)
         WHERE m.src_line = '社媒'
           AND {conditions['text']}
           {window}
      )
      SELECT count(*)::bigint AS social_candidate_rows,
             count(*) FILTER (WHERE NOT g1_ok)::bigint AS g1_competitor_rows,
             count(*) FILTER (WHERE g1_ok AND NOT g2_ok)::bigint AS g2_dewater_rows,
             count(*) FILTER (WHERE g1_ok AND g2_ok AND NOT g3_ok)::bigint
               AS g3_official_rows,
             count(*) FILTER (WHERE g1_ok AND g2_ok AND g3_ok)::bigint
               AS structural_passed_rows
        FROM candidates
    """, params)
    return rows[0] if rows else {
        "social_candidate_rows": 0,
        "g1_competitor_rows": 0,
        "g2_dewater_rows": 0,
        "g3_official_rows": 0,
        "structural_passed_rows": 0,
    }


def generation_pool(week_start: str | None = None,
                    week_end: str | None = None) -> list[dict]:
    """统一证据池：电商口径不变；社媒只经 G1--G3 后送 G4。"""
    conditions = social_gate_conditions()
    params = _social_gate_params()
    sql = f"""
      SELECT e.message_id, e.seq, e.tag, e.tag_raw, e.sentiment,
             e.snippet, e.tax_path, e.tax_stage, e.tax_domain,
             e.tax_sub, e.tax_leaf, e.is_product, e.low_conf,
             m.content, m.content_zh, m.category, m.star, m.country,
             m.product_name, m.platform, m.lang, m.interactions,
             m.brands, m.content_type, m.url, m.src_line, m.spu,
             m.spu_inherited, m.prod_line, m.message_group_id,
             m.message_type, m.parent_id, m.author_name, m.message_title,
             CASE
               WHEN m.message_type IN ('评论','回复') THEN COALESCE((
                 SELECT NULLIF(btrim(parent.message_title), '')
                   FROM voc_message parent
                  WHERE parent.message_group_id = m.message_group_id
                    AND parent.message_type IN ('帖子','视频')
                    AND NULLIF(btrim(parent.message_title), '') IS NOT NULL
                  ORDER BY parent.message_id
                  LIMIT 1
               ), '（无）')
               ELSE '（无）'
             END AS parent_title,
             p.requires_spu AS source_requires_spu,
             COALESCE(NULLIF(btrim(e.snippet), ''),
                      NULLIF(btrim(m.content), '')) AS evidence_text,
             -- 片段是按标签切出来的单句，脱离上下文常常不足以判断诉求主体
             -- （实测「正规大品牌的」6 个字生成出「品牌正规性背书」标题）。
             -- Stage1/Stage2 的提示词需要完整原文兜底。截断与提示词侧
             -- （_evidence_body 的 limit=400）保持同一契约值：SQL 多传的
             -- 字节提示词永远用不到，纯浪费传输。译文优先：提示词是中文。
             left(COALESCE(NULLIF(btrim(m.content_zh), ''),
                           NULLIF(btrim(m.content), '')), 400) AS full_text
        FROM voc_evidence e
        JOIN voc_message m USING (message_id)
        JOIN voc_source_policy p USING (src_line)
       WHERE (
         -- 电商入口保持：产品标签 + 负面 + 非空原声 +
         -- requires_spu 来源必须挂云听事实 SPU。
         (m.src_line = '电商'
          AND e.is_product
          AND e.sentiment = '负面'
          AND NULLIF(btrim(e.snippet), '') IS NOT NULL
          AND (NOT p.requires_spu OR COALESCE(cardinality(m.spu), 0) > 0))
         OR
         (m.src_line = '社媒'
          AND {conditions['g1']}
          AND {conditions['g2']}
          AND {conditions['g3']}
          AND {conditions['text']})
       )
    """
    if week_start is not None:
        sql += " AND m.publish_time >= %(week_start)s"
        params["week_start"] = week_start
    if week_end is not None:
        sql += " AND m.publish_time < %(week_end)s"
        params["week_end"] = week_end
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

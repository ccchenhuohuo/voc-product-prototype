"""M1 事实层：抽取 → 清洗 → 炸开 → 落库（PRD v8 §4.1–4.2）。"""
from __future__ import annotations
import io, time
from datetime import datetime, timedelta

import openpyxl

from . import clean, config as C, db, taxonomy, yunting


def read_xlsx(blob: bytes) -> list[dict]:
    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header = [str(h) if h is not None else "" for h in next(rows)]
    except StopIteration:
        return []
    out = [dict(zip(header, r)) for r in rows]
    wb.close()
    return out


OVERLAP_DAYS = 1          # §4.1：抽取窗口回溯一天，吸收上游迟到数据


def week_bounds(week: str) -> tuple[datetime, datetime]:
    """'2026-W33' -> (周一 00:00, 下周一 00:00)。日历意义上的那一周，用于打标。"""
    y, w = week.split("-W")
    start = datetime.fromisocalendar(int(y), int(w), 1)
    return start, start + timedelta(days=7)


def window_bounds(week: str) -> tuple[datetime, datetime]:
    """实际处理窗口：T-8 到 T，比日历周多往前一天（§4.1）。

    上游数据会迟到——周一 02:00 跑的时候，上周日晚些时候的消息可能还没进
    云听。严格按周边界切会永久漏掉这部分：下一周的窗口从周一开始，那条
    周日的数据两边都不覆盖。多回溯一天把边界盖住，重复部分由 message_id
    主键幂等吸收，不会重复计数。

    抽取与生成必须用【同一个窗口】：若抽取回溯一天而生成严格按周过滤，
    迟到数据虽然入了库，却永远落在任何一次生成的窗口之外，等于没抽。
    """
    start, end = week_bounds(week)
    return start - timedelta(days=OVERLAP_DAYS), end


def ingest_window(ctx, start: datetime, end: datetime) -> dict:
    """抽取一个时间窗的两条线，落 voc_message + voc_evidence。"""
    tax = taxonomy.load()
    dewater = set(taxonomy.dewater_values())
    stats: dict = {"slices": {}, "misaligned": 0, "tail_fixed": 0}
    t0 = time.time()

    plans = [
        ("电商", "COMMENT", C.COMMENT_FILTER, 30),   # 月切片，实测单月 <5000
        ("社媒", "SOCIAL", C.SOCIAL_FILTER, 15),     # 半月切片，实测单月约 4500
    ]
    all_msgs: list[dict] = []
    all_evi: list[dict] = []

    for src_line, qtype, tf, slice_days in plans:
        blobs, metas = yunting.export_window(qtype, start, end, tf, slice_days, ctx.mode)
        stats["slices"][src_line] = metas
        seen: set[str] = set()
        for blob in blobs:
            for row in read_xlsx(blob):
                mid = row.get("消息ID")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                msg = clean.to_message(row, src_line, ctx.run_id, dewater)
                # 保留期（§10.3）
                pt = msg.get("publish_time")
                if isinstance(pt, datetime):
                    msg["retention_until"] = (pt + timedelta(days=30 * C.RETENTION_MONTHS)).date()
                all_msgs.append(msg)
                evi, mis = clean.explode(row, tax, src_line)
                stats["misaligned"] += mis
                for e in evi:
                    if e["snippet"] != e["snippet_raw"]:
                        stats["tail_fixed"] += 1
                all_evi.extend(evi)
        stats[f"{src_line}_messages"] = len(seen)

    n_msg = db.save_messages(all_msgs)
    n_evi = db.save_evidence(all_evi)
    stats.update(messages_written=n_msg, evidence_written=n_evi,
                 elapsed_s=round(time.time() - t0, 1))
    ctx.metrics.setdefault("ingest", {}).update(stats)
    return stats


def snapshot_taxonomy(week: str) -> int:
    """每周快照标签体系，用于检测上游变更（§9.1）。"""
    tax = taxonomy.load()
    rows = []
    for tag, path in tax.path.items():
        for line in tax.lines.get(tag, {"未定"}):
            rows.append({"tag": tag, "prod_line": line, "tax_path": path,
                         "tax_l1": tax.l1.get(tag, ""),
                         "is_product": tax.is_product(tag),
                         "is_scene": tax.is_scene(tag), "week": week})
    return db.upsert("voc_tag_taxonomy", rows, ["tag", "prod_line", "week"])

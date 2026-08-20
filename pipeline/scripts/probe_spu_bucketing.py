"""v3 SPU 分桶探针 —— 只读，绝不写库。

回答两个问题：

  1. **卡数**：SPU 分桶下 Stage1 到底切出多少个问题模式。
     这是 v3 耗时的主变量——Stage2/3/4 按卡调用，v2 实测 9,427 次调用里
     大头在这里。当前只知道上界：v2 的 1,006 张老品卡按其证据涉及的 SPU
     数展开，上界 4,357（4.33×）。真实值取决于 Stage1 在 SPU 桶内的合并
     行为，只能实测。

  2. **质量**：同一批证据在 v2 标签桶与 v3 SPU 桶下的分组差异。

同时按规格 §7.1 做「桶内标签直方图」提示词特征的成对对比（--tag-hist），
因为该特征目前【没有实现】，不做 A/B 就无法声称 v3 分桶更好。

安全边界：本脚本只 SELECT，不 INSERT/UPDATE/DELETE，不调用 db.save_*，
不写 voc_run_log。RunCtx 是纯内存对象，账本不落库。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 按脚本自身位置解析包根，本机与服务器同一份代码。旁边的 run_generate.py
# 写死了 /home/sdy/voc-analytics，那份只能在服务器上跑。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voc_analytics import config as C, db, llm  # noqa: E402
from voc_analytics.stages import stage1  # noqa: E402

OUT_DEFAULT = "/tmp/probe_spu_bucketing.json"

# 与 v2 同一批证据：本轮生成期挂靠到老品卡的行（排除 cross_line —— 那是
# 收尾跨源汇聚的产物，正是 v3 要删掉的环节，计入会污染对比基底）。
POOL_SQL = """
WITH fact AS (
  SELECT DISTINCT oe.message_id, oe.seq
    FROM voc_opp_evidence oe
    JOIN voc_opportunity o ON o.opp_id = oe.opp_id
   WHERE o.opp_type = '老品迭代' AND oe.match_by IN ('rule','llm')
)
SELECT f.message_id, f.seq, e.snippet, e.tag, e.tax_path, e.tax_domain,
       m.star, m.category, m.prod_line, m.src_line, m.content,
       left(COALESCE(NULLIF(btrim(m.content_zh), ''),
                     NULLIF(btrim(m.content), '')), 400) AS full_text,
       COALESCE(NULLIF(btrim(e.snippet), ''),
                NULLIF(btrim(m.content), '')) AS evidence_text,
       COALESCE(m.spu, '{}'::text[]) || COALESCE(m.spu_inherited, '{}'::text[])
         AS spu_all,
       COALESCE(cardinality(m.spu), 0) > 0 AS has_fact_spu
  FROM fact f
  JOIN voc_evidence e ON e.message_id = f.message_id AND e.seq = f.seq
  JOIN voc_message m ON m.message_id = f.message_id
 ORDER BY f.message_id, f.seq
"""


def fan_out(rows: list[dict]) -> dict[str, list[dict]]:
    """按 spu ∪ spu_inherited 扇出分桶。多 SPU 证据进入每个被提及的桶。"""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        seen: set[str] = set()
        for raw in row.get("spu_all") or []:
            spu = (raw or "").strip()
            if not spu or spu in seen:
                continue
            seen.add(spu)
            item = dict(row)
            item["assigned_spu"] = spu
            item["assignment_source"] = "fact" if row["has_fact_spu"] else "inherited"
            buckets.setdefault(spu, []).append(item)
    return buckets


def bucket_context(spu: str, items: list[dict], tag_hist: bool) -> dict:
    """镜像 pipeline._bucket_context，但分块键换成 SPU。"""
    categories = sorted({r.get("category") for r in items if r.get("category")})
    prod_lines = sorted({r.get("prod_line") for r in items if r.get("prod_line")})
    paths = Counter(r.get("tax_path") for r in items if r.get("tax_path"))
    stars = [r["star"] for r in items if r.get("star") is not None]
    low = (sum(1 for s in stars if s <= 2) / len(stars)) if stars else None

    tag = spu
    if tag_hist:
        # 规格 §7.1 的待实现特征：桶内标签直方图，只作特征不作硬分块。
        hist = Counter(r.get("tag") for r in items if r.get("tag"))
        top = "、".join(f"{name}×{n}" for name, n in hist.most_common(8))
        if top:
            tag = f"{spu}（桶内标签分布：{top}）"

    return {
        "bucket_key": f"老品迭代|{spu}",
        "category": categories[0] if len(categories) == 1 else (
            "跨品类" if categories else "未定"),
        "storage_category": categories[0] if len(categories) == 1 else None,
        "tag": tag,
        "tax_path": paths.most_common(1)[0][0] if paths else "",
        "prod_line": prod_lines[0] if len(prod_lines) == 1 else (
            "通用" if prod_lines else "未定"),
        "low_star_rate": round(low, 4) if low is not None else None,
    }


def run_bucket(spu: str, items: list[dict], ctx, tag_hist: bool) -> dict:
    info = bucket_context(spu, items, tag_hist)
    started = time.time()
    split = stage1.split_bucket(items, "老品迭代", info, ctx, vote=False)
    groups = stage1.merge_similar_modes(split["groups"], ctx)
    return {
        "spu": spu,
        "rows": len(items),
        "batches": (len(items) + C.BATCH_SIZE - 1) // C.BATCH_SIZE,
        "groups_raw": len(split["groups"]),
        "groups": len(groups),
        "singletons": sum(1 for g in groups if len(g.get("members", [])) == 1),
        "unclassified": len(split["unclassified"]),
        "dropped": len(split["dropped"]),
        "inherited_rows": sum(1 for i in items
                              if i["assignment_source"] == "inherited"),
        "elapsed_s": round(time.time() - started, 1),
        "mode_names": [g.get("mode_name") for g in groups],
        "sample": [
            {"mode": g.get("mode_name"),
             "n": len(g.get("members", [])),
             "voices": [(items[i].get("evidence_text") or "")[:60]
                        for i in g.get("members", [])[:3]]}
            for g in groups[:6]
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-buckets", type=int, default=0,
                    help="只跑最大的 N 个 SPU 桶；0 = 全部 277 个")
    ap.add_argument("--min-rows", type=int, default=1,
                    help="跳过小于该行数的桶")
    ap.add_argument("--tag-hist", action="store_true",
                    help="启用规格 §7.1 的桶内标签直方图特征（成对对比用）")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="并行桶数；桶内批次另有 LLM_CONCURRENCY")
    ap.add_argument("--merge-cos", type=float, default=None,
                    help="覆盖 MODE_MERGE_COS（默认 0.85）。它只决定跨批模式归并的"
                         "【候选】，判同仍由 L3 做——降低它是把漏合并的对子送进 L3，"
                         "误合并由 L3 挡。规格 §8.3 的主门槛就是漏合并。")
    ap.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()

    if args.merge_cos is not None:
        C.MODE_MERGE_COS = args.merge_cos    # 探针内存态覆盖，不改配置文件

    rows = db.q(POOL_SQL)
    buckets = fan_out(rows)
    unique_facts = len({(r["message_id"], r["seq"]) for r in rows})
    fanned = sum(len(v) for v in buckets.values())

    picked = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    picked = [(s, i) for s, i in picked if len(i) >= args.min_rows]
    if args.limit_buckets:
        picked = picked[:args.limit_buckets]

    print(f"[探针] 唯一事实 U={unique_facts}  扇出行 F={fanned}  "
          f"F/U={fanned/unique_facts:.3f}")
    print(f"[探针] SPU 桶 {len(buckets)} 个，本次跑 {len(picked)} 个，"
          f"标签直方图={'开' if args.tag_hist else '关'}，"
          f"MODE_MERGE_COS={C.MODE_MERGE_COS}")

    ctx = C.RunCtx(run_id=f"probe_spu_{int(time.time())}", week="probe")
    llm.reset_usage()          # 本次探针的花费必须与外面的重跑账分开
    started = time.time()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run_bucket, s, i, ctx, args.tag_hist): s
                   for s, i in picked}
        for done, fut in enumerate(as_completed(futures), 1):
            spu = futures[fut]
            try:
                results.append(fut.result())
            except Exception as exc:                      # noqa: BLE001
                results.append({"spu": spu, "error": repr(exc)})
            if done % 20 == 0 or done == len(picked):
                ok = [r for r in results if "error" not in r]
                print(f"  … {done}/{len(picked)} 桶，累计 {sum(r['groups'] for r in ok)} 个模式")

    elapsed = time.time() - started
    ok = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]
    total_groups = sum(r["groups"] for r in ok)
    total_rows = sum(r["rows"] for r in ok)
    total_batches = sum(r["batches"] for r in ok)

    usage = llm.usage()
    summary = {
        "config": {"tag_hist": args.tag_hist, "buckets_run": len(picked),
                   "min_rows": args.min_rows, "concurrency": args.concurrency,
                   "merge_cos": C.MODE_MERGE_COS},
        "pool": {"unique_facts_U": unique_facts, "fanned_rows_F": fanned,
                 "fan_ratio": round(fanned / unique_facts, 3),
                 "spu_buckets_total": len(buckets)},
        "result": {
            "buckets_ok": len(ok), "buckets_failed": len(failed),
            "rows_processed": total_rows, "stage1_batches": total_batches,
            "cards_v3": total_groups,
            "cards_per_100_evidence": round(100.0 * total_groups / total_rows, 2)
            if total_rows else None,
            "singleton_cards": sum(r["singletons"] for r in ok),
            "singleton_rate": round(sum(r["singletons"] for r in ok) / total_groups, 4)
            if total_groups else None,
            "unclassified": sum(r["unclassified"] for r in ok),
            "dropped": sum(r["dropped"] for r in ok),
            "inherited_rows": sum(r["inherited_rows"] for r in ok),
            "elapsed_s": round(elapsed, 1),
            "llm_calls": ctx.llm_calls, "llm_tokens": ctx.llm_tokens,
            "llm_failed_modes": ctx.llm_failed_modes,
            "usage": usage,
        },
        "buckets": sorted(results, key=lambda r: -(r.get("rows") or 0)),
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    r = summary["result"]
    print(f"\n[探针] 完成：{r['buckets_ok']} 桶成功 / {r['buckets_failed']} 失败")
    print(f"[探针] v3 卡数 = {r['cards_v3']}（每百证据 {r['cards_per_100_evidence']} 张），"
          f"单证据卡占比 {r['singleton_rate']}")
    print(f"[探针] Stage1 批次 {r['stage1_batches']}，LLM 调用 {r['llm_calls']}，"
          f"tokens {r['llm_tokens']}，耗时 {r['elapsed_s']}s")
    print(f"[探针] 明细已写入 {args.out}（未写库）")
    if failed:
        print(f"[探针] 失败桶：{[f['spu'] for f in failed][:10]}")


if __name__ == "__main__":
    main()

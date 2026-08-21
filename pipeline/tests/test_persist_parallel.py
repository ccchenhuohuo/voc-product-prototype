#!/usr/bin/env python3
"""落库并行化的行为测试：真正执行 generate_opportunities 的桶循环。

只打桩到「外部依赖」这一层（数据库、LLM、G4 价值门），生成的编排逻辑
本身照常执行，因此能抓到并行改造引入的运行时错误——源码契约测试抓不到。

验证三件事：
  1. 每个桶各落库一次，id 全部收齐
  2. 落库确实发生在 worker 线程里（不是主线程），即并行生效
  3. 对账不变量 planned == completed + failed + cancelled 仍成立
"""
from __future__ import annotations

import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import config as C, pipeline as P  # noqa: E402


def _pool_row(mid: str, spu: str, seq: int = 0) -> dict:
    """一条最小可路由的电商证据：电商走 R3 契约，必挂事实 SPU，不经 G4。"""
    return {
        "message_id": mid, "seq": seq, "src_line": "电商",
        "source_requires_spu": True, "assign_run_id": "test_parallel",
        "spu_assignments": [
            {"assigned_spu": spu, "assignment_source": "fact"},
        ],
        "tag": f"标签-{spu}", "snippet": f"{spu} 的问题描述",
        "evidence_text": f"{spu} 的问题描述", "full_text": f"{spu} 的问题描述",
        "content": f"{spu} 的问题描述", "star": 2, "category": "支架类",
        "prod_line": "支撑", "tax_path": "使用反馈/结构/松动",
        "tax_domain": "使用反馈", "tax_sub": "结构", "tax_leaf": "松动",
        "is_product": True, "sentiment": "负面", "low_conf": False,
    }


def test_persistence_happens_in_worker_threads(monkeypatch) -> None:
    persist_threads: list[str] = []
    lock = threading.Lock()

    rows = [_pool_row("m1", "SPU-A"), _pool_row("m2", "SPU-B"),
            _pool_row("m3", "SPU-C")]

    monkeypatch.setattr(P.db, "social_structural_gate_counts",
                        lambda *a, **k: {})
    monkeypatch.setattr(P.db, "generation_pool", lambda *a, **k: rows)
    monkeypatch.setattr(P.db, "verify_assign_snapshot", lambda _run_id: len(rows))
    monkeypatch.setattr(
        P.db, "load_assign_snapshot_rows",
        lambda _run_id: [
            {"message_id": row["message_id"], "seq": row["seq"],
             "assigned_spu": row["spu_assignments"][0]["assigned_spu"]}
            for row in rows
        ],
    )
    monkeypatch.setattr(P.db, "clear_terminal_evidence", lambda _run_id: 0)
    monkeypatch.setattr(P.db, "save_unclassified", lambda *a, **k: 0)
    monkeypatch.setattr(P.db, "q", lambda *a, **k: [])

    def fake_split(items, opp_type, info, ctx_, vote=None):
        # 真 split_bucket 会写 Stage1 桶级账本，收尾对账依赖它；桩必须照写，
        # 否则失败的是对账而不是被测的并行逻辑。
        ctx_.metric_update(
            ("stage1", opp_type, "buckets", str(info.get("bucket_key"))),
            bucket=str(info.get("bucket_key")), status="completed", rounds=1,
            input_rows=len(items), accounted_rows=len(items),
            planned_batches=1, completed_batches=1, failed_batches=0,
            cancelled_batches=0, planned_batch_rows=len(items),
            completed_batch_rows=len(items), failed_batch_rows=0,
            cancelled_batch_rows=0)
        return {"groups": [{"mode_name": "松动", "members": [0]}],
                "dropped": [], "unclassified": [], "rounds": 1}

    monkeypatch.setattr(P.stage1, "split_bucket", fake_split)
    monkeypatch.setattr(P.stage1, "merge_similar_modes", lambda groups, ctx: groups)
    monkeypatch.setattr(P, "build_opportunity",
                        lambda items, group, opp_type, info, ctx, hist: {
                            "problem_mode": "松动", "title": "标题"})

    def fake_persist(pairs, week, ctx, *, verbose=False, account=False):
        with lock:
            persist_threads.append(threading.current_thread().name)
        if account:
            ctx.metric_incr(("generation",), completed_persistence=len(pairs))
        return [f"OPP2-{id(pairs):x}"] * len(pairs)

    monkeypatch.setattr(P, "persist_opportunities", fake_persist)

    ctx = C.RunCtx(run_id="test_parallel", week="2026-W33")
    result = P.generate_opportunities("2026-W33", ctx)

    assert len(result["created"]) == 3, result["created"]

    # 三个桶各落一次，且都不在主线程
    assert len(persist_threads) == 3, persist_threads
    assert all(name != "MainThread" for name in persist_threads), persist_threads

    g = ctx.metrics["generation"]
    assert g["planned_buckets"] == (
        g["completed_buckets"] + g["failed_buckets"] + g["cancelled_buckets"])
    assert result["reconciliation"]["complete"] is True

#!/usr/bin/env python3
"""收尾必须刷新 SPU 派生层的静态契约；绝不连库、绝不执行脚本。

派生层（voc_spu / voc_spu_issue / voc_opportunity.n_eff）过去只由旧调度资产
刷新，手工重建路径 rerun_both.sh -> run_generate.py --finalize-only 完全绕过它。
结果是全量重跑之后「老品迭代」卡片页与战略视图读到的仍是上一轮
的物化视图——里面的 opp_id 甚至已经被删除。2026-08-17 实测：机会点表已有
1054 条，voc_spu_issue 却停留在 163 条全部指向已删机会点的旧数据，两页空白。

本测试把「收尾的最后一步是刷新派生层」钉成契约，避免它再次被静默摘掉。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import pipeline  # noqa: E402


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "run_generate.py"
EXPLODE = pathlib.Path(__file__).resolve().parents[1] / "voc_analytics" / "explode.py"


def test_finalize_refreshes_the_derived_layer_after_attaching_evidence() -> None:
    text = SCRIPT.read_text()

    assert "explode" in text, "run_generate 必须导入 explode 才能刷新派生层"
    refresh = text.index("explode.refresh(")
    nn_refresh = text.index("pipeline.refresh_opportunity_neighbors(")
    release = text.index("release = lifecycle.release_to_pm()")

    # 刷新必须发生在证据挂靠与跨源合并之后，否则刷出来的还是半成品。
    attach = text.index("pipeline.attach_evidence(")
    snapshot = text.index("INSERT INTO voc_opp_snapshot")
    assert attach < refresh, "派生层刷新早于证据挂靠，会漏掉本轮新挂的证据"
    assert snapshot < refresh, "派生层刷新应在快照落库之后收口"
    assert refresh < nn_refresh, "最近邻必须在机会点与派生层写入完成后刷新"
    assert nn_refresh < release, "悬空校验失败时不得先改变 PM 可见性"

    # 刷新落在 _finalize 内部，而不是只挂在已退役的调度路径上。
    finalize_start = text.index("def _finalize(")
    finalize_end = text.index("def _parse_args(")
    assert finalize_start < refresh < finalize_end
    assert finalize_start < nn_refresh < finalize_end

    # 统计要进 metrics，否则刷了多少、有没有刷成没人看得见。
    assert "spu_layer=spu_stat" in text
    assert "opp_nn=nn_stat" in text


def test_refresh_helper_calls_the_database_function() -> None:
    text = EXPLODE.read_text()

    assert "voc_refresh_spu_layer()" in text
    for key in ("spu_count", "issue_count", "n_eff_count"):
        assert key in text, f"刷新统计缺少 {key}，收尾日志无法自证"


class _Ctx:
    def __init__(self) -> None:
        self.metrics: dict = {}

    def metric_update(self, path, **values) -> None:
        node = self.metrics
        for part in path:
            node = node.setdefault(part, {})
        node.update(values)


def test_nn_refresh_records_rows_and_zero_orphans(monkeypatch) -> None:
    executed: list[str] = []
    monkeypatch.setattr(
        pipeline.db, "execute", lambda sql, _params=None: executed.append(sql) or 0)
    monkeypatch.setattr(
        pipeline.db, "q",
        lambda _sql, _params=None: [{"nn_rows": 81, "nn_orphan_rows": 0}],
    )
    ctx = _Ctx()

    stat = pipeline.refresh_opportunity_neighbors(ctx)

    assert executed == ["SELECT voc_refresh_opp_nn()"]
    assert stat == {"nn_rows": 81, "nn_orphan_rows": 0}
    assert ctx.metrics["finalize"] == stat


def test_nn_refresh_rejects_any_orphan_after_recording_it(monkeypatch) -> None:
    monkeypatch.setattr(pipeline.db, "execute", lambda _sql, _params=None: 0)
    monkeypatch.setattr(
        pipeline.db, "q",
        lambda _sql, _params=None: [{"nn_rows": 81, "nn_orphan_rows": 2}],
    )
    ctx = _Ctx()

    with pytest.raises(RuntimeError, match="2 条悬空"):
        pipeline.refresh_opportunity_neighbors(ctx)

    assert ctx.metrics["finalize"]["nn_orphan_rows"] == 2

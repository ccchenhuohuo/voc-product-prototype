#!/usr/bin/env python3
"""路线 B 手工链的提案与复活契约；不连库、不调用外部接口。"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_generate.py"
RERUN = ROOT / "scripts" / "rerun_both.sh"
RETIRED_DEPLOY = ROOT / "scripts" / "retired" / "m5_deploy_dagster.sh"


def _load_script():
    spec = importlib.util.spec_from_file_location("route_b_run_generate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Ctx:
    def __init__(self) -> None:
        self.metrics: dict = {}

    def metric_update(self, path, **values) -> None:
        node = self.metrics
        for part in path:
            node = node.setdefault(part, {})
        node.update(values)


def test_proposals_run_before_generation_and_respect_auto_merge_default(
    monkeypatch,
) -> None:
    module = _load_script()
    calls: list[str] = []
    monkeypatch.delenv("VOC_AUTO_MERGE", raising=False)
    monkeypatch.setattr(
        module.execute, "run_auto", lambda _week: calls.append("auto") or {})
    monkeypatch.setattr(
        module.execute, "run",
        lambda week: calls.append(f"proposals:{week}") or {
            "MERGE": 2, "SPLIT": 1, "skipped": 0, "failed": 0,
        },
    )
    ctx = _Ctx()

    stat = module._execute_proposals("2026-W34", ctx)

    assert calls == ["proposals:2026-W34"]
    assert stat["auto_accepted"] == "已关闭"
    assert ctx.metrics["finalize"]["proposal_execution"] == stat

    text = SCRIPT.read_text()
    pre_generate = text.index("_execute_proposals(args.week, ctx)")
    generate = text.index("result = pipeline.generate_opportunities(", pre_generate)
    assert pre_generate < generate


def test_explicit_auto_merge_runs_before_accepted_proposals(monkeypatch) -> None:
    module = _load_script()
    calls: list[str] = []
    monkeypatch.setenv("VOC_AUTO_MERGE", "1")
    monkeypatch.setattr(
        module.execute, "run_auto",
        lambda week: calls.append(f"auto:{week}") or {"auto_accepted": 3},
    )
    monkeypatch.setattr(
        module.execute, "run",
        lambda week: calls.append(f"proposals:{week}") or {
            "MERGE": 3, "SPLIT": 0, "skipped": 0, "failed": 0,
        },
    )

    module._execute_proposals("2026-W34", _Ctx())

    assert calls == ["auto:2026-W34", "proposals:2026-W34"]


def test_revive_runs_after_snapshot_and_before_release(monkeypatch) -> None:
    module = _load_script()
    calls: list[str] = []
    monkeypatch.setattr(
        module.lifecycle, "check_revive",
        lambda week: calls.append(f"revive:{week}") or 4,
    )
    ctx = _Ctx()

    assert module._check_revive("2026-W34", ctx) == 4
    assert calls == ["revive:2026-W34"]
    assert ctx.metrics["finalize"]["revive_proposals"] == 4

    text = SCRIPT.read_text()
    snapshot = text.index("INSERT INTO voc_opp_snapshot")
    revive = text.index("revive_total = _check_revive(")
    release = text.index("release = lifecycle.release_to_pm()")
    finalize_start = text.index("def _finalize(")
    finalize_end = text.index("def _parse_args(")
    assert finalize_start < snapshot < revive < release < finalize_end


def test_full_rebuild_reaches_both_hooks_after_cleanup() -> None:
    text = RERUN.read_text()
    cleanup = text.index("== 清空机会点层")
    generate = text.index("nohup .venv/bin/python -u scripts/run_generate.py", cleanup)
    finalize = text.index("--finalize-only", generate)

    assert cleanup < generate < finalize
    assert "执行 PM 提案落地" in text[cleanup:generate]
    assert "REVIVE 检测" in text[cleanup:generate]


def test_retired_scheduler_cannot_be_loaded_or_redeployed() -> None:
    assert not (ROOT / "voc_analytics" / "definitions.py").exists()
    assert not (ROOT / "scripts" / "m5_deploy_dagster.sh").exists()
    assert RETIRED_DEPLOY.read_text().splitlines()[0] == "exit 1"
    assert "dagster" not in (ROOT / "pyproject.toml").read_text().casefold()
    assert "--dagster" not in (ROOT / "scripts" / "smoke.py").read_text()
    assert "voc_dagster" not in (ROOT / "scripts" / "m0_deploy_pg.sh").read_text()

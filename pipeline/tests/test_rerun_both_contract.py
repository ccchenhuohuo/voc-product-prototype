#!/usr/bin/env python3
"""双生命周期监督脚本的静态契约；绝不执行脚本。"""
from __future__ import annotations

import pathlib


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "rerun_both.sh"


def test_nonzero_generation_is_checked_before_finalize_and_scope_defaults_full() -> None:
    text = SCRIPT.read_text()
    version_guard = text.index("BASH_VERSINFO")
    env_load = text.index(". ./.env")
    circuit_probe = text.index("CIRCUIT_PROBE=")
    destructive_start = text.index("== 停止旧进程")
    failure_guard = text.index("至少一个生命周期生成失败")
    finalize = text.index("== 阶段二：统一收尾")

    assert "wait -n -p" in text
    assert version_guard < destructive_start
    assert env_load < destructive_start
    assert circuit_probe < destructive_start
    assert "${BUCKETS:-0}" in text
    assert "${BUCKETS:-12}" not in text
    assert failure_guard < finalize
    assert "--finalize-only" in text[finalize:]


def test_first_failure_terminates_and_drains_the_sibling() -> None:
    text = SCRIPT.read_text()

    assert "terminate_and_drain" in text
    assert "kill -TERM" in text
    assert "kill -KILL" in text
    assert "trap 'handle_signal INT' INT" in text
    assert "trap 'handle_signal TERM' TERM" in text
    assert "VOC_LLM_CIRCUIT_FILE" in text
    assert "FATAL_PUBLISHER_PID" in text
    assert "wait_publisher_and_drain" in text
    assert "PID_FINALIZE=$!" in text
    assert "i <= 350" in text

#!/usr/bin/env python3
"""战略轴 15 条冻结契约；全程内存 fake，不连库、不调用真实模型。"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import config as C, prompts  # noqa: E402
from voc_analytics.strategy import MaxPairsExceeded, run_strategy  # noqa: E402


def make_card(opp_id: str, *, axis_type: str = "通病", evi: int = 10,
              mode: str | None = None) -> dict:
    return {
        "opp_id": opp_id,
        "opp_type": "老品迭代" if axis_type == "通病" else "新品创新",
        "problem_mode": mode or f"{opp_id}在正常使用条件下出现明确问题",
        "title": f"{opp_id} 标题",
        "prod_line": "支撑",
        "category": "三脚架",
        "evi_total": evi,
    }


def stable_namer(axis_type, cards, prompt):
    del cards, prompt
    return {
        "axis_name": ("锁定机构在承力时持续失效" if axis_type == "通病"
                      else "便携支撑场景需要快速适配"),
        "summary": "成员卡指向同一具体方向。",
    }


def always_same(axis_type, left, right, prompt):
    del axis_type, left, right, prompt
    return True


class MemoryStore:
    def __init__(self, *, cards=(), recalls=None, spu_rows=(), axes=(), members=()):
        self.cards = [dict(row) for row in cards]
        self.recalls = copy.deepcopy(recalls or {})
        self.spu_rows = [dict(row) for row in spu_rows]
        self.verdicts: dict[tuple[str, ...], bool] = {}
        self.axes = [dict(row) for row in axes]
        self.members = [dict(row) for row in members]
        self.cache_writes: list[dict] = []
        self.logs: list[dict] = []
        self.replace_calls = 0
        self.fail_axis_type: str | None = None
        self.checked_generations: list[str] = []

    def assert_spu_view_generation(self, generation):
        self.checked_generations.append(generation)

    def load_cards(self, generation, axis_type):
        opp_type = "老品迭代" if axis_type == "通病" else "新品创新"
        return [
            dict(row) for row in self.cards
            if row["opp_id"].startswith(generation) and row["opp_type"] == opp_type
        ]

    def recall_pairs(self, generation, axis_type, topk, min_cos):
        del generation, topk
        return [
            dict(row) for row in self.recalls.get(axis_type, [])
            if float(row.get("cosine") or row.get("cos") or 0) >= min_cos
        ]

    def load_spu_rows(self, generation, opp_ids):
        wanted = set(opp_ids)
        return [
            dict(row) for row in self.spu_rows
            if row["opp_id"].startswith(generation) and row["opp_id"] in wanted
        ]

    def load_cached_verdicts(self, policy, opp_ids):
        wanted = set(opp_ids)
        return [
            {
                "opp_a": key[0], "opp_b": key[1],
                "mode_hash_a": key[2], "mode_hash_b": key[3],
                "judge_policy": key[4], "same": same,
            }
            for key, same in self.verdicts.items()
            if key[4] == policy and key[0] in wanted and key[1] in wanted
        ]

    def save_pair_verdicts(self, rows):
        for row in rows:
            saved = dict(row)
            self.cache_writes.append(saved)
            key = (
                saved["opp_a"], saved["opp_b"], saved["mode_hash_a"],
                saved["mode_hash_b"], saved["judge_policy"],
            )
            self.verdicts.setdefault(key, bool(saved["same"]))

    def replace_partitions(self, generation, axis_types, axes, members):
        """模拟 DELETE/INSERT 同事务；第二分区故障时恢复逐行快照。"""
        self.replace_calls += 1
        before_axes = copy.deepcopy(self.axes)
        before_members = copy.deepcopy(self.members)
        try:
            for axis_type in axis_types:
                self.axes = [
                    row for row in self.axes
                    if not (row.get("generation") == generation
                            and row.get("axis_type") == axis_type)
                ]
                self.members = [
                    row for row in self.members
                    if not (row.get("generation") == generation
                            and row.get("axis_type") == axis_type)
                ]
                self.axes.extend(
                    copy.deepcopy([row for row in axes if row["axis_type"] == axis_type])
                )
                self.members.extend(
                    copy.deepcopy([row for row in members if row["axis_type"] == axis_type])
                )
                if self.fail_axis_type == axis_type:
                    raise RuntimeError(f"fault inside {axis_type} insert")
        except BaseException:
            self.axes = before_axes
            self.members = before_members
            raise

    def save_run_log(self, ctx, status, started_at, error=None):
        self.logs.append({
            "run_id": ctx.run_id,
            "status": status,
            "started_at": started_at,
            "error": type(error).__name__ if error else None,
            "metrics": copy.deepcopy(ctx.metrics),
        })


def pair(a: str, b: str, cosine: float = 0.9) -> dict:
    return {"opp_a": a, "opp_b": b, "cosine": cosine}


def spu(opp_id: str, value: str, evi: int) -> dict:
    return {"opp_id": opp_id, "spu": value, "evi_count": evi}


def test_01_greedy_is_deterministic_and_axis_id_is_stable():
    cards = [make_card("OPP2-A", evi=8), make_card("OPP2-B", evi=5)]
    store = MemoryStore(
        cards=cards,
        recalls={"通病": [pair("OPP2-A", "OPP2-B")]},
        spu_rows=[spu("OPP2-A", "SPU-A", 8), spu("OPP2-B", "SPU-B", 5)],
    )
    first = run_strategy(
        axis_type="通病", store=store, judge=always_same, namer=stable_namer)
    second = run_strategy(
        axis_type="通病", store=store, judge=always_same, namer=stable_namer)

    assert first["clusters"] == second["clusters"]
    assert [row["axis_id"] for row in first["axes"]] == [
        row["axis_id"] for row in second["axes"]
    ]


def test_02_greedy_leader_is_non_transitive():
    ids = ["OPP2-A", "OPP2-B", "OPP2-C", "OPP2-D"]
    store = MemoryStore(
        cards=[make_card(value, evi=40 - index * 10)
               for index, value in enumerate(ids)],
        recalls={"通病": [
            pair("OPP2-A", "OPP2-B", 0.8),
            pair("OPP2-B", "OPP2-C", 0.8),
            pair("OPP2-A", "OPP2-C", 0.3),
            pair("OPP2-C", "OPP2-D", 0.8),
        ]},
        spu_rows=[spu(value, f"SPU-{value[-1]}", 2) for value in ids],
    )
    result = run_strategy(
        axis_type="通病", store=store, judge=always_same, namer=stable_namer)

    assert result["clusters"]["通病"] == [
        ["OPP2-A", "OPP2-B"], ["OPP2-C", "OPP2-D"],
    ]


def test_03_defect_and_enhancement_redline_stays_split():
    defect = "伸缩杆全高展开后末端持续晃动"
    enhancement = "伸缩杆需要增加最大伸展高度"
    cards = [
        make_card("OPP2-A", evi=9, mode=defect),
        make_card("OPP2-B", evi=8, mode=enhancement),
    ]
    seen = []

    def redline_judge(axis_type, left, right, prompt):
        seen.append((left.problem_mode, right.problem_mode, prompt))
        return False

    store = MemoryStore(
        cards=cards, recalls={"通病": [pair("OPP2-A", "OPP2-B")]})
    result = run_strategy(
        axis_type="通病", store=store, judge=redline_judge, namer=stable_namer)

    assert result["clusters"]["通病"] == [["OPP2-A"], ["OPP2-B"]]
    assert seen and defect in seen[0][2] and enhancement in seen[0][2]


def test_04_second_run_uses_lazy_verdict_cache_only_for_evaluated_pairs():
    cards = [make_card("OPP2-A", evi=30), make_card("OPP2-B", evi=20),
             make_card("OPP2-C", evi=10)]
    calls = []

    def judge(axis_type, left, right, prompt):
        del axis_type, prompt
        calls.append((left.opp_id, right.opp_id))
        return right.opp_id == "OPP2-B"

    store = MemoryStore(
        cards=cards,
        recalls={"通病": [pair("OPP2-A", "OPP2-B"),
                            pair("OPP2-A", "OPP2-C"),
                            pair("OPP2-B", "OPP2-C")]},
        spu_rows=[spu("OPP2-A", "SPU-A", 3), spu("OPP2-B", "SPU-B", 2)],
    )
    first = run_strategy(
        axis_type="通病", store=store, judge=judge, namer=stable_namer)
    first_calls = len(calls)
    second = run_strategy(
        axis_type="通病", store=store, judge=judge, namer=stable_namer)

    assert first["metrics"]["pairs_evaluated"] == 2 < first["metrics"]["pairs_recalled"]
    assert first_calls == 2
    assert second["metrics"]["llm_judged"] == 0
    assert second["metrics"]["verdict_cache_hits"] == second["metrics"]["pairs_evaluated"]
    assert len(calls) == first_calls


def test_05_problem_mode_change_invalidates_related_cache_key():
    store = MemoryStore(
        cards=[make_card("OPP2-A", evi=9), make_card("OPP2-B", evi=8)],
        recalls={"通病": [pair("OPP2-A", "OPP2-B")]},
        spu_rows=[spu("OPP2-A", "SPU-A", 2), spu("OPP2-B", "SPU-B", 2)],
    )
    run_strategy(axis_type="通病", store=store, judge=always_same,
                 namer=stable_namer)
    store.cards[1]["problem_mode"] = "锁定旋钮在低负载下也会持续回退松脱"
    second = run_strategy(axis_type="通病", store=store, judge=always_same,
                          namer=stable_namer)

    assert second["metrics"]["llm_judged"] == 1
    assert second["metrics"]["verdict_cache_hits"] == 0
    assert len(store.verdicts) == 2


def test_uncached_candidates_for_one_leader_are_judged_concurrently():
    barrier = threading.Barrier(2)

    def synchronized_judge(axis_type, left, right, prompt):
        del axis_type, left, right, prompt
        barrier.wait(timeout=1)
        return True

    store = MemoryStore(
        cards=[make_card("OPP2-A", evi=30), make_card("OPP2-B", evi=20),
               make_card("OPP2-C", evi=10)],
        recalls={"通病": [pair("OPP2-A", "OPP2-B"),
                            pair("OPP2-A", "OPP2-C")]},
        spu_rows=[spu("OPP2-A", "SPU-A", 3), spu("OPP2-B", "SPU-B", 2),
                  spu("OPP2-C", "SPU-C", 1)],
    )

    result = run_strategy(
        axis_type="通病", store=store, judge=synchronized_judge,
        namer=stable_namer)

    assert result["clusters"]["通病"] == [["OPP2-A", "OPP2-B", "OPP2-C"]]
    assert result["metrics"]["llm_judged"] == 2
    assert result["metrics"]["llm_failed"] == 0


def test_06_max_pairs_aborts_without_any_table_write_and_cli_is_nonzero(
        monkeypatch, capsys):
    store = MemoryStore(
        cards=[make_card("OPP2-A", evi=3), make_card("OPP2-B", evi=2),
               make_card("OPP2-C", evi=1)],
        recalls={"通病": [pair("OPP2-A", "OPP2-B"),
                            pair("OPP2-A", "OPP2-C")]},
        axes=[{"axis_id": "old", "generation": "OPP2-", "axis_type": "通病"}],
    )
    before = copy.deepcopy(store.axes)
    monkeypatch.setattr(C, "STRATEGY_MAX_PAIRS", 1)
    with pytest.raises(MaxPairsExceeded) as caught:
        run_strategy(axis_type="通病", store=store, judge=always_same,
                     namer=stable_namer)
    assert caught.value.actual == 2
    assert store.replace_calls == 0 and store.cache_writes == [] and store.logs == []
    assert store.axes == before

    script = Path(__file__).resolve().parents[1] / "scripts" / "run_strategy.py"
    spec = importlib.util.spec_from_file_location("strategy_cli_for_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def explode(**kwargs):
        del kwargs
        raise MaxPairsExceeded(2, 1)

    monkeypatch.setattr(module, "run_strategy", explode)
    assert module.main(["--type", "通病"]) != 0
    assert "实际 2 对" in capsys.readouterr().err


def test_07_singletons_and_one_spu_problem_clusters_are_not_admitted():
    store = MemoryStore(
        cards=[make_card("OPP2-A", evi=6), make_card("OPP2-B", evi=5),
               make_card("OPP2-C", evi=4)],
        recalls={"通病": [pair("OPP2-A", "OPP2-B")]},
        spu_rows=[
            spu("OPP2-A", "SPU-ONE", 6), spu("OPP2-B", "SPU-ONE", 5),
            spu("OPP2-C", "SPU-X", 2), spu("OPP2-C", "SPU-Y", 2),
        ],
    )
    result = run_strategy(
        axis_type="通病", store=store, judge=always_same, namer=stable_namer)

    assert result["clusters"]["通病"] == [["OPP2-A", "OPP2-B"], ["OPP2-C"]]
    assert result["axes"] == [] and result["members"] == []


def test_08_n_eff_uses_weighted_90_10_distribution_and_equals_1_22():
    store = MemoryStore(
        cards=[make_card("OPP2-A", evi=90), make_card("OPP2-B", evi=10)],
        recalls={"通病": [pair("OPP2-A", "OPP2-B")]},
        spu_rows=[spu("OPP2-A", "SPU-90", 90), spu("OPP2-B", "SPU-10", 10)],
    )
    result = run_strategy(
        axis_type="通病", store=store, judge=always_same, namer=stable_namer)

    assert result["axes"][0]["n_eff"] == pytest.approx(10000 / 8200)
    assert round(result["axes"][0]["n_eff"], 2) == 1.22


def test_09_failure_inside_second_partition_insert_rolls_back_old_rows_exactly():
    old_axes = [
        {"axis_id": "old-p", "generation": "OPP2-", "axis_type": "通病", "v": 1},
        {"axis_id": "old-t", "generation": "OPP2-", "axis_type": "诉求", "v": 2},
    ]
    old_members = [
        {"axis_id": "old-p", "generation": "OPP2-", "axis_type": "通病", "opp_id": "x"},
        {"axis_id": "old-t", "generation": "OPP2-", "axis_type": "诉求", "opp_id": "y"},
    ]
    cards = [
        make_card("OPP2-A", axis_type="通病", evi=8),
        make_card("OPP2-B", axis_type="通病", evi=7),
        make_card("OPP2-C", axis_type="诉求", evi=6),
        make_card("OPP2-D", axis_type="诉求", evi=5),
    ]
    store = MemoryStore(
        cards=cards,
        recalls={"通病": [pair("OPP2-A", "OPP2-B")],
                 "诉求": [pair("OPP2-C", "OPP2-D")]},
        spu_rows=[spu("OPP2-A", "SPU-A", 4), spu("OPP2-B", "SPU-B", 4)],
        axes=old_axes, members=old_members,
    )
    store.fail_axis_type = "诉求"
    before_axes, before_members = copy.deepcopy(store.axes), copy.deepcopy(store.members)

    with pytest.raises(RuntimeError, match="inside 诉求 insert"):
        run_strategy(axis_type="all", store=store, judge=always_same,
                     namer=stable_namer)
    assert store.axes == before_axes
    assert store.members == before_members


def test_10_generic_name_twice_falls_back_to_leader_problem_mode():
    leader_mode = "磁吸支架在快速拆装时缺少通用生态接口"
    store = MemoryStore(
        cards=[make_card("OPP2-A", axis_type="诉求", evi=8, mode=leader_mode),
               make_card("OPP2-B", axis_type="诉求", evi=4)],
        recalls={"诉求": [pair("OPP2-A", "OPP2-B")]},
    )
    calls = []

    def generic_namer(axis_type, cards, prompt):
        del axis_type, cards, prompt
        calls.append(1)
        return {"axis_name": "用户诉求", "summary": "无效分类名"}

    result = run_strategy(
        axis_type="诉求", store=store, judge=always_same, namer=generic_namer)

    assert len(calls) == 2
    assert result["axes"][0]["axis_name"] == leader_mode
    assert result["metrics"]["naming_invalid"] == 2


def test_11_generation_filter_excludes_shadow_generation_members():
    store = MemoryStore(
        cards=[
            make_card("OPP-OLD-A", evi=20), make_card("OPP-OLD-B", evi=19),
            make_card("OPP2-NEW-A", evi=10), make_card("OPP2-NEW-B", evi=9),
        ],
        recalls={"通病": [
            pair("OPP-OLD-A", "OPP2-NEW-A"),
            pair("OPP2-NEW-A", "OPP2-NEW-B"),
        ]},
        spu_rows=[
            spu("OPP-OLD-A", "SPU-OLD", 20),
            spu("OPP2-NEW-A", "SPU-A", 5), spu("OPP2-NEW-B", "SPU-B", 4),
        ],
    )
    result = run_strategy(
        axis_type="通病", generation="OPP2-", store=store,
        judge=always_same, namer=stable_namer)

    assert result["members"]
    assert all(row["opp_id"].startswith("OPP2-") for row in result["members"])
    assert store.checked_generations == ["OPP2-"]


def test_12_single_type_replacement_preserves_other_partition_row_for_row():
    theme_axis = {"axis_id": "theme-old", "generation": "OPP2-",
                  "axis_type": "诉求", "axis_name": "原诉求"}
    theme_member = {"axis_id": "theme-old", "generation": "OPP2-",
                    "axis_type": "诉求", "opp_id": "OPP2-T"}
    store = MemoryStore(
        cards=[make_card("OPP2-A", evi=5), make_card("OPP2-B", evi=4)],
        recalls={"通病": [pair("OPP2-A", "OPP2-B")]},
        spu_rows=[spu("OPP2-A", "SPU-A", 3), spu("OPP2-B", "SPU-B", 2)],
        axes=[theme_axis], members=[theme_member],
    )
    run_strategy(axis_type="通病", store=store, judge=always_same,
                 namer=stable_namer)

    assert [row for row in store.axes if row["axis_type"] == "诉求"] == [theme_axis]
    assert [row for row in store.members if row["axis_type"] == "诉求"] == [theme_member]


def test_13_judge_failure_is_not_cached_as_false_verdict():
    store = MemoryStore(
        cards=[make_card("OPP2-A", evi=5), make_card("OPP2-B", evi=4)],
        recalls={"通病": [pair("OPP2-A", "OPP2-B")]},
    )

    def failed_judge(axis_type, left, right, prompt):
        del axis_type, left, right, prompt
        raise RuntimeError("fake retry exhausted")

    result = run_strategy(
        axis_type="通病", store=store, judge=failed_judge, namer=stable_namer)

    assert store.verdicts == {} and store.cache_writes == []
    assert result["metrics"]["llm_judged"] == 1
    assert result["metrics"]["llm_failed"] == 1
    assert result["clusters"]["通病"] == [["OPP2-A"], ["OPP2-B"]]


def test_14_prompt_change_changes_judge_policy_and_misses_old_cache(monkeypatch):
    store = MemoryStore(
        cards=[make_card("OPP2-A", evi=5), make_card("OPP2-B", evi=4)],
        recalls={"通病": [pair("OPP2-A", "OPP2-B")]},
        spu_rows=[spu("OPP2-A", "SPU-A", 2), spu("OPP2-B", "SPU-B", 2)],
    )
    first = run_strategy(
        axis_type="通病", store=store, judge=always_same, namer=stable_namer)
    monkeypatch.setattr(
        prompts, "STRATEGY_AXIS_SAME", prompts.STRATEGY_AXIS_SAME + "\n策略版本二")
    second = run_strategy(
        axis_type="通病", store=store, judge=always_same, namer=stable_namer)

    assert first["metrics"]["judge_policies"]["通病"] != second["metrics"]["judge_policies"]["通病"]
    assert second["metrics"]["llm_judged"] == 1
    assert second["metrics"]["verdict_cache_hits"] == 0
    assert len({key[4] for key in store.verdicts}) == 2


def test_15_strategy_migration_grants_all_three_roles_without_cache_read_leak():
    sql = (Path(__file__).resolve().parents[1]
           / "sql" / "034_strategy_axis.sql").read_text(encoding="utf-8")
    compact = " ".join(sql.split()).lower()

    assert "grant select, insert, delete on public.voc_strategy_axis, public.voc_strategy_axis_member to voc_writer" in compact
    assert "grant select, insert on public.voc_strategy_pair_verdict to voc_writer" in compact
    assert "grant select on public.voc_strategy_axis, public.voc_strategy_axis_member to voc_human, voc_reader" in compact
    assert "revoke all on public.voc_strategy_pair_verdict from voc_human, voc_reader" in compact

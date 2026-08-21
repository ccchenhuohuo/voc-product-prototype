"""战略层：跨 SPU 通病轴与核心诉求主题的独立派生管道。

输入只读当前发布代次的有效机会点；召回使用库内 ``mode_vec``，聚类保留
Stage1 母本的贪心 leader 与惰性 LLM 判定。轴与成员按代次、类型在单事务内
替换，判定缓存则以问题文本哈希和提示词/模型策略哈希自动失效。
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Sequence

from psycopg.types.json import Jsonb

from . import config as C, db, llm, prompts
from .stages import validate


VALID_GENERATIONS = frozenset(("OPP-", "OPP2-"))
VALID_AXIS_TYPES = frozenset(("通病", "诉求"))
AXIS_TO_OPP_TYPE = {"通病": "老品迭代", "诉求": "新品创新"}
TYPE_ORDER = ("通病", "诉求")


class StrategyError(RuntimeError):
    """战略层可诊断错误。"""


class MaxPairsExceeded(StrategyError):
    """去重召回对数超过花费保险丝。"""

    def __init__(self, actual: int, limit: int):
        self.actual = int(actual)
        self.limit = int(limit)
        super().__init__(f"战略召回对数 {self.actual} 超过上限 {self.limit}")


class GenerationMismatch(StrategyError):
    """发布代次与 ``voc_spu_issue`` 当前兼容视图指向不一致。"""


@dataclass(frozen=True)
class Card:
    opp_id: str
    opp_type: str
    problem_mode: str
    title: str
    prod_line: str | None
    category: str | None
    evi_total: int

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "Card":
        return cls(
            opp_id=str(row["opp_id"]),
            opp_type=str(row["opp_type"]),
            problem_mode=str(row.get("problem_mode") or "").strip(),
            title=str(row.get("title") or row.get("problem_mode") or "").strip(),
            prod_line=(str(row["prod_line"]).strip()
                       if row.get("prod_line") else None),
            category=(str(row["category"]).strip()
                      if row.get("category") else None),
            evi_total=int(row.get("evi_total") or 0),
        )


Judge = Callable[[str, Card, Card, str], bool]
Namer = Callable[[str, Sequence[Card], str], Mapping[str, object]]


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def judge_policy(axis_type: str, *, template: str | None = None,
                 model: str | None = None) -> str:
    """提示词模板与模型共同决定缓存命中域。"""
    if axis_type not in VALID_AXIS_TYPES:
        raise ValueError(f"未知轴类型：{axis_type}")
    source = template if template is not None else (
        prompts.STRATEGY_AXIS_SAME
        if axis_type == "通病" else prompts.STRATEGY_THEME_SAME
    )
    return _md5(f"{source}\nmodel={model or C.CHAT_MODEL}")


def axis_identity(generation: str, axis_type: str,
                  opp_ids: Iterable[str]) -> str:
    members = ",".join(sorted(set(opp_ids)))
    return _md5(f"{generation}|{axis_type}|{members}")


def effective_spu_count(weights: Iterable[int]) -> float | None:
    positive = [int(weight) for weight in weights if int(weight) > 0]
    if not positive:
        return None
    total = sum(positive)
    return total * total / sum(weight * weight for weight in positive)


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _normalized_pairs(rows: Iterable[Mapping[str, object]],
                      card_ids: set[str]) -> dict[tuple[str, str], float]:
    pairs: dict[tuple[str, str], float] = {}
    for row in rows:
        left = str(row.get("opp_a") or "")
        right = str(row.get("opp_b") or "")
        if not left or not right or left == right:
            continue
        key = _pair(left, right)
        if key[0] not in card_ids or key[1] not in card_ids:
            continue
        similarity = float(row.get("cosine") or row.get("cos") or 0.0)
        if key not in pairs or similarity > pairs[key]:
            pairs[key] = similarity
    return dict(sorted(pairs.items()))


class PostgresStrategyStore:
    """生产数据库适配；测试使用内存替身，不会触达此类。"""

    def assert_spu_view_generation(self, generation: str) -> None:
        expected = "voc_spu_issue_v3" if generation == "OPP2-" else "voc_spu_issue_v2"
        definition = str(db.q1(
            "SELECT pg_get_viewdef('voc_spu_issue'::regclass, true)"
        ) or "")
        if expected not in definition:
            raise GenerationMismatch(
                f"STRATEGY_GENERATION={generation}，但 voc_spu_issue 未指向 {expected}"
            )

    def load_cards(self, generation: str, axis_type: str) -> list[dict]:
        pattern = generation + "%"
        return db.q(
            """
            WITH issue_weights AS (
              SELECT i.opp_id, COALESCE(sum(i.evi_count), 0)::int AS evi_total
                FROM voc_spu_issue i
                JOIN voc_opportunity source_opp
                  ON source_opp.opp_id = i.opp_id
               WHERE source_opp.opp_id LIKE %s
               GROUP BY i.opp_id
            ), evidence_weights AS (
              SELECT oe.opp_id, count(*)::int AS evi_total
                FROM voc_opp_evidence oe
               GROUP BY oe.opp_id
            )
            SELECT o.opp_id, o.opp_type, o.problem_mode, o.title,
                   o.prod_line, o.category,
                   CASE WHEN o.opp_type = '老品迭代'
                        THEN COALESCE(i.evi_total, 0)
                        ELSE COALESCE(e.evi_total, 0)
                    END::int AS evi_total
              FROM voc_opportunity o
              LEFT JOIN issue_weights i ON i.opp_id = o.opp_id
              LEFT JOIN evidence_weights e ON e.opp_id = o.opp_id
             WHERE o.opp_type = %s
               AND o.classification_state = '确定'
               AND o.merged_into IS NULL
               AND o.mode_vec IS NOT NULL
               AND o.opp_id LIKE %s
             ORDER BY evi_total DESC, o.opp_id ASC
            """,
            [pattern, AXIS_TO_OPP_TYPE[axis_type], pattern],
        )

    def recall_pairs(self, generation: str, axis_type: str,
                     topk: int, min_cos: float) -> list[dict]:
        pattern = generation + "%"
        return db.q(
            """
            WITH active AS MATERIALIZED (
              SELECT o.opp_id, o.mode_vec
                FROM voc_opportunity o
               WHERE o.opp_type = %s
                 AND o.classification_state = '确定'
                 AND o.merged_into IS NULL
                 AND o.mode_vec IS NOT NULL
                 AND o.opp_id LIKE %s
            ), recalled AS (
              SELECT LEAST(a.opp_id, neighbor.opp_id) AS opp_a,
                     GREATEST(a.opp_id, neighbor.opp_id) AS opp_b,
                     (1.0 - neighbor.distance)::double precision AS cosine
                FROM active a
               CROSS JOIN LATERAL (
                 SELECT b.opp_id,
                        (b.mode_vec <=> a.mode_vec)::double precision AS distance
                   FROM active b
                  WHERE b.opp_id <> a.opp_id
                  ORDER BY b.mode_vec <=> a.mode_vec, b.opp_id
                  LIMIT %s
               ) neighbor
               WHERE 1.0 - neighbor.distance >= %s
            )
            SELECT opp_a, opp_b, max(cosine)::double precision AS cosine
              FROM recalled
             GROUP BY opp_a, opp_b
             ORDER BY opp_a, opp_b
            """,
            [AXIS_TO_OPP_TYPE[axis_type], pattern, int(topk), float(min_cos)],
        )

    def load_spu_rows(self, generation: str,
                      opp_ids: Sequence[str]) -> list[dict]:
        if not opp_ids:
            return []
        return db.q(
            """
            SELECT i.opp_id, i.spu, i.evi_count::int AS evi_count
              FROM voc_spu_issue i
              JOIN voc_opportunity o ON o.opp_id = i.opp_id
             WHERE o.opp_id LIKE %s
               AND i.opp_id = ANY(%s)
             ORDER BY i.opp_id, i.evi_count DESC, i.spu ASC
            """,
            [generation + "%", list(opp_ids)],
        )

    def load_cached_verdicts(self, policy: str,
                             opp_ids: Sequence[str]) -> list[dict]:
        if not opp_ids:
            return []
        return db.q(
            """
            SELECT opp_a, opp_b, mode_hash_a, mode_hash_b,
                   judge_policy, same
              FROM voc_strategy_pair_verdict
             WHERE judge_policy = %s
               AND opp_a = ANY(%s)
               AND opp_b = ANY(%s)
             ORDER BY opp_a, opp_b, mode_hash_a, mode_hash_b
            """,
            [policy, list(opp_ids), list(opp_ids)],
        )

    def save_pair_verdicts(self, rows: Sequence[Mapping[str, object]]) -> None:
        if not rows:
            return
        with db.conn() as connection:
            connection.cursor().executemany(
                """
                INSERT INTO voc_strategy_pair_verdict
                       (opp_a, opp_b, mode_hash_a, mode_hash_b,
                        judge_policy, same)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (opp_a, opp_b, mode_hash_a, mode_hash_b, judge_policy)
                DO NOTHING
                """,
                [
                    (row["opp_a"], row["opp_b"], row["mode_hash_a"],
                     row["mode_hash_b"], row["judge_policy"], row["same"])
                    for row in rows
                ],
            )

    def replace_partitions(self, generation: str, axis_types: Sequence[str],
                           axes: Sequence[Mapping[str, object]],
                           members: Sequence[Mapping[str, object]]) -> None:
        """所选分区的 DELETE 与全部 INSERT 共用一个数据库事务。"""
        selected = list(axis_types)
        axis_rows = sorted(
            (row for row in axes if row["axis_type"] in selected),
            key=lambda row: (TYPE_ORDER.index(str(row["axis_type"])),
                             str(row["axis_id"])),
        )
        member_rows = sorted(
            (row for row in members if row["axis_type"] in selected),
            key=lambda row: (TYPE_ORDER.index(str(row["axis_type"])),
                             str(row["axis_id"]),
                             0 if row["role"] == "leader" else 1,
                             str(row["opp_id"])),
        )
        with db.conn() as connection:
            connection.execute(
                "DELETE FROM voc_strategy_axis "
                "WHERE generation = %s AND axis_type = ANY(%s)",
                [generation, selected],
            )
            if axis_rows:
                connection.cursor().executemany(
                    """
                    INSERT INTO voc_strategy_axis
                           (axis_id, generation, axis_type, axis_name, summary,
                            n_cards, n_spu, n_eff, evi_total, prod_lines,
                            categories, top_spus, run_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (row["axis_id"], row["generation"], row["axis_type"],
                         row["axis_name"], row.get("summary"), row["n_cards"],
                         row.get("n_spu"), row.get("n_eff"), row["evi_total"],
                         row["prod_lines"], row["categories"],
                         Jsonb(row["top_spus"]) if row.get("top_spus") is not None else None,
                         row["run_id"])
                        for row in axis_rows
                    ],
                )
            if member_rows:
                connection.cursor().executemany(
                    """
                    INSERT INTO voc_strategy_axis_member
                           (axis_id, generation, axis_type, opp_id, role,
                            cos_to_leader, link_spu)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (row["axis_id"], row["generation"], row["axis_type"],
                         row["opp_id"], row["role"], row["cos_to_leader"],
                         row.get("link_spu"))
                        for row in member_rows
                    ],
                )

    def save_run_log(self, ctx: C.RunCtx, status: str, started_at: datetime,
                     error: BaseException | None = None) -> None:
        metrics = ctx.metrics.get("strategy", {})
        db.save_run_log(
            ctx,
            "strategy",
            status=status,
            matched_rows=metrics.get("pairs_evaluated"),
            exported_rows=metrics.get("members_written"),
            batch_count=metrics.get("axes_written"),
            error_message=(f"{type(error).__name__}: {error}"[:1000]
                           if error else None),
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )


def _default_judge(axis_type: str, left: Card, right: Card,
                   prompt: str) -> bool:
    del axis_type, left, right
    obj, _ = llm.chat_json(prompt, max_tokens=200, required=["same"])
    return obj.get("same") is True


def _default_namer(axis_type: str, cards: Sequence[Card],
                   prompt: str) -> Mapping[str, object]:
    del axis_type, cards
    obj, _ = llm.chat_json(prompt, max_tokens=300, required=["axis_name"])
    return obj


def _usage_delta(before: Mapping[str, object],
                 after: Mapping[str, object]) -> dict:
    models: dict[str, dict[str, int]] = {}
    before_models = before.get("by_model") if isinstance(before, Mapping) else {}
    after_models = after.get("by_model") if isinstance(after, Mapping) else {}
    before_models = before_models if isinstance(before_models, Mapping) else {}
    after_models = after_models if isinstance(after_models, Mapping) else {}
    for model in sorted(set(before_models) | set(after_models)):
        start = before_models.get(model) or {}
        end = after_models.get(model) or {}
        entry = {
            key: max(int(end.get(key, 0)) - int(start.get(key, 0)), 0)
            for key in ("calls", "input", "output", "tokens")
        }
        if any(entry.values()):
            models[str(model)] = entry
    return {
        "calls": max(int(after.get("calls", 0)) - int(before.get("calls", 0)), 0),
        "tokens": max(int(after.get("tokens", 0)) - int(before.get("tokens", 0)), 0),
        "by_model": models,
    }


def _verdict_key(left: Card, right: Card, policy: str) -> tuple[str, ...]:
    a, b = (left, right) if left.opp_id < right.opp_id else (right, left)
    return (a.opp_id, b.opp_id, _md5(a.problem_mode),
            _md5(b.problem_mode), policy)


def _cache_index(rows: Iterable[Mapping[str, object]]) -> dict[tuple[str, ...], bool]:
    return {
        (str(row["opp_a"]), str(row["opp_b"]),
         str(row["mode_hash_a"]), str(row["mode_hash_b"]),
         str(row["judge_policy"])): bool(row["same"])
        for row in rows
    }


def _greedy_clusters(
    cards: Sequence[Card],
    pairs: Mapping[tuple[str, str], float],
    *,
    axis_type: str,
    policy: str,
    cache: dict[tuple[str, ...], bool],
    judge: Judge,
    new_verdicts: list[dict],
    metrics: dict[str, int],
) -> list[list[Card]]:
    """镜像 Stage1：leader 只吸收仍未归并的后续卡，判定保持惰性。"""
    ordered = sorted(cards, key=lambda card: (-card.evi_total, card.opp_id))
    merged: set[str] = set()
    clusters: list[list[Card]] = []
    template = (prompts.STRATEGY_AXIS_SAME
                if axis_type == "通病" else prompts.STRATEGY_THEME_SAME)

    for index, leader in enumerate(ordered):
        if leader.opp_id in merged:
            continue
        cluster = [leader]
        for candidate in ordered[index + 1:]:
            if candidate.opp_id in merged:
                continue
            pair_key = _pair(leader.opp_id, candidate.opp_id)
            if pair_key not in pairs:
                continue
            metrics["pairs_evaluated"] += 1
            key = _verdict_key(leader, candidate, policy)
            if key in cache:
                same = cache[key]
                metrics["verdict_cache_hits"] += 1
            else:
                metrics["llm_judged"] += 1
                prompt = template.format(
                    a=leader.problem_mode, b=candidate.problem_mode)
                try:
                    same = bool(judge(axis_type, leader, candidate, prompt))
                except Exception:  # 判定失败只影响本对，不落正常缓存
                    metrics["llm_failed"] += 1
                    same = False
                else:
                    verdict = {
                        "opp_a": key[0], "opp_b": key[1],
                        "mode_hash_a": key[2], "mode_hash_b": key[3],
                        "judge_policy": key[4], "same": same,
                    }
                    cache[key] = same
                    new_verdicts.append(verdict)
            if same:
                cluster.append(candidate)
                merged.add(candidate.opp_id)
        clusters.append(cluster)
    return clusters


def _axis_name(axis_type: str, cards: Sequence[Card], namer: Namer,
               metrics: dict[str, int]) -> tuple[str, str | None]:
    distribution = Counter(
        card.prod_line for card in cards if card.prod_line
    )
    prompt = prompts.STRATEGY_AXIS_NAME.format(
        axis_type=axis_type,
        modes="\n".join(f"- {card.problem_mode}" for card in cards),
        prod_lines=(json.dumps(dict(sorted(distribution.items())), ensure_ascii=False)
                    if distribution else "—"),
    )
    for _attempt in range(2):
        try:
            obj = namer(axis_type, cards, prompt)
        except Exception:
            metrics["naming_failed"] += 1
            continue
        name = str(obj.get("axis_name") or "").strip()
        generic_errors = validate.check_problem_mode({"problem_mode": name})
        invalid = (not name or len(name) > 24
                   or any("分类名" in error for error in generic_errors))
        if invalid:
            metrics["naming_invalid"] += 1
            continue
        summary = str(obj.get("summary") or "").strip() or None
        if summary is not None and len(summary) > 60:
            summary = None
            metrics["summary_dropped"] += 1
        return name, summary
    return cards[0].problem_mode, None


def _spu_indexes(rows: Sequence[Mapping[str, object]]) -> tuple[
        dict[str, list[tuple[str, int]]], dict[str, str | None]]:
    by_opp: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in rows:
        by_opp[str(row["opp_id"])].append(
            (str(row["spu"]), int(row.get("evi_count") or 0)))
    links: dict[str, str | None] = {}
    for opp_id, values in by_opp.items():
        ranked = sorted(values, key=lambda item: (-item[1], item[0]))
        links[opp_id] = ranked[0][0] if ranked else None
    return dict(by_opp), links


def _materialize_axes(
    generation: str,
    axis_type: str,
    clusters: Sequence[Sequence[Card]],
    pairs: Mapping[tuple[str, str], float],
    spu_rows: Sequence[Mapping[str, object]],
    *,
    run_id: str,
    namer: Namer,
    metrics: dict[str, int],
) -> tuple[list[dict], list[dict]]:
    by_opp, link_spu = _spu_indexes(spu_rows)
    axes: list[dict] = []
    members: list[dict] = []

    for cluster in clusters:
        cards = list(cluster)
        if len(cards) < 2:
            continue

        per_spu: Counter[str] = Counter()
        if axis_type == "通病":
            for card in cards:
                for spu, weight in by_opp.get(card.opp_id, []):
                    per_spu[spu] += weight
        n_spu = len(per_spu) if axis_type == "通病" else None
        n_eff = (effective_spu_count(per_spu.values())
                 if axis_type == "通病" else None)

        # 任务书冻结为每个多卡簇命名；即使随后因 n_spu 不达标不落库，
        # 也不把实现悄悄改成另一套成本口径。
        name, summary = _axis_name(axis_type, cards, namer, metrics)
        admitted = (
            n_spu is not None and n_spu >= C.STRATEGY_MIN_SPU
            if axis_type == "通病"
            else len(cards) >= C.STRATEGY_MIN_CARDS
        )
        if not admitted:
            continue

        axis_id = axis_identity(
            generation, axis_type, (card.opp_id for card in cards))
        top_spus = (
            [{"spu": spu, "evi": weight}
             for spu, weight in sorted(
                 per_spu.items(), key=lambda item: (-item[1], item[0])
             )[:C.STRATEGY_TOP_SPUS]]
            if axis_type == "通病" else None
        )
        axes.append({
            "axis_id": axis_id,
            "generation": generation,
            "axis_type": axis_type,
            "axis_name": name,
            "summary": summary,
            "n_cards": len(cards),
            "n_spu": n_spu,
            "n_eff": n_eff,
            "evi_total": sum(card.evi_total for card in cards),
            "prod_lines": (sorted({card.prod_line for card in cards if card.prod_line})
                           if axis_type == "诉求" else []),
            "categories": (sorted({card.category for card in cards if card.category})
                           if axis_type == "通病" else []),
            "top_spus": top_spus,
            "run_id": run_id,
        })
        leader = cards[0]
        for card in cards:
            members.append({
                "axis_id": axis_id,
                "generation": generation,
                "axis_type": axis_type,
                "opp_id": card.opp_id,
                "role": "leader" if card.opp_id == leader.opp_id else "member",
                "cos_to_leader": (1.0 if card.opp_id == leader.opp_id else
                                  pairs[_pair(leader.opp_id, card.opp_id)]),
                "link_spu": (link_spu.get(card.opp_id)
                             if axis_type == "通病" else None),
            })
    return axes, members


def _selected_types(axis_type: str) -> tuple[str, ...]:
    if axis_type == "all":
        return TYPE_ORDER
    if axis_type not in VALID_AXIS_TYPES:
        raise ValueError(f"未知 --type：{axis_type}")
    return (axis_type,)


def run_strategy(
    *,
    axis_type: str = "all",
    generation: str = C.STRATEGY_GENERATION,
    dry_run: bool = False,
    run_id: str | None = None,
    store=None,
    judge: Judge | None = None,
    namer: Namer | None = None,
) -> dict:
    """运行战略聚合；依赖可注入，离线测试不会连接数据库或真实模型。"""
    if generation not in VALID_GENERATIONS:
        raise ValueError(
            f"generation 只能是 {sorted(VALID_GENERATIONS)}，实际为 {generation!r}")
    selected = _selected_types(axis_type)
    repository = store or PostgresStrategyStore()
    judge_fn = judge or _default_judge
    namer_fn = namer or _default_namer
    base_run_id = run_id or f"strategy_{int(time.time())}"
    ctx = C.RunCtx(run_id=base_run_id, week="")
    started_at = datetime.now(timezone.utc)
    usage_before = llm.usage()
    metrics: dict[str, object] = {
        "generation": generation,
        "axis_types": list(selected),
        "dry_run": dry_run,
        "pairs_recalled": 0,
        "pairs_evaluated": 0,
        "verdict_cache_hits": 0,
        "llm_judged": 0,
        "llm_failed": 0,
        "naming_failed": 0,
        "naming_invalid": 0,
        "summary_dropped": 0,
        "axes_written": 0,
        "members_written": 0,
        "stage": "input",
    }
    ctx.metrics["strategy"] = metrics

    try:
        if "通病" in selected:
            repository.assert_spu_view_generation(generation)

        cards_by_type: dict[str, list[Card]] = {}
        pairs_by_type: dict[str, dict[tuple[str, str], float]] = {}
        spu_by_type: dict[str, list[dict]] = {}
        metrics["stage"] = "recall"
        for current_type in selected:
            cards = [Card.from_row(row) for row in
                     repository.load_cards(generation, current_type)]
            cards = sorted(cards, key=lambda card: (-card.evi_total, card.opp_id))
            cards_by_type[current_type] = cards
            card_ids = {card.opp_id for card in cards}
            pairs = _normalized_pairs(
                repository.recall_pairs(
                    generation, current_type, C.STRATEGY_TOPK,
                    C.STRATEGY_MERGE_COS,
                ),
                card_ids,
            )
            pairs_by_type[current_type] = pairs
            metrics["pairs_recalled"] = int(metrics["pairs_recalled"]) + len(pairs)
            spu_by_type[current_type] = (
                repository.load_spu_rows(generation, sorted(card_ids))
                if current_type == "通病" else []
            )

        if int(metrics["pairs_recalled"]) > C.STRATEGY_MAX_PAIRS:
            raise MaxPairsExceeded(
                int(metrics["pairs_recalled"]), C.STRATEGY_MAX_PAIRS)

        all_axes: list[dict] = []
        all_members: list[dict] = []
        clusters_out: dict[str, list[list[str]]] = {}
        policies: dict[str, str] = {}
        metrics["stage"] = "judge"
        for current_type in selected:
            cards = cards_by_type[current_type]
            policy = judge_policy(current_type)
            policies[current_type] = policy
            cache = _cache_index(repository.load_cached_verdicts(
                policy, [card.opp_id for card in cards]))
            new_verdicts: list[dict] = []
            clusters = _greedy_clusters(
                cards,
                pairs_by_type[current_type],
                axis_type=current_type,
                policy=policy,
                cache=cache,
                judge=judge_fn,
                new_verdicts=new_verdicts,
                metrics=metrics,  # type: ignore[arg-type]
            )
            if not dry_run:
                repository.save_pair_verdicts(new_verdicts)
            clusters_out[current_type] = [
                [card.opp_id for card in cluster] for cluster in clusters
            ]
            metrics["stage"] = "naming"
            axes, members = _materialize_axes(
                generation,
                current_type,
                clusters,
                pairs_by_type[current_type],
                spu_by_type[current_type],
                run_id=base_run_id,
                namer=namer_fn,
                metrics=metrics,  # type: ignore[arg-type]
            )
            all_axes.extend(axes)
            all_members.extend(members)
            metrics["stage"] = "judge"

        metrics["judge_policies"] = policies
        metrics["axes_planned"] = len(all_axes)
        metrics["members_planned"] = len(all_members)
        if not dry_run:
            metrics["stage"] = "replace"
            repository.replace_partitions(
                generation, selected, all_axes, all_members)
            metrics["axes_written"] = len(all_axes)
            metrics["members_written"] = len(all_members)

        usage_delta = _usage_delta(usage_before, llm.usage())
        ctx.set_llm_usage(usage_delta["calls"], usage_delta["tokens"])
        metrics["llm_calls"] = usage_delta["calls"]
        metrics["llm_tokens"] = usage_delta["tokens"]
        metrics["cost"] = llm.cost(usage_delta)
        metrics["stage"] = "completed"
        if not dry_run:
            repository.save_run_log(ctx, "success", started_at)
        return {
            "run_id": base_run_id,
            "generation": generation,
            "axis_types": list(selected),
            "axes": all_axes,
            "members": all_members,
            "clusters": clusters_out,
            "metrics": dict(metrics),
        }
    except BaseException as error:
        usage_delta = _usage_delta(usage_before, llm.usage())
        ctx.set_llm_usage(usage_delta["calls"], usage_delta["tokens"])
        metrics["llm_calls"] = usage_delta["calls"]
        metrics["llm_tokens"] = usage_delta["tokens"]
        metrics["failure"] = {
            "exception_type": type(error).__name__,
            "stage": metrics.get("stage"),
            "message": str(error)[:500],
        }
        # MAX_PAIRS 的冻结语义是“硬中止且不写任何表”；被主管线挂钩时，
        # 外层 strategy_hook 仍会按冻结决议 8 写独立失败段。
        if not dry_run and not isinstance(error, MaxPairsExceeded):
            try:
                repository.save_run_log(ctx, "failed", started_at, error)
            except Exception as log_error:
                if hasattr(error, "add_note"):
                    error.add_note(f"strategy 失败台账写入也失败：{log_error}")
        raise

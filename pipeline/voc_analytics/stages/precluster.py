"""新品创新的归一化诉求预聚类。

只对 ``_claim`` 短句向量化；用户原文、片段和中译都不得进入本模块的
embedding 输入。
"""
from __future__ import annotations

import hashlib
import heapq
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Sequence

from .. import config as C, llm

_PAIR_BLOCK = 64


@dataclass
class _Unit:
    key: tuple[str, int]
    claim: str
    rows: list[dict]


class _UnionFind:
    def __init__(self, members: Sequence[int]) -> None:
        self.parent = {member: member for member in members}
        self.size = {member: 1 for member in members}

    def find(self, member: int) -> int:
        root = member
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[member] != member:
            parent = self.parent[member]
            self.parent[member] = root
            member = parent
        return root

    def union(self, left: int, right: int, *, max_size: int | None = None) -> bool:
        a, b = self.find(left), self.find(right)
        if a == b:
            return False
        if max_size is not None and self.size[a] + self.size[b] > max_size:
            return False
        # 大小优先，根 id 破平局：边遍历顺序不会改变最终根。
        if (self.size[a], -a) < (self.size[b], -b):
            a, b = b, a
        self.parent[b] = a
        self.size[a] += self.size[b]
        return True


def _unit_key(row: dict) -> tuple[str, int]:
    message_id = row.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("预聚类行缺少非空 message_id")
    seq = row.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise ValueError(f"{message_id} 的 seq 必须是整数")
    return message_id, seq


def _make_units(rows: list[dict]) -> tuple[list[_Unit], list[dict]]:
    claim_rows: dict[str, list[dict]] = defaultdict(list)
    empty: list[dict] = []
    for row in rows:
        claim = row.get("_claim")
        if not isinstance(claim, str) or not claim.strip():
            empty.append(row)
            continue
        message_id, _ = _unit_key(row)
        claim_rows[message_id].append(row)

    units: list[_Unit] = []
    for message_id in sorted(claim_rows):
        # seq 是规格指定的代表行选择键；原始下标只用于极端脏数据中
        # 同一 (message_id, seq) 重复时保持输入稳定。
        ordered = sorted(enumerate(claim_rows[message_id]),
                         key=lambda pair: (_unit_key(pair[1])[1], pair[0]))
        representative = ordered[0][1]
        rep_key = _unit_key(representative)
        units.append(_Unit(
            key=rep_key,
            claim=representative["_claim"].strip(),
            rows=[row for _, row in ordered],
        ))
    units.sort(key=lambda unit: unit.key)
    return units, empty


def _normalize(vector: Sequence[float]) -> tuple[float, ...]:
    try:
        values = tuple(float(value) for value in vector)
    except (TypeError, ValueError) as error:
        raise ValueError("向量必须是数字序列") from error
    if not values or any(not math.isfinite(value) for value in values):
        raise ValueError("向量不得为空或包含非有限数")
    norm = math.sqrt(sum(value * value for value in values))
    if not norm:
        raise ValueError("向量模长不得为 0")
    return tuple(value / norm for value in values)


def _dimension_order(vectors: dict[int, tuple[float, ...]]) -> tuple[int, ...]:
    """高方差维度优先，让不相似向量尽早超过距离阈值。"""
    if not vectors:
        return ()
    dim = len(next(iter(vectors.values())))
    sums = [0.0] * dim
    squares = [0.0] * dim
    for vector in vectors.values():
        if len(vector) != dim:
            raise ValueError("预聚类向量维度不一致")
        for index, value in enumerate(vector):
            sums[index] += value
            squares[index] += value * value
    n = len(vectors)
    return tuple(sorted(
        range(dim),
        key=lambda index: (-(squares[index] - sums[index] * sums[index] / n), index),
    ))


def _cosine_distance_below(
    left: Sequence[float], right: Sequence[float], order: Sequence[int], threshold: float,
) -> float | None:
    """对单位向量用平方欧距离等价计算余弦距离，并可安全早停。"""
    squared_limit = threshold * 2.0
    squared = 0.0
    for index in order:
        delta = left[index] - right[index]
        squared += delta * delta
        if squared >= squared_limit:
            return None
    return squared / 2.0


def _components(
    members: Sequence[int], vectors: dict[int, tuple[float, ...]], *,
    threshold: float, k: int, order: Sequence[int],
) -> list[list[int]]:
    ordered_members = sorted(members)
    if len(ordered_members) < 2:
        return [ordered_members] if ordered_members else []
    neighbors: dict[int, list[tuple[float, int]]] = {
        member: [] for member in ordered_members
    }
    # 只流式保留阈值内候选，不构造 n×n 距离矩阵。64 单元分块
    # 让当前向量尽量留在 CPU cache，同时不改变全量精确比较口径。
    count = len(ordered_members)
    for left_start in range(0, count, _PAIR_BLOCK):
        left_end = min(left_start + _PAIR_BLOCK, count)
        for right_start in range(left_start, count, _PAIR_BLOCK):
            right_end = min(right_start + _PAIR_BLOCK, count)
            for left_pos in range(left_start, left_end):
                first_right = (
                    left_pos + 1 if right_start == left_start else right_start)
                left_index = ordered_members[left_pos]
                left = vectors[left_index]
                for right_pos in range(first_right, right_end):
                    right_index = ordered_members[right_pos]
                    distance = _cosine_distance_below(
                        left, vectors[right_index], order, threshold)
                    if distance is None:
                        continue
                    neighbors[left_index].append((distance, right_index))
                    neighbors[right_index].append((distance, left_index))

    edges: set[tuple[int, int]] = set()
    for member in ordered_members:
        nearest = heapq.nsmallest(
            k, neighbors[member], key=lambda pair: (pair[0], pair[1]))
        for _, neighbor in nearest:
            edges.add((min(member, neighbor), max(member, neighbor)))

    union = _UnionFind(ordered_members)
    for left, right in sorted(edges):
        union.union(left, right)
    grouped: dict[int, list[int]] = defaultdict(list)
    for member in ordered_members:
        grouped[union.find(member)].append(member)
    return sorted((sorted(group) for group in grouped.values()),
                  key=lambda group: tuple(group))


def _distance(left: Sequence[float], right: Sequence[float]) -> float:
    return sum((a - b) * (a - b) for a, b in zip(left, right)) / 2.0


def _split_by_distance(
    members: Sequence[int], vectors: dict[int, tuple[float, ...]], max_size: int,
) -> list[list[int]]:
    """收紧三次仍超限时，用距离升序的受限 Kruskal 做确定性切分。"""
    ordered = sorted(members)
    edges = [
        (_distance(vectors[left], vectors[right]), left, right)
        for offset, left in enumerate(ordered)
        for right in ordered[offset + 1:]
    ]
    edges.sort(key=lambda edge: (edge[0], edge[1], edge[2]))
    union = _UnionFind(ordered)
    for _, left, right in edges:
        union.union(left, right, max_size=max_size)
    grouped: dict[int, list[int]] = defaultdict(list)
    for member in ordered:
        grouped[union.find(member)].append(member)
    return sorted((sorted(group) for group in grouped.values()),
                  key=lambda group: tuple(group))


def _bounded_components(
    members: Sequence[int], vectors: dict[int, tuple[float, ...]], *,
    threshold: float, k: int, max_size: int, order: Sequence[int], ctx,
) -> tuple[list[list[int]], int]:
    base = _components(
        members, vectors, threshold=threshold, k=k, order=order)
    final: list[list[int]] = []
    oversized_count = 0
    for component in base:
        if len(component) <= max_size:
            final.append(component)
            continue
        oversized_count += 1
        ctx.metric_incr(("precluster",), oversized_clusters=1)
        parts = [component]
        tightened = threshold
        for _ in range(3):
            tightened *= 0.7
            refined: list[list[int]] = []
            for part in parts:
                if len(part) > max_size:
                    refined.extend(_components(
                        part, vectors, threshold=tightened, k=k, order=order))
                else:
                    refined.append(part)
            parts = refined
            if all(len(part) <= max_size for part in parts):
                break
        if any(len(part) > max_size for part in parts):
            parts = [
                split
                for part in parts
                for split in (_split_by_distance(part, vectors, max_size)
                              if len(part) > max_size else [part])
            ]
        final.extend(parts)
    return sorted(final, key=lambda group: tuple(group)), oversized_count


def _cluster_id(units: Sequence[_Unit], members: Sequence[int]) -> str:
    material = [[units[index].key[0], units[index].key[1]]
                for index in sorted(members)]
    digest = hashlib.sha256(json.dumps(
        material, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest().upper()
    return f"PCL-{digest}"


def cluster_claims(rows: list[dict], ctx) -> dict:
    """按消息去重后对 ``_claim`` 构建确定性 k 近邻连通分量。"""
    units, claim_empty_rows = _make_units(rows)
    ctx.metric_update(("precluster",), oversized_clusters=0)

    vectors: dict[int, tuple[float, ...]] = {}
    failed: set[int] = set()
    batches = [list(range(start, min(start + C.EMBED_BATCH, len(units))))
               for start in range(0, len(units), C.EMBED_BATCH)]

    def embed_batch(indexes: list[int]):
        try:
            embedded = llm.embed([units[index].claim for index in indexes])
            if len(embedded) != len(indexes):
                raise llm.LLMError(
                    f"Embedding 数量不匹配: expected={len(indexes)}, "
                    f"actual={len(embedded)}")
            normalized = [_normalize(vector) for vector in embedded]
            if len({len(vector) for vector in normalized}) > 1:
                raise llm.LLMError("Embedding 批内维度不一致")
            return indexes, normalized, None
        except llm.FatalLLMError:
            raise
        except Exception as error:  # noqa: BLE001 - 单批失败是明确的降级边界
            return indexes, [], error

    results = llm.parallel_map(embed_batch, batches) if batches else []
    expected_dim: int | None = None
    for indexes, normalized, error in results:
        if error is not None:
            failed.update(indexes)
            continue
        batch_dim = len(normalized[0]) if normalized else None
        if expected_dim is None:
            expected_dim = batch_dim
        if batch_dim != expected_dim:
            failed.update(indexes)
            continue
        vectors.update(zip(indexes, normalized))

    order = _dimension_order(vectors)
    embedded_members = sorted(vectors)
    components, oversized = _bounded_components(
        embedded_members, vectors, threshold=C.PRECLUSTER_COS,
        k=C.PRECLUSTER_K, max_size=C.PRECLUSTER_MAX, order=order, ctx=ctx,
    ) if embedded_members else ([], 0)
    # 任何 embedding 失败的单元都有明确终态：各自单例簇。
    components.extend([[index] for index in sorted(failed)])
    components.sort(key=lambda group: tuple(group))

    clusters: dict[str, list[dict]] = {}
    cluster_units: dict[str, list[tuple[str, int]]] = {}
    assignments: dict[tuple[str, int], str] = {}
    for component in components:
        cluster_id = _cluster_id(units, component)
        cluster_units[cluster_id] = [units[index].key for index in component]
        cluster_rows: list[dict] = []
        for index in component:
            for row in units[index].rows:
                enhanced = dict(row)
                enhanced["_precluster_id"] = cluster_id
                cluster_rows.append(enhanced)
                assignments[_unit_key(row)] = cluster_id
        clusters[cluster_id] = cluster_rows

    stats = {
        "input_rows": sum(len(unit.rows) for unit in units),
        "input_units": len(units),
        "embedded_units": len(vectors),
        "embed_failed_units": len(failed),
        "clusters": len(components),
        "singleton_clusters": sum(len(component) == 1 for component in components),
        "oversized_clusters": oversized,
        "claim_empty": len(claim_empty_rows),
        "cluster_member_units": sum(len(component) for component in components),
        "cluster_member_rows": sum(len(cluster) for cluster in clusters.values()),
    }
    ctx.metric_update(
        ("precluster",),
        **{key: value for key, value in stats.items()
           if key != "oversized_clusters"},
    )
    return {
        "clusters": clusters,
        "cluster_units": cluster_units,
        "assignments": assignments,
        "claim_empty_rows": list(claim_empty_rows),
        "stats": stats,
    }

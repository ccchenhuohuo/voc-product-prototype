"""生成前的机会生命周期路由。

本模块只组合 :func:`classification.classify_evidence`，不复制分类规则。
输入的每条证据先独立分类，再用生命周期对应的内容键分桶。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping
import re
import unicodedata

from .classification import Classification, classify_evidence


@dataclass(frozen=True)
class LifecycleBucket:
    """生成桶的稳定内容键。

    ``channel`` 在老品桶中固定为 ``None``，所以来源名与渠道都
    不参与老品分桶；跨来源的同一规范化 tag/topic 会自然合流。
    新品桶用 ``channel`` 与 ``topic`` 分别承载规范化后的
    ``channel`` 和 ``content_branch``。
    """

    opp_type: str
    topic: str
    channel: str | None


@dataclass
class LifecycleRouting:
    """按生命周期分桶的证据，以及 R3 判定的无效证据。"""

    buckets: dict[LifecycleBucket, list[dict]]
    invalid: list[dict]


_WHITESPACE = re.compile(r"\s+")


def normalize_bucket_value(value: object, *, field: str) -> str:
    """对分桶文本做确定性规范化。

    NFKC 统一全半角，连续空白折叠成单空格，``casefold``
    让英文 tag/topic 不因大小写分裂。空值无法形成内容桶，
    因此显式报错，不生成一个含混所有缺失值的「空桶」。
    """
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是非空字符串")
    normalized = _WHITESPACE.sub(
        " ", unicodedata.normalize("NFKC", value).strip()
    ).casefold()
    if not normalized:
        raise ValueError(f"{field} 必须是非空字符串")
    return normalized


def _old_product_topic(row: Mapping[str, object]) -> str:
    tag = row.get("tag")
    if isinstance(tag, str) and tag.strip():
        return normalize_bucket_value(tag, field="tag/topic")
    return normalize_bucket_value(row.get("topic"), field="tag/topic")


def _enhance(row: Mapping[str, object], classification: Classification) -> dict:
    enhanced = dict(row)
    enhanced["_classification"] = classification
    enhanced["_opp_type"] = classification.opp_type
    enhanced["_classification_state"] = classification.classification_state
    enhanced["_classify_rule"] = classification.classify_rule
    return enhanced


def route_evidence_by_lifecycle(
    evidence: Iterable[Mapping[str, object]],
) -> LifecycleRouting:
    """逐条分类后，按生命周期的内容键分桶。

    分类依然只由 :func:`classify_evidence` 给出；这里每次传入单条
    证据，使分组前就确定该证据的生命周期。R3 证据放入
    ``invalid``，不与任何可生成桶混合。R0 只会在调用者传入
    空集时发生，此时两个输出集合均为空。

    返回的每条证据都是输入的浅拷贝，并携带
    ``_classification`` 及其 ``_opp_type`` / ``_classification_state`` /
    ``_classify_rule`` 标量投影，供后续 Stage1/Stage2 直接复用
    前置结果。
    本函数不修改输入对象。
    """
    grouped: dict[LifecycleBucket, list[dict]] = defaultdict(list)
    invalid: list[dict] = []

    for row in evidence:
        classification = classify_evidence((row,))
        enhanced = _enhance(row, classification)
        if classification.classification_state == "无效":
            invalid.append(enhanced)
            continue

        if classification.opp_type == "老品迭代":
            bucket = LifecycleBucket(
                opp_type=classification.opp_type,
                topic=_old_product_topic(row),
                channel=None,
            )
        elif classification.opp_type == "新品创新":
            bucket = LifecycleBucket(
                opp_type=classification.opp_type,
                topic=normalize_bucket_value(
                    row.get("content_branch"), field="content_branch"
                ),
                channel=normalize_bucket_value(row.get("channel"), field="channel"),
            )
        else:  # pragma: no cover - classify_evidence 的封闭返回域保护
            raise ValueError(f"不支持的机会类型：{classification.opp_type!r}")

        grouped[bucket].append(enhanced)

    return LifecycleRouting(buckets=dict(grouped), invalid=invalid)

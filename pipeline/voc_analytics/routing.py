"""生成前的机会生命周期路由。

G5 先将社媒 G4 结果收敛为 2×2 去向，再按生命周期内容键分桶。
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from . import config as C
from .classification import Classification, classify_evidence


@dataclass(frozen=True)
class LifecycleBucket:
    """生成桶的稳定内容键；来源不参与。"""

    opp_type: str
    topic: str


@dataclass
class LifecycleRouting:
    """按生命周期分桶的证据，以及 R0/R3 无效证据。"""

    buckets: dict[LifecycleBucket, list[dict]]
    invalid: list[dict]


@dataclass
class SocialValueRouting:
    """G5 的可入池行与「缺陷不可归属」终态。"""

    eligible: list[dict]
    unassigned_defects: list[dict]


_WHITESPACE = re.compile(r"\s+")


def normalize_bucket_value(value: object, *, field: str) -> str:
    """对分桶文本做 NFKC、空白折叠与 casefold 确定性规范化。"""
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


def classify_evidence_by_lifecycle(
    evidence: Iterable[Mapping[str, object]],
) -> tuple[list[dict], list[dict]]:
    """逐条固化生命周期，不从内容或来源名反推。"""
    classified: list[dict] = []
    invalid: list[dict] = []
    for row in evidence:
        classification = classify_evidence((row,))
        enhanced = _enhance(row, classification)
        if classification.classification_state == "无效":
            invalid.append(enhanced)
        else:
            classified.append(enhanced)
    return classified, invalid


def _has_fact_spu(row: Mapping[str, object]) -> bool:
    values = row.get("spu")
    return isinstance(values, (list, tuple)) and len(values) > 0


def _demote_inherited_claim(row: Mapping[str, object]) -> Mapping[str, object]:
    """诉求 + SPU 仅来自组继承 → 按无 SPU 处理，改走新品创新。

    继承是「这条评论所在的帖子在讲哪个产品」，不是「这条评论在讲哪个产品」。
    一条「能不能出个小卡收纳盒」跑到某产品视频底下，继承到该 SPU 后会被
    R1 判成该产品的老品迭代问题——但用户要的是一个还不存在的东西，
    不是在报这个产品的故障。实测（2026-08-19 生产库）：SPU 仅来自继承的
    消息里诉求缺口 176 条 / 产品缺陷 49 条，诉求是缺陷的 3.6 倍；
    已挂靠层有 167 行诉求证据靠继承挤进老品，涉及 100 张卡，
    其中 18 张整张都是「新增/增加/升级 XX」的新品诉求。

    只降级继承来的。云听直接从正文识别出 SPU 的诉求仍进老品——
    那是用户明确对着某个产品提要求，归属没有疑问。
    """
    if _has_fact_spu(row):
        return row
    if not row.get("spu_inherited"):
        return row
    demoted = dict(row)
    demoted["spu_inherited"] = []
    demoted["_inherit_demoted"] = True
    return demoted


def route_social_value_evidence(
    evidence: Iterable[Mapping[str, object]],
) -> SocialValueRouting:
    """G5 2×2：诉求/缺陷 × SPU 事实或继承挂载。

    诉求命中继承 SPU 时先降级为无 SPU（见 ``_demote_inherited_claim``），
    再进 2×2；降级后由现有 R2 规则自动送入新品创新，路由表本身不变。
    """
    eligible: list[dict] = []
    unassigned: list[dict] = []
    for row in evidence:
        value_cls = row.get("_value_cls")
        if value_cls not in {"诉求缺口", "产品缺陷"}:
            raise ValueError(f"G5 不支持的 G4 类别：{value_cls!r}")
        if value_cls == "诉求缺口":
            row = _demote_inherited_claim(row)
        classification = classify_evidence((row,))
        enhanced = _enhance(row, classification)
        if value_cls == "产品缺陷" and classification.opp_type != "老品迭代":
            enhanced["_terminal"] = "缺陷不可归属"
            unassigned.append(enhanced)
            continue
        if classification.classification_state != "确定":
            raise ValueError(f"社媒 G5 产生无效分类：{classification}")
        eligible.append(enhanced)
    return SocialValueRouting(eligible, unassigned)


def route_classified_evidence(
    evidence: Iterable[Mapping[str, object]],
) -> LifecycleRouting:
    """将已固化生命周期的证据按内容键分桶。"""
    grouped: dict[LifecycleBucket, list[dict]] = defaultdict(list)
    invalid: list[dict] = []

    for row in evidence:
        classification = row.get("_classification")
        if not isinstance(classification, Classification):
            raise ValueError("分桶前证据缺少 _classification")
        enhanced = dict(row)
        if classification.classification_state == "无效":
            invalid.append(enhanced)
            continue

        if classification.opp_type == "老品迭代":
            bucket = LifecycleBucket(
                opp_type=classification.opp_type,
                topic=_old_product_topic(row),
            )
        elif classification.opp_type == "新品创新":
            topic_value = (
                row.get("_precluster_id")
                if C.PRECLUSTER_ENABLED
                else row.get("_claim")
            )
            bucket = LifecycleBucket(
                opp_type=classification.opp_type,
                topic=normalize_bucket_value(
                    topic_value,
                    field="precluster_id" if C.PRECLUSTER_ENABLED else "claim",
                ),
            )
        else:  # pragma: no cover - classify_evidence 的封闭返回域保护
            raise ValueError(f"不支持的机会类型：{classification.opp_type!r}")

        grouped[bucket].append(enhanced)

    return LifecycleRouting(buckets=dict(grouped), invalid=invalid)


def route_evidence_by_lifecycle(
    evidence: Iterable[Mapping[str, object]],
) -> LifecycleRouting:
    """逐条分类后按生命周期内容键分桶；不修改输入对象。"""
    classified, invalid = classify_evidence_by_lifecycle(evidence)
    routed = route_classified_evidence(classified)
    routed.invalid.extend(invalid)
    return routed

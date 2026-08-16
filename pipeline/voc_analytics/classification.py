"""机会点分类契约：只依赖已挂载证据的消息属性。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class Classification:
    """分类与有效性是两个维度；无效时不伪造 ``opp_type``。"""

    opp_type: str | None
    classification_state: str
    classify_rule: str


def _has_spu(evidence: Mapping[str, object]) -> bool:
    """与 PostgreSQL ``cardinality(spu) > 0`` 同口径。"""
    spu = evidence.get("spu")
    if spu is None:
        return False
    if not isinstance(spu, (list, tuple)):
        raise ValueError(f"证据 spu 必须是数组或 None，实得 {type(spu).__name__}")
    return len(spu) > 0


def classify_evidence(evidence: Iterable[Mapping[str, object]]) -> Classification:
    """按 R0 → R1 → R2 → R3 对一个机会点的完整证据集求值。

    R2/R3 判断的是证据所属来源是否保证挂载 SPU，不是
    机会点的发现渠道。这个来源政策由取数层以
    ``source_requires_spu`` 布尔字段逐条携带，分类函数不再
    认识任何具体渠道名。无 SPU 且来源政策混合时，因 R2
    先于 R3，任一不强制挂 SPU 的证据即命中 R2。

    R0/R3 只能判定「无效」，不能从证据推导老品/新品，因此
    ``opp_type`` 返回 None，而不是引入第三个类型。

    每条证据都必须显式携带布尔值政策。即使证据挂了
    SPU，也会先校验整个证据集的政策字段，避免数据缺陷被
    R1 短路掩盖。
    """
    rows = tuple(evidence)
    if not rows:
        return Classification(None, "无效", "R0")

    policies: list[bool] = []
    for index, row in enumerate(rows):
        if "source_requires_spu" not in row:
            raise ValueError(
                f"第 {index} 条证据缺少 source_requires_spu 布尔字段"
            )
        requires_spu = row["source_requires_spu"]
        if not isinstance(requires_spu, bool):
            raise ValueError(
                f"第 {index} 条证据 source_requires_spu 必须是 bool，"
                f"实得 {type(requires_spu).__name__}"
            )
        policies.append(requires_spu)

    if any(_has_spu(row) for row in rows):
        return Classification("老品迭代", "确定", "R1")

    if any(not requires_spu for requires_spu in policies):
        return Classification("新品创新", "确定", "R2")
    return Classification(None, "无效", "R3")

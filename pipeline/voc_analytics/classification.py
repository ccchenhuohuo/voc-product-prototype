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

    R2/R3 判断的是证据所属消息的 ``src_line``，不是机会点的
    发现渠道。无 SPU 且混合两种来源时，因 R2 先于 R3，命中 R2。

    R0/R3 只能判定「无效」，不能从证据推导老品/新品，因此
    ``opp_type`` 返回 None，而不是引入第三个类型。
    """
    rows = tuple(evidence)
    if not rows:
        return Classification(None, "无效", "R0")

    if any(_has_spu(row) for row in rows):
        return Classification("老品迭代", "确定", "R1")

    lines = {row.get("src_line") for row in rows}
    if "社媒" in lines:
        return Classification("新品创新", "确定", "R2")
    if "电商" in lines:
        return Classification(None, "无效", "R3")

    raise ValueError(f"非空证据集缺少可识别的 src_line：{sorted(map(str, lines))}")

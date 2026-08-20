"""SPU 列表页共用的排序参数校验。"""
from __future__ import annotations


SORT_KEYS = frozenset(("grade", "ratio", "evidence", "star", "issues"))


def sort_state(sort: str, direction: str) -> tuple[str, str]:
    key = sort.strip()
    value = direction.strip().lower()
    return (key, value) if key in SORT_KEYS and value in ("asc", "desc") else ("", "")

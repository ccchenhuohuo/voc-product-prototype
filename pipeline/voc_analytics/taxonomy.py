"""标签体系（PRD v8 §4.2）。

三个已验证的结构事实：
  · "X标签树"(扁平) 与 "X-全局分析"(层级) 的叶子集合完全相同（灯光 159/159，支撑 140/140）
    => 用层级树给扁平标签补语义路径
  · 5A 顶层中只有 A4.Act > 产品体验 分支能生成产品原型
  · 两棵树有 50 个共用标签、17 组同义异名 => 必须跨线归一，否则同一问题生成两个机会点
"""
from __future__ import annotations
from functools import lru_cache

from . import yunting

PROD_LINES = ("灯光", "支撑")
ANALYSIS_TREES = [f"{l}-全局分析" for l in PROD_LINES]
DEWATER_TREE = "社媒去水标签树"

PRODUCT_BRANCH = "产品体验"
SCENE_BRANCH = "用户群体&场景"

# 跨产品线同义异名映射（人工维护，17 组）
SYNONYMS = {
    "产品包装品质": "包装品质",
    "产品包装外观": "包装外观",
    "产品包装完整性": "包装完整性",
    "噪音控制": "噪声控制",
    "标准螺丝接口": "标准螺纹接口",
    "vlog(含生活记录）": "Vlog",
    "vlog(含生活记录)": "Vlog",
    "产品瑕疵": "整体质量",
    "产品成色": "整体质量",
    "产品质量": "整体质量",
    "产品部件": "产品组件",
}


def split_tax(tax_path: str | None) -> tuple[str | None, str | None,
                                              str | None, str | None]:
    """从「树名/阶段/体验域/子类/叶子」拆出业务四级，缺层返回 None。

    这里刻意保留中间空段，与迁移中的 split_part 位置语义一致；过滤空段会让
    缺失层之后的值错误上移，面包屑看似完整但含义已经变了。
    """
    parts = tax_path.split("/") if tax_path is not None else []

    def part(index: int) -> str | None:
        if index >= len(parts):
            return None
        value = parts[index].strip()
        return value or None

    return part(1), part(2), part(3), part(4)


def normalize(tag: str) -> str:
    return SYNONYMS.get(tag.strip(), tag.strip())


class Taxonomy:
    """叶子标签 -> (语义路径, 5A顶层, 是否产品体验, 是否场景, 产品线集合)"""

    def __init__(self, trees: list[dict]):
        self.path: dict[str, str] = {}
        self.l1: dict[str, str] = {}
        self.lines: dict[str, set[str]] = {}
        for tree in trees:
            line = tree["name"].split("-")[0]
            self._walk(tree, [], line)

    def _walk(self, node: dict, trail: list[str], line: str) -> None:
        kids = node.get("children") or []
        if not kids:
            tag = normalize(node["name"])
            # 首个产品线的路径优先；后续只补充 lines
            self.path.setdefault(tag, "/".join(trail))
            self.l1.setdefault(tag, trail[0] if trail else "")
            self.lines.setdefault(tag, set()).add(line)
            return
        for k in kids:
            self._walk(k, trail + [node["name"]], line)

    # -- 查询 --
    def is_product(self, tag: str) -> bool:
        p = self.path.get(normalize(tag), "")
        return PRODUCT_BRANCH in p and SCENE_BRANCH not in p

    def is_scene(self, tag: str) -> bool:
        return SCENE_BRANCH in self.path.get(normalize(tag), "")

    def prod_line(self, tag: str) -> str:
        s = self.lines.get(normalize(tag), set())
        return next(iter(s)) if len(s) == 1 else ("未定" if not s else "通用")

    def info(self, tag: str) -> dict:
        t = normalize(tag)
        return {"tag": t, "tax_path": self.path.get(t, ""), "tax_l1": self.l1.get(t, ""),
                "is_product": self.is_product(t), "is_scene": self.is_scene(t)}

    def stats(self) -> dict:
        return {"leaves": len(self.path),
                "product": sum(1 for t in self.path if self.is_product(t)),
                "scene": sum(1 for t in self.path if self.is_scene(t)),
                "shared": sum(1 for t, s in self.lines.items() if len(s) > 1)}


@lru_cache(maxsize=1)
def load() -> Taxonomy:
    """从云听拉取层级树并构建。带缓存，一次运行只拉一次。"""
    return Taxonomy(yunting.tag_tree_details(ANALYSIS_TREES))


def dewater_values() -> list[str]:
    trees = yunting.tag_tree_details([DEWATER_TREE])
    out: list[str] = []

    def walk(n: dict) -> None:
        kids = n.get("children") or []
        if not kids:
            out.append(n["name"])
        for k in kids:
            walk(k)

    for t in trees:
        walk(t)
    return out

"""Stage 1：问题模式切分（PRD v8 §5.3–5.5）。

三个关键设计，都是审计指出的原方案缺陷的修正：
  1. 取消 60 条截断 —— 改分批（50/批）+ 跨批模式归并，全量证据参与
  2. 投票置换必须确定性 —— 3 个写死的置换，否则引入的变异大于消除的
  3. 共现矩阵 → 分组不能用连通分量 —— 链式传递会把不同失效模式粘成一大组，
     改用最大团分解 + 单组上限
"""
from __future__ import annotations
import hashlib, itertools
from collections import Counter, defaultdict
from .. import config as C, llm, prompts

# 命名失败时的兜底名。它是内部占位符，【不得】流入 title/core_tag/problem_mode，
# 也不参与跨批归并（见 merge_similar_modes）。实测泄漏过一次，产出的标题是
# 「摄影配件品类：填补未命名模式（用户自定义功能组合）」。
PLACEHOLDER_MODE = "未命名模式"


# ---------------------------------------------------------------- 确定性置换
def permutations(items: list[dict]) -> list[list[int]]:
    """3 个固定置换，随 prompt_ver 落库。返回原始下标序列。"""
    n = len(items)
    p1 = sorted(range(n), key=lambda i: (
        items[i].get("star") if items[i].get("star") is not None else 99,
        str(items[i].get("message_id")), items[i].get("seq", 0)))
    p2 = list(reversed(p1))
    p3 = sorted(range(n), key=lambda i: hashlib.md5(
        f'{items[i].get("message_id")}:{items[i].get("seq",0)}'.encode()).hexdigest())
    return [p1, p2, p3]


def batches(order: list[int], size: int = C.BATCH_SIZE) -> list[list[int]]:
    return [order[i:i + size] for i in range(0, len(order), size)]


# ---------------------------------------------------------------- 最大团分解
def _bron_kerbosch(graph: dict[int, set[int]], limit_nodes: int = 60) -> list[set[int]]:
    """列出极大团。节点过多时退化为贪心，避免指数爆炸。"""
    nodes = set(graph)
    if len(nodes) > limit_nodes:
        return _greedy_cliques(graph)
    out: list[set[int]] = []

    def expand(r: set[int], p: set[int], x: set[int]) -> None:
        if not p and not x:
            out.append(set(r))
            return
        if len(out) > 400:            # 安全阀
            return
        pivot = max(p | x, key=lambda v: len(graph[v] & p), default=None)
        for v in list(p - (graph[pivot] if pivot is not None else set())):
            expand(r | {v}, p & graph[v], x & graph[v])
            p.discard(v); x.add(v)

    expand(set(), set(nodes), set())
    return out or _greedy_cliques(graph)


def _greedy_cliques(graph: dict[int, set[int]]) -> list[set[int]]:
    out, remaining = [], set(graph)
    while remaining:
        seed = max(remaining, key=lambda v: len(graph[v] & remaining))
        clique = {seed}
        for v in sorted(graph[seed] & remaining,
                        key=lambda v: -len(graph[v] & remaining)):
            if clique <= graph[v]:
                clique.add(v)
        out.append(clique)
        remaining -= clique
    return out


def aggregate_votes(co: dict[tuple[int, int], int], members: set[int],
                    min_votes: int = 2,
                    max_size: int = C.MAX_GROUP_SIZE) -> tuple[list[set[int]], set[int]]:
    """共现次数 >= min_votes 建图，做最大团分解。返回 (分组, 被淘汰的成员)。"""
    graph: dict[int, set[int]] = {m: set() for m in members}
    for (a, b), n in co.items():
        if n >= min_votes and a in graph and b in graph:
            graph[a].add(b); graph[b].add(a)

    cliques = sorted(_bron_kerbosch(graph), key=len, reverse=True)
    used: set[int] = set()
    groups: list[set[int]] = []
    for cl in cliques:
        cl = cl - used
        if len(cl) < 1:
            continue
        # 单组上限：超限时按度数截断，剩余进入下一轮
        if len(cl) > max_size:
            ranked = sorted(cl, key=lambda v: -len(graph[v] & cl))
            cl = set(ranked[:max_size])
        groups.append(cl)
        used |= cl
    dropped = members - used
    return [g for g in groups if g], dropped


# ---------------------------------------------------------------- 单批调用
def _fmt_a(items: list[dict], idx: list[int]) -> str:
    out = []
    for n, i in enumerate(idx, 1):
        it = items[i]
        star = f'{it["star"]:.0f}星' if it.get("star") is not None else "无星级"
        out.append(f'[{n}] {star} | {it.get("country") or "?"} | {it.get("snippet")}')
    return "\n".join(out)


def _fmt_b(items: list[dict], idx: list[int]) -> str:
    out = []
    for n, i in enumerate(idx, 1):
        it = items[i]
        txt = (it.get("content") or "").replace("\n", " ")[:300]
        br = ",".join(it.get("brands") or []) or "-"
        out.append(f'[{n}] {it.get("platform")} | 赞{it.get("interactions") or 0} | {br} | {txt}')
    return "\n".join(out)


def split_batch(items: list[dict], idx: list[int], line: str, ctx_info: dict,
                batch_i: int, batch_n: int) -> dict:
    """对一个批次调 Stage 1，返回 {mode_name: [原始下标]} 与 unclassified。"""
    common = dict(n=len(idx), min_evidence=C.MIN_EVIDENCE[line],
                  batch_i=batch_i, batch_n=batch_n)
    if line == "线A":
        prompt = prompts.STAGE1_A.format(
            items=_fmt_a(items, idx), category=ctx_info.get("category", "?"),
            tag=ctx_info.get("tag", "?"), tax_path=ctx_info.get("tax_path", ""), **common)
    else:
        prompt = prompts.STAGE1_B.format(
            items=_fmt_b(items, idx), channel=ctx_info.get("channel", "需求缺口"), **common)

    obj, _ = llm.chat_json(prompt, max_tokens=2000, required=["modes"])
    modes: dict[str, list[int]] = {}
    for m in obj.get("modes", []):
        name = str(m.get("mode_name", "")).strip()
        picks = [idx[j - 1] for j in m.get("evidence_idx", [])
                 if isinstance(j, int) and 1 <= j <= len(idx)]
        if name and picks:
            modes.setdefault(name, []).extend(picks)
    uncl = [idx[j - 1] for j in obj.get("unclassified", [])
            if isinstance(j, int) and 1 <= j <= len(idx)]
    return {"modes": modes, "unclassified": uncl}


# ---------------------------------------------------------------- 编排
def split_bucket(items: list[dict], line: str, ctx_info: dict, ctx,
                 vote: bool | None = None) -> dict:
    """对一个证据桶做完整切分：分批 × 投票 → 共现 → 最大团 → 命名。

    返回 {"groups":[{"mode_name":..,"members":[下标..]}], "dropped":[下标..],
          "unclassified":[下标..], "rounds":n}
    """
    if vote is None:
        vote = C.VOTE_ENABLED
    n = len(items)
    if n == 0:
        return {"groups": [], "dropped": [], "unclassified": [], "rounds": 0}

    perms = permutations(items)
    rounds = perms if vote else perms[:1]
    co: Counter = Counter()
    name_votes: dict[frozenset, Counter] = defaultdict(Counter)
    uncl_votes: Counter = Counter()
    all_batches = 0

    # 批次并行：早期这里是双层串行循环，5 个批次逐个跑，Stage1 占了整桶
    # 耗时的 ~2/3，而 LLM_CONCURRENCY 的配额大量闲置。
    jobs = [(bi, bidx, (len(order) + C.BATCH_SIZE - 1) // C.BATCH_SIZE)
            for order in rounds
            for bi, bidx in enumerate(batches(order), 1)]
    all_batches = len(jobs)

    def _run(job):
        bi, bidx, bn = job
        try:
            return split_batch(items, bidx, line, ctx_info, bi, bn)
        except Exception:  # noqa: BLE001 —— 批次级失败隔离
            ctx.bump(failed=1)
            return None

    for r in llm.parallel_map(_run, jobs):
        if not isinstance(r, dict):
            continue
        for name, members in r["modes"].items():
            for a, b in itertools.combinations(sorted(set(members)), 2):
                co[(a, b)] += 1
            name_votes[frozenset(members)][name] += 1
        for u in r["unclassified"]:
            uncl_votes[u] += 1

    members = set(range(n))
    unclassified = {u for u, c in uncl_votes.items() if c >= (2 if vote else 1)}
    members -= unclassified
    groups_idx, dropped = aggregate_votes(co, members, min_votes=2 if vote else 1)

    # 命名：取与该组重合度最高、且票数最多的名字
    groups = []
    for g in groups_idx:
        best_name, best_score = None, -1.0
        for key, cnt in name_votes.items():
            overlap = len(g & set(key))
            if not overlap:
                continue
            for nm, votes in cnt.items():
                score = overlap / max(len(g | set(key)), 1) * votes
                if score > best_score:
                    best_name, best_score = nm, score
        groups.append({"mode_name": best_name or PLACEHOLDER_MODE,
                       "members": sorted(g)})

    ctx.metrics.setdefault("stage1", {}).setdefault(line, []).append(
        {"bucket": ctx_info.get("tag") or ctx_info.get("channel"),
         "n": n, "batches": all_batches, "groups": len(groups),
         "unclassified": len(unclassified), "dropped": len(dropped)})
    return {"groups": groups, "dropped": sorted(dropped),
            "unclassified": sorted(unclassified), "rounds": len(rounds)}


# ---------------------------------------------------------------- 跨批模式归并
def merge_similar_modes(groups: list[dict], ctx) -> list[dict]:
    """同桶内跨批产生的同义模式合并：embedding 召回 + LLM 判定（不设阈值判定）。

    两个约束都是实测事故的修正，不能去掉：
      1. 合并后仍受 MAX_GROUP_SIZE 约束 —— 早期这里无上限，把几个 40 人组
         滚成 88 人（OPP-E1DCDBD5DA），Stage2 拿到 88 条证据却只描述了 1 条，
         剩下的全靠编。aggregate_votes 辛苦设的上限在这一步被绕过了。
      2. 兜底名 PLACEHOLDER_MODE 不参与合并 —— 它语义空泛，在 0.85 余弦下
         和什么都像，会变成吸附器把无关组统统吸进来。
    """
    if len(groups) < 2:
        return groups
    names = [g["mode_name"] for g in groups]
    vecs = llm.embed(names)
    merged: dict[int, int] = {}
    for i in range(len(groups)):
        if i in merged or names[i] == PLACEHOLDER_MODE:
            continue
        for j in range(i + 1, len(groups)):
            if j in merged or names[j] == PLACEHOLDER_MODE:
                continue
            if len(groups[i]["members"]) + len(groups[j]["members"]) > C.MAX_GROUP_SIZE:
                continue                  # 合并会超上限，宁可留作两个组
            if llm.cosine(vecs[i], vecs[j]) < C.MODE_MERGE_COS:
                continue
            try:
                obj, _ = llm.chat_json(
                    prompts.MODE_MERGE.format(a=names[i], b=names[j]),
                    max_tokens=200, required=["same"])
                ctx.bump()
            except Exception:  # noqa: BLE001
                continue
            if obj.get("same"):
                groups[i]["members"] = sorted(set(groups[i]["members"]) | set(groups[j]["members"]))
                groups[i]["mode_name"] = obj.get("merged_name") or names[i]
                merged[j] = i
    return [g for k, g in enumerate(groups) if k not in merged]

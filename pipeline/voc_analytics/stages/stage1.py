"""Stage 1：问题模式切分（PRD v8 §5.3–5.5）。

三个关键设计，都是审计指出的原方案缺陷的修正：
  1. 取消 60 条截断 —— 改分批（50/批）+ 跨批模式归并，全量证据参与
  2. 投票置换必须确定性 —— 3 个写死的置换，否则引入的变异大于消除的
  3. 共现矩阵 → 分组不能用连通分量 —— 链式传递会把不同失效模式粘成一大组，
     改用最大团分解 + 单组上限
"""
from __future__ import annotations
import hashlib, itertools, threading
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
def _evidence_text(item: dict, limit: int = 400) -> str:
    """按统一证据契约取原文，不从桶的来源推断字段。"""
    text = (item.get("evidence_text") or item.get("snippet")
            or item.get("content") or "")
    return str(text).replace("\n", " ")[:limit]


def _fmt_items(items: list[dict], idx: list[int]) -> str:
    """逐条展示可用元数据，允许一个生命周期桶包含多个来源。"""
    out = []
    for n, i in enumerate(idx, 1):
        it = items[i]
        meta: list[str] = []
        if it.get("star") is not None:
            star = it["star"]
            star_text = f"{star:g}" if isinstance(star, (int, float)) else str(star)
            meta.append(f"星级:{star_text}")
        if it.get("country"):
            meta.append(f'国家:{it["country"]}')
        if it.get("platform"):
            meta.append(f'平台:{it["platform"]}')
        brands = it.get("brands")
        if brands:
            if isinstance(brands, (list, tuple, set)):
                brands = ",".join(str(x) for x in brands if x)
            meta.append(f"品牌:{brands}")
        out.append(f'[{n}] {" | ".join(meta) or "无可用元数据"} | {_evidence_text(it)}')
    return "\n".join(out)


def _context_line(ctx_info: dict) -> str:
    fields = (
        ("品类", "category"), ("标签", "tag"), ("语义路径", "tax_path"),
        ("内容通道", "channel"),
    )
    parts = [f"{label}:{ctx_info[key]}" for label, key in fields if ctx_info.get(key)]
    return " | ".join(parts) or "未提供额外桶上下文"


def split_batch(items: list[dict], opp_type: str, idx: list[int], ctx_info: dict,
                batch_i: int, batch_n: int) -> dict:
    """对一个批次调 Stage 1，返回 {mode_name: [原始下标]} 与 unclassified。"""
    prompt_by_type = {
        "老品迭代": prompts.STAGE1_A,
        "新品创新": prompts.STAGE1_B,
    }
    if opp_type not in prompt_by_type:
        raise ValueError(f"Stage1 不支持的机会类型: {opp_type!r}")
    prompt = prompt_by_type[opp_type].format(
        n=len(idx), min_evidence=C.MIN_EVIDENCE[opp_type],
        batch_i=batch_i, batch_n=batch_n, context=_context_line(ctx_info),
        items=_fmt_items(items, idx))

    # BATCH_SIZE=50 条证据可能产出十几个模式，每个带 mode_name + evidence_idx
    # + mechanism。2000 实测不够：2b 首轮响应在 4465 字符处被截断，JSON 断在
    # 半路。全历史池的桶比单周大得多，这个上限只在全量重建时才撑爆。
    obj, _ = llm.chat_json(prompt, max_tokens=6000, required=["modes"])
    modes: dict[str, list[int]] = {}
    for m in obj.get("modes", []):
        name = str(m.get("mode_name", "")).strip()
        picks = [idx[j - 1] for j in m.get("evidence_idx", [])
                 if isinstance(j, int) and 1 <= j <= len(idx)]
        if name and picks:
            modes.setdefault(name, []).extend(picks)
    uncl = [idx[j - 1] for j in obj.get("unclassified", [])
            if isinstance(j, int) and 1 <= j <= len(idx)]

    # 归属守恒在这里【只记账不中止】，原因是两类偏差都不是失败：
    #
    # · 重复归属是设计预期。一条抱怨同时命中两个失效模式很正常，而 Stage1
    #   架构本就是「分批 × 投票 → 共现 → 最大团」，多次归属的共现正是构图
    #   信号，重叠由最大团一步收敛。
    # · 遗漏是 LLM 的概率性瑕疵。归进 unclassified 即可——那本来就是
    #   「归不了类的证据」这个语义桶，不丢数据、可审计、下游已有处理。
    #
    # 2c 曾在此要求严格一一对应，2026-08-17 单周探针连撞两次：先因
    # duplicated=1 在 19 次调用后中止，放宽后又因 missing=2 在 89 次调用
    # 后中止。一轮全量要跨几千次调用，要求零偏差等于这轮永远跑不完。
    # 真正防「部分失败记成全量成功」的是批次级 planned/completed/failed/
    # cancelled 账本，那个不能松。
    assigned = [member for picks in modes.values() for member in picks] + uncl
    counts = Counter(assigned)
    missing = [member for member in idx if counts[member] == 0]
    if missing:
        uncl = uncl + missing
    duplicated = sum(count - 1 for count in counts.values() if count > 1)
    return {"modes": modes, "unclassified": uncl,
            "missing": len(missing), "duplicated": duplicated}


# ---------------------------------------------------------------- 编排
def split_bucket(items: list[dict], opp_type: str, ctx_info: dict, ctx,
                 vote: bool | None = None) -> dict:
    """对一个证据桶做完整切分：分批 × 投票 → 共现 → 最大团 → 命名。

    返回 {"groups":[{"mode_name":..,"members":[下标..]}], "dropped":[下标..],
          "unclassified":[下标..], "rounds":n}
    """
    if opp_type not in ("老品迭代", "新品创新"):
        raise ValueError(f"Stage1 不支持的机会类型: {opp_type!r}")
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
    jobs = [(job_i, bi, bidx, (len(order) + C.BATCH_SIZE - 1) // C.BATCH_SIZE)
            for job_i, (order, bi, bidx) in enumerate(
                ((order, bi, bidx) for order in rounds
                 for bi, bidx in enumerate(batches(order), 1)), 1)]
    all_batches = len(jobs)

    bucket_label = str(ctx_info.get("bucket_key") or ctx_info.get("bucket") or ctx_info.get("tag")
                       or ctx_info.get("channel") or ctx_info.get("category") or "未命名桶")
    bucket_fingerprint = hashlib.sha256(repr(sorted(
        (str(key), repr(value)) for key, value in ctx_info.items())).encode()).hexdigest()[:10]
    metric_path = ("stage1", opp_type, "buckets", f"{bucket_label}:{bucket_fingerprint}")
    planned_batch_rows = sum(len(bidx) for _, _, bidx, _ in jobs)
    ctx.metric_update(
        metric_path, bucket=bucket_label, status="running", rounds=len(rounds),
        input_rows=n, accounted_rows=0, planned_batches=all_batches,
        completed_batches=0, failed_batches=0, cancelled_batches=0,
        planned_batch_rows=planned_batch_rows, completed_batch_rows=0,
        failed_batch_rows=0, cancelled_batch_rows=0)

    outcome_lock = threading.Lock()
    outcomes: dict[int, tuple[str, int]] = {}

    def _record(job_i: int, outcome: str, row_count: int) -> None:
        with outcome_lock:
            outcomes[job_i] = (outcome, row_count)
        ctx.metric_incr(
            metric_path,
            **{f"{outcome}_batches": 1, f"{outcome}_batch_rows": row_count})

    def _finalize_metrics(status: str, accounted_rows: int = 0, **values: object) -> None:
        with outcome_lock:
            snapshot = dict(outcomes)
        completed_rows = sum(rows for state, rows in snapshot.values() if state == "completed")
        failed_rows = sum(rows for state, rows in snapshot.values() if state == "failed")
        cancelled_jobs = [job for job in jobs if job[0] not in snapshot]
        cancelled_rows = sum(len(job[2]) for job in cancelled_jobs)
        ctx.metric_update(
            metric_path, status=status, accounted_rows=accounted_rows,
            completed_batches=sum(1 for state, _ in snapshot.values() if state == "completed"),
            failed_batches=sum(1 for state, _ in snapshot.values() if state == "failed"),
            cancelled_batches=len(cancelled_jobs), completed_batch_rows=completed_rows,
            failed_batch_rows=failed_rows, cancelled_batch_rows=cancelled_rows,
            **values)

    def _run(job):
        job_i, bi, bidx, bn = job
        try:
            result = split_batch(items, opp_type, bidx, ctx_info, bi, bn)
        except llm.FatalLLMError:
            # 配额/鉴权耗尽这类错误继续跑也是白费，必须立刻停整轮。
            ctx.bump(failed=1)
            _record(job_i, "failed", len(bidx))
            raise
        except Exception as error:  # noqa: BLE001
            # 单批的输出瑕疵（JSON 截断、字段缺失）不是整轮该死的理由：
            # 210 个桶 × 多批次 × 3 轮投票是几千次调用，指望每次都产出合法
            # JSON 不现实。记进批次账本、返回异常对象让上层按失败率裁决。
            # 2026-08-17 的 2b 第二轮就是被一个批次的 JSON 截断带崩全进程的。
            ctx.bump(failed=1)
            _record(job_i, "failed", len(bidx))
            return error
        _record(job_i, "completed", len(bidx))
        return result

    try:
        results = llm.parallel_map(_run, jobs)
    except Exception:
        _finalize_metrics("failed")
        raise

    if len(results) != all_batches:
        _finalize_metrics("failed")
        raise llm.LLMError(
            f"Stage1 并行结果数不匹配: planned={all_batches}, returned={len(results)}")
    failed_results = [result for result in results if isinstance(result, BaseException)]
    invalid_results = [result for result in results
                       if not isinstance(result, dict) and not isinstance(result, BaseException)]
    if invalid_results:
        _finalize_metrics("failed")
        raise llm.LLMError(f"Stage1 并行调用返回非法结果: {type(invalid_results[0]).__name__}")

    # 个别批次失败可容忍并记账；失败率过高说明是系统性问题（提示词失效、
    # 模型行为变化），继续跑只会产出残缺结果，必须中止让人来看。
    if failed_results:
        failed_ratio = len(failed_results) / max(all_batches, 1)
        if failed_ratio > C.STAGE1_MAX_FAILED_RATIO:
            _finalize_metrics("failed")
            raise llm.LLMError(
                f"Stage1 批次失败率过高: {len(failed_results)}/{all_batches} "
                f"({failed_ratio:.0%} > {C.STAGE1_MAX_FAILED_RATIO:.0%})，"
                f"首个错误: {failed_results[0]}")
        print(f"   [Stage1] 桶内 {len(failed_results)}/{all_batches} 批失败已跳过"
              f"（阈值 {C.STAGE1_MAX_FAILED_RATIO:.0%}）：{failed_results[0]}")
    results = [r for r in results if isinstance(r, dict)]

    for r in results:
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

    grouped_members = [member for group in groups for member in group["members"]]
    accounted = grouped_members + list(unclassified) + list(dropped)
    if len(accounted) != n or set(accounted) != set(range(n)):
        _finalize_metrics(
            "failed", accounted_rows=len(set(accounted)), groups=len(groups),
            unclassified=len(unclassified), dropped=len(dropped))
        raise RuntimeError(
            f"Stage1 桶证据对账失败: input={n}, accounted={len(set(accounted))}, "
            f"assignments={len(accounted)}")

    # 对账要的是【账目守恒】——每个计划中的批次都有归宿（完成/失败/取消），
    # 而不是「必须全部完成」。后者会让账本永远记不下一个被容忍的失败：
    # 上面刚按失败率放行的批次，到这里又会把整桶判死。
    with outcome_lock:
        completed_batches = sum(1 for state, _ in outcomes.values() if state == "completed")
        failed_batches = sum(1 for state, _ in outcomes.values() if state == "failed")
        completed_rows = sum(rows for state, rows in outcomes.values() if state == "completed")
        failed_rows = sum(rows for state, rows in outcomes.values() if state == "failed")
    settled_batches = completed_batches + failed_batches
    settled_rows = completed_rows + failed_rows
    if settled_batches != all_batches or settled_rows != planned_batch_rows:
        _finalize_metrics(
            "failed", accounted_rows=n, groups=len(groups),
            unclassified=len(unclassified), dropped=len(dropped))
        raise RuntimeError(
            f"Stage1 批次对账失败（账目不守恒）: 已结算 {settled_batches}/{all_batches} 批, "
            f"{settled_rows}/{planned_batch_rows} 行")

    _finalize_metrics(
        "completed", accounted_rows=n, groups=len(groups),
        unclassified=len(unclassified), dropped=len(dropped))
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
            obj, _ = llm.chat_json(
                prompts.MODE_MERGE.format(a=names[i], b=names[j]),
                max_tokens=200, required=["same"])
            ctx.bump()
            if obj.get("same"):
                groups[i]["members"] = sorted(set(groups[i]["members"]) | set(groups[j]["members"]))
                groups[i]["mode_name"] = obj.get("merged_name") or names[i]
                merged[j] = i
    return [g for k, g in enumerate(groups) if k not in merged]

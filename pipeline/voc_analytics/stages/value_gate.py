"""社媒 G4 统一价值门。

判定按 message_id 去重；同消息的多条证据共享投票与缓存结果。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from .. import config as C, db, llm, prompts

_CLASSES = {"诉求缺口", "产品缺陷", "无价值"}
_COMMENT_TYPES = {"评论", "回复"}
_ROOT_TYPES = {"帖子", "视频"}
_PLACEHOLDER = re.compile(r"某款|某个|待发布")
_GENERIC_WORDS = re.compile(
    r"需要|缺少|希望|新增|推出|提供|具备|"
    r"某款|某个|待发布|一款|一个|这款|这个|"
    r"新品|产品|东西|对象|配件|功能|能力|方案|"
    r"相关|对应|通用|未定|未知|的"
)
_SUBSTANTIVE = re.compile(r"[A-Za-z0-9一-鿿]{2,}")


@dataclass(frozen=True)
class ValueGateResult:
    passed_rows: list[dict]
    no_value_rows: list[dict]
    generic_claim_rows: list[dict]
    failed_rows: list[dict]
    stats: dict


def _clean_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def message_content(row: Mapping[str, object]) -> str:
    """逐消息取文：优先完整用户正文，缺失时才回落原声片段。"""
    for key in ("content", "evidence_text", "snippet", "content_zh"):
        text = _clean_text(row.get(key))
        if text:
            return text[:4000]
    return ""


def parent_title_for(
    row: Mapping[str, object],
    thread_rows: Iterable[Mapping[str, object]] = (),
) -> str:
    """评论/回复只取同组帖子/视频标题；帖子本身一律不喂标题。"""
    if row.get("message_type") not in _COMMENT_TYPES:
        return "（无）"

    projected = _clean_text(row.get("parent_title"))
    if projected and projected != "（无）":
        return projected

    group_id = row.get("message_group_id")
    candidates: list[tuple[str, str]] = []
    for candidate in thread_rows:
        if candidate.get("message_group_id") != group_id:
            continue
        if candidate.get("message_type") not in _ROOT_TYPES:
            continue
        title = _clean_text(candidate.get("message_title"))
        if title:
            candidates.append((str(candidate.get("message_id") or ""), title))
    return min(candidates)[1] if candidates else "（无）"


def is_generic_claim(claim: object) -> bool:
    """空 claim，或命中指定占位词且剩余无具体名词，均为终态「诉求过泛」。"""
    text = _clean_text(claim)
    if not text:
        return True
    if not _PLACEHOLDER.search(text):
        return False
    residual = _GENERIC_WORDS.sub("", text)
    residual = re.sub(r"[^A-Za-z0-9一-鿿]+", "", residual)
    return not bool(_SUBSTANTIVE.search(residual))


def _validated_vote(obj: Mapping[str, object]) -> dict:
    cls = obj.get("cls")
    if cls not in _CLASSES:
        raise llm.LLMError(f"G4 返回未知类别：{cls!r}")
    try:
        confidence = float(obj.get("confidence"))
    except (TypeError, ValueError) as error:
        raise llm.LLMError("G4 confidence 必须是数字") from error
    if not 0.0 <= confidence <= 1.0:
        raise llm.LLMError(f"G4 confidence 越界：{confidence}")
    claim = _clean_text(obj.get("claim")) if cls == "诉求缺口" else ""
    return {
        "cls": cls,
        "confidence": confidence,
        "reason": _clean_text(obj.get("reason")),
        "claim": claim,
    }


def majority_vote(votes: list[Mapping[str, object]]) -> dict:
    """取类别多数；置信度取胜出票平均，claim 取胜出票中最高置信的忠实归一。"""
    if not votes:
        raise ValueError("投票不得为空")
    normalized = [_validated_vote(vote) for vote in votes]
    counts = Counter(vote["cls"] for vote in normalized)
    first_position = {
        vote["cls"]: index
        for index, vote in reversed(list(enumerate(normalized)))
    }
    winner = min(counts, key=lambda cls: (-counts[cls], first_position[cls]))
    winning = [vote for vote in normalized if vote["cls"] == winner]
    representative = max(
        enumerate(winning), key=lambda pair: (pair[1]["confidence"], -pair[0])
    )[1]
    return {
        "cls": winner,
        "claim": representative["claim"] if winner == "诉求缺口" else "",
        "confidence": sum(vote["confidence"] for vote in winning) / len(winning),
        "votes": len(normalized),
    }


def _representatives(rows: list[dict]) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    grouped: dict[str, list[tuple[int, int, dict]]] = {}
    for position, row in enumerate(rows):
        message_id = row.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("G4 行缺少非空 message_id")
        seq = row.get("seq")
        seq_order = seq if isinstance(seq, int) and not isinstance(seq, bool) else 0
        grouped.setdefault(message_id, []).append((seq_order, position, row))
    representatives = {
        message_id: min(items, key=lambda item: (item[0], item[1]))[2]
        for message_id, items in grouped.items()
    }
    expanded = {
        message_id: [item[2] for item in sorted(items, key=lambda item: item[1])]
        for message_id, items in grouped.items()
    }
    return representatives, expanded


def apply_value_gate(
    rows: list[dict],
    ctx,
    *,
    cache_loader: Callable[[list[str], str], list[dict]] | None = None,
    cache_saver: Callable[[list[dict]], int] | None = None,
) -> ValueGateResult:
    """执行 G4；非致命单消息失败单独记账，FatalLLMError 继续燔断整轮。"""
    representatives, grouped = _representatives(rows)
    message_ids = sorted(representatives)
    loader = cache_loader or db.load_social_gates
    saver = cache_saver or db.save_social_gates
    cached_rows = loader(message_ids, prompts.VALUE_GATE_VER) if message_ids else []
    judgments: dict[str, dict] = {
        row["message_id"]: {
            "cls": row["cls"],
            "claim": _clean_text(row.get("claim")),
            "confidence": float(row.get("confidence") or 0),
            "votes": int(row.get("votes") or 1),
            "cached": True,
        }
        for row in cached_rows
        if row.get("prompt_ver") == prompts.VALUE_GATE_VER
        and row.get("message_id") in representatives
    }
    pending = [message_id for message_id in message_ids if message_id not in judgments]

    def judge(message_id: str) -> tuple[str, dict | None, BaseException | None]:
        row = representatives[message_id]
        content = message_content(row)
        if not content:
            return message_id, None, ValueError("G4 消息无可判文本")
        prompt = prompts.SOCIAL_VALUE_GATE.format(
            parent_title=parent_title_for(row),
            content=content,
            platform=_clean_text(row.get("platform")) or "（无）",
        )

        def one_vote(index: int) -> dict:
            obj, meta = llm.chat_json(
                prompt,
                max_tokens=500,
                seed=42 + index,
                required=["cls", "confidence", "reason", "claim"],
            )
            ctx.bump(tokens=meta.get("tokens", 0))
            return _validated_vote(obj)

        try:
            first = one_vote(0)
            votes = [first]
            if C.GATE_VOTE_ENABLED and (
                first["confidence"] < C.GATE_VOTE_CONF
                or first["cls"] == "产品缺陷"
            ):
                votes.extend((one_vote(1), one_vote(2)))
            return message_id, {**majority_vote(votes), "cached": False}, None
        except llm.FatalLLMError:
            raise
        except Exception as error:  # noqa: BLE001 - 单消息是明确失败边界
            ctx.bump(failed=1)
            return message_id, None, error

    results = llm.parallel_map(judge, pending) if pending else []
    failures: dict[str, BaseException] = {}
    new_cache: list[dict] = []
    for message_id, judgment, error in results:
        if error is not None:
            failures[message_id] = error
            continue
        assert judgment is not None
        judgments[message_id] = judgment
        new_cache.append({
            "message_id": message_id,
            "cls": judgment["cls"],
            "claim": judgment["claim"],
            "confidence": round(float(judgment["confidence"]), 3),
            "votes": judgment["votes"],
            "prompt_ver": prompts.VALUE_GATE_VER,
        })
    if new_cache:
        saver(new_cache)

    passed: list[dict] = []
    no_value: list[dict] = []
    generic: list[dict] = []
    failed: list[dict] = []
    for message_id in message_ids:
        if message_id in failures:
            error = failures[message_id]
            failed.extend(
                {**row, "_gate_error": f"{type(error).__name__}: {error}"}
                for row in grouped[message_id]
            )
            continue
        judgment = judgments[message_id]
        enhanced = [
            {
                **row,
                "_value_cls": judgment["cls"],
                "_conf": float(judgment["confidence"]),
                "_claim": judgment["claim"],
                "_gate_votes": int(judgment["votes"]),
                "_gate_cached": bool(judgment["cached"]),
            }
            for row in grouped[message_id]
        ]
        if judgment["cls"] == "无价值":
            no_value.extend(enhanced)
        elif judgment["cls"] == "诉求缺口" and is_generic_claim(
            judgment["claim"]
        ):
            generic.extend(enhanced)
        else:
            passed.extend(enhanced)

    stats = {
        "input_rows": len(rows),
        "input_messages": len(message_ids),
        "cache_hit_messages": len(message_ids) - len(pending),
        "llm_messages": len(pending),
        "llm_votes": sum(
            int(judgments[message_id]["votes"])
            for message_id in pending
            if message_id in judgments
        ),
        "failed_messages": len(failures),
        "failed_rows": len(failed),
        "no_value_rows": len(no_value),
        "generic_claim_rows": len(generic),
        "passed_rows": len(passed),
    }
    ctx.metric_update(("value_gate",), **stats)
    return ValueGateResult(passed, no_value, generic, failed, stats)

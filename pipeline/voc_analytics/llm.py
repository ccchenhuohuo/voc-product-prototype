"""百炼客户端（PRD v8 §5.8 稳定性控制 / §8.4 重试与超时）。

M0 实测约束：
  · embedding 批量上限 = 10（25 直接报 batch size is invalid）
  · chat 单次约 1.5s；当前默认全局在飞调用上限为 64，可由环境变量覆盖
  · 权限变更后短窗口返回 AccessDenied.Unpurchased —— 属传播延迟，必须指数退避重试
"""
from __future__ import annotations
import json, math, re, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, Iterator, Sequence

from . import config as C

_LOCK = threading.Lock()
_USAGE = {"calls": 0, "tokens": 0}
# 可重试：限流、传播延迟、5xx、网络抖动
_RETRYABLE = re.compile(
    r"429|Throttl|RateLimit|Unpurchased|InternalError|ServiceUnavailable|"
    r"timed out|Connection|5\d\d", re.I)


class LLMError(RuntimeError):
    pass


def usage() -> dict:
    with _LOCK:
        return dict(_USAGE)


def reset_usage() -> None:
    with _LOCK:
        _USAGE.update(calls=0, tokens=0)


def _account(tokens: int) -> None:
    with _LOCK:
        _USAGE["calls"] += 1
        _USAGE["tokens"] += tokens


# 全局在飞调用闸门。并发是【嵌套】的——桶级线程池里每个桶又各开一个批次级
# 线程池，每个池都按 LLM_CONCURRENCY 开，乘起来远超实测拐点 64，撞 429 后
# 走指数退避反而更慢。真正该约束的是「同时在飞的 HTTP 调用总数」，与调用点
# 嵌套几层无关，所以闸门放在这里而不是各个线程池上。
# 有了它，上层线程池可以放心开大：谁抢到令牌谁发车，没抢到的排队等，
# 吞吐稳定压在拐点上。
_GATE = threading.Semaphore(C.LLM_CONCURRENCY)


def _post(path: str, payload: dict, timeout: int = C.LLM_TIMEOUT) -> dict:
    url = C.BAILIAN_BASE.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Authorization": "Bearer " + C.require("BAILIAN_API_KEY", C.BAILIAN_KEY),
               "Content-Type": "application/json"}
    last = ""
    for attempt in range(C.LLM_RETRY):
        # 每次重试重新构造 Request：urllib 的 Request 对象重复使用会带上
        # 上一次的状态，退避重试时容易出怪问题
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with _GATE:                   # 令牌在手才发车
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.load(r)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.read().decode()[:200]}"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        if attempt == C.LLM_RETRY - 1 or not _RETRYABLE.search(last):
            break
        time.sleep(2 ** attempt)          # 2/4/8s 指数退避
    raise LLMError(last)


# ---------------------------------------------------------------- embedding
def embed(texts: Sequence[str], dim: int = C.EMBED_DIM) -> list[list[float]]:
    """批量向量化。自动按实测上限 10 分批。"""
    out: list[list[float]] = []
    for i in range(0, len(texts), C.EMBED_BATCH):
        chunk = list(texts[i:i + C.EMBED_BATCH])
        r = _post("/embeddings", {"model": C.EMBED_MODEL, "input": chunk, "dimensions": dim})
        _account(r.get("usage", {}).get("total_tokens", 0))
        out.extend(d["embedding"] for d in sorted(r["data"], key=lambda x: x["index"]))
    return out


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    return num / (da * db) if da and db else 0.0


# ---------------------------------------------------------------- chat
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def chat(prompt: str, *, max_tokens: int = 1600, model: str | None = None,
         seed: int = 42) -> tuple[str, dict]:
    r = _post("/chat/completions", {
        "model": model or C.CHAT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0, "top_p": 0.01, "seed": seed, "max_tokens": max_tokens,
    })
    _account(r.get("usage", {}).get("total_tokens", 0))
    text = r["choices"][0]["message"]["content"]
    meta = {"model_id": r.get("model", C.CHAT_MODEL),
            "model_ver": r.get("model", C.CHAT_MODEL),
            "tokens": r.get("usage", {}).get("total_tokens", 0)}
    return text, meta


def chat_json(prompt: str, *, max_tokens: int = 1600, seed: int = 42,
              required: Sequence[str] = ()) -> tuple[dict, dict]:
    """要求模型输出 JSON。带一次纠正重试——模型偶尔会包裹说明文字。"""
    text, meta = chat(prompt, max_tokens=max_tokens, seed=seed)
    for attempt in range(2):
        raw = text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
        m = _JSON_BLOCK.search(raw)
        if m:
            try:
                obj = json.loads(m.group(0))
                if all(k in obj for k in required):
                    return obj, meta
                missing = [k for k in required if k not in obj]
                err = f"缺少字段 {missing}"
            except json.JSONDecodeError as e:
                err = f"JSON 解析失败: {e}"
        else:
            err = "未找到 JSON 对象"
        if attempt == 1:
            raise LLMError(f"{err}；原始输出前 300 字: {text[:300]}")
        text, meta = chat(prompt + f"\n\n【上次输出无效：{err}】请只输出合法 JSON，不要任何解释文字。",
                          max_tokens=max_tokens, seed=seed)
    raise LLMError("unreachable")


def _safe(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def run(x: Any) -> Any:
        try:
            return fn(x)
        except Exception as e:  # noqa: BLE001
            return LLMError(str(e)[:300])
    return run


def parallel_map(fn: Callable[[Any], Any], items: Iterable[Any],
                 workers: int | None = None) -> list[Any]:
    """受控并发，**保持输入顺序**。失败不阻断其他项，返回 LLMError 占位（§8.4）。

    结果与输入位置一一对应，调用方靠下标取回上下文时依赖这一点。
    只在乎"尽快拿到每个结果"时用 parallel_imap。
    """
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=workers or C.LLM_CONCURRENCY) as ex:
        return list(ex.map(_safe(fn), items))


def parallel_imap(fn: Callable[[Any], Any], items: Iterable[Any],
                  workers: int | None = None) -> Iterator[Any]:
    """同上，但**谁先完成先产出**，顺序不保证。

    用于长跑任务的增量落库：parallel_map 是个 barrier，要等最后一项完成才返回，
    冷启动跑一个多小时中途崩溃则全部丢失。逐个产出后调用方可以边跑边入库。
    """
    items = list(items)
    if not items:
        return iter(())

    def gen() -> Iterator[Any]:
        with ThreadPoolExecutor(max_workers=workers or C.LLM_CONCURRENCY) as ex:
            futs = [ex.submit(_safe(fn), x) for x in items]
            for f in as_completed(futs):
                yield f.result()
    return gen()

"""百炼客户端（PRD v8 §5.8 稳定性控制 / §8.4 重试与超时）。

M0 实测约束：
  · embedding 批量上限 = 10（25 直接报 batch size is invalid）
  · chat 单次约 1.5s；当前默认全局在飞调用上限为 64，可由环境变量覆盖

阶段 2c 失败语义：限流、网络抖动和 5xx 可重试；鉴权、欠费和硬配额耗尽会
打开熔断器；监督脚本配置共享文件时，首因会传播到同一 run 的另一生命周期
进程。唯一的鉴权例外是 AccessDenied.Unpurchased——它是权限变更后的传播延迟，
实测 1–2 分钟自愈（运维交接 §3.1），仍走重试而非熔断。
"""
from __future__ import annotations

import json, math, os, re, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, Iterator, Sequence

from . import config as C

# 信号处理器会在主线程任意字节码边界发布取消；若恰好打断本线程持锁区，
# 普通 Lock 会自锁。RLock 保持跨线程互斥，同时允许同一主线程安全重入。
_LOCK = threading.RLock()
_USAGE = {"calls": 0, "tokens": 0}

# 记录首个致命错误。只保存文本而不复用异常对象，避免多线程同时抛出
# 同一对象时互相改写 traceback。
_CIRCUIT_REASON: str | None = None

# 可重试：普通限流和网络抖动。HTTP 5xx 由状态码单独判定。
_RETRYABLE = re.compile(
    r"429|Throttl|Rate[ _.-]*Limit|InternalError|ServiceUnavailable|"
    r"timed?\s*out|Timeout|Connection|temporar(?:y|ily)",
    re.I,
)

# 鉴权类里唯一的可重试例外，判定顺序必须排在 _FATAL_AUTH 之前。
_TRANSIENT_AUTH = re.compile(r"Access[ _.-]*Denied[ _.-]*Unpurchased", re.I)

# 致命判定必须早于 ``_RETRYABLE``，因为硬配额耗尽也常以 HTTP 429 返回。
_FATAL_AUTH = re.compile(
    r"Arrearage|Access[ _.-]*Denied|Unauthorized|Authentication|"
    r"Invalid[ _.-]*(?:API[ _.-]*)?Key|Incorrect[ _.-]*(?:API[ _.-]*)?Key|"
    r"API[ _.-]*Key.{0,40}(?:invalid|incorrect|expired|disabled)|"
    r"鉴权(?:失败|错误)|认证失败|未授权|无权限|账户?欠费|欠费",
    re.I,
)
_FATAL_QUOTA = re.compile(
    r"insufficient[ _.-]*(?:quota|balance)|"
    r"(?:Throttling[ _.-]*)?Allocation[ _.-]*Quota(?:[ _.-]*(?:Exceeded|Exhausted))?|"
    r"Allocation[ _.-]*quota.{0,40}(?:exceed|exhaust|deplet)|"
    r"(?:Allocation[ _.-]*)?Quota[ _.-]*Exceeded|"
    r"(?:quota|balance)[ _.-]*(?:exhausted|depleted|insufficient)|"
    r"exceeded.{0,40}(?:current|monthly|total|hard)[ _.-]*quota|"
    r"current[ _.-]*quota.{0,40}(?:exceed|exhaust|deplet)|"
    r"(?:monthly|total|hard|billing)[ _.-]*quota.{0,40}(?:exceed|exhaust|deplet)|"
    r"quota.{0,60}(?:billing|payment|recharge)|"
    r"(?:balance|credit).{0,30}(?:insufficient|exhausted|depleted|too[ _.-]*low)|"
    r"(?:not[ _.-]*enough|insufficient).{0,20}(?:balance|credit)|"
    r"余额(?:不足|已?用尽|已?耗尽)|额度(?:不足|已?用尽|已?耗尽)|"
    r"配额(?:不足|已?用尽|已?耗尽)",
    re.I,
)
_RATE_QUOTA = re.compile(r"Rate[ _.-]*Quota|rate\s+quota", re.I)
_HARD_QUOTA_QUALIFIER = re.compile(
    r"Allocation|monthly|total|hard|billing|payment|balance|credit|recharge|"
    r"余额|充值|硬配额|总配额|月配额",
    re.I,
)


class LLMError(RuntimeError):
    pass


class FatalLLMError(LLMError):
    """不可重试的 LLM 失败。

    首个此类错误会打开本进程及可选共享 run 熔断器；后续请求不触达 transport。
    """


def usage() -> dict:
    with _LOCK:
        return dict(_USAGE)


def reset_usage() -> None:
    """新进程 run 的边界：清空用量与本进程熔断状态。

    ``VOC_LLM_CIRCUIT_FILE`` 若存在则由外层 supervisor 按 run 管理，
    这里不能删除：两个生命周期几乎同时启动时，后启动者删除文件会抹掉
    先启动者刚发布的致命错误。
    """
    global _CIRCUIT_REASON
    with _LOCK:
        _USAGE.update(calls=0, tokens=0)
        _CIRCUIT_REASON = None


def reset_for_tests() -> None:
    """纯测试状态重置入口；不发请求，不读环境，不产生外部副作用。"""
    reset_usage()


def _shared_circuit_path() -> str | None:
    value = os.environ.get("VOC_LLM_CIRCUIT_FILE", "").strip()
    return value or None


def _read_shared_circuit() -> str | None:
    path = _shared_circuit_path()
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read(2000).strip()
    except FileNotFoundError:
        return None
    except OSError as error:
        # supervisor 显式配置了共享熔断文件却不可读时，不能继续发请求。
        return f"共享 LLM 熔断文件不可读：{type(error).__name__}: {error}"
    if not text:
        return "另一生命周期已触发致命 LLM 熔断"
    lines = text.splitlines()
    if lines and lines[0].startswith("pid="):
        text = "\n".join(lines[1:]).strip()
    return text or "另一生命周期已触发致命 LLM 熔断"


def _publish_shared_circuit(reason: str) -> str:
    """以硬链接竞争发布跨进程首因，避免读到半写入文件。"""
    path = _shared_circuit_path()
    if path is None:
        return reason
    payload = f"pid={os.getpid()}\n{reason}\n"
    temporary = (
        f"{path}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    publish_error: OSError | None = None
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # 另一进程更早发布；下方读取并保留它的首因。
    except OSError as error:
        # 本进程已先打开本地 breaker；保留业务首因，同时让日志显式暴露
        # 跨进程传播失败，supervisor 随进程非零退出再终止兄弟进程。
        publish_error = error
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError:
            pass
    shared = _read_shared_circuit()
    if shared is not None:
        return shared
    if publish_error is not None:
        return (f"{reason}；共享熔断发布失败："
                f"{type(publish_error).__name__}: {publish_error}")
    return reason


def _circuit_reason() -> str | None:
    global _CIRCUIT_REASON
    shared = _read_shared_circuit()
    with _LOCK:
        if shared is not None:
            _CIRCUIT_REASON = shared
        return _CIRCUIT_REASON


def _open_circuit(reason: str) -> str:
    """原子地保留本进程/共享 run 的首个致命原因。"""
    global _CIRCUIT_REASON
    reason = reason or "LLM 发生未知致命错误"
    # 线性化点必须在任何文件 I/O 之前：同进程其他线程立刻停止发车，
    # 文件系统变慢/失败也不会形成进程内 fail-open 窗口。
    with _LOCK:
        if _CIRCUIT_REASON is None:
            _CIRCUIT_REASON = reason
        local_first = _CIRCUIT_REASON
    published = _publish_shared_circuit(local_first)
    with _LOCK:
        if _shared_circuit_path() is not None:
            _CIRCUIT_REASON = published
        return _CIRCUIT_REASON


def _raise_if_circuit_open() -> None:
    reason = _circuit_reason()
    if reason is not None:
        raise FatalLLMError(reason)


def _raise_fatal(reason: str) -> None:
    raise FatalLLMError(_open_circuit(reason))


def cancel(reason: str) -> None:
    """由 supervisor/顶层失败发布线程及跨进程可见的取消原因。"""
    _open_circuit(reason or "LLM run 已取消")


def ensure_available() -> None:
    """在持久化等非 HTTP 边界检查同一 run 是否已被取消。"""
    _raise_if_circuit_open()


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


def _http_error_text(error: urllib.error.HTTPError) -> str:
    try:
        raw = error.read()
    except Exception:  # noqa: BLE001 - 诊断信息读取失败不应覆盖 HTTP 错误
        raw = b""
    if isinstance(raw, bytes):
        detail = raw.decode(errors="replace")
    else:
        detail = str(raw)
    return f"HTTP {error.code} {detail[:500]}".strip()


def _is_fatal(error_text: str, status: int | None = None) -> bool:
    """鉴权/欠费/硬配额错误不受 HTTP 429 通用重试规则影响。"""
    # 唯一的鉴权例外：AccessDenied.Unpurchased 是百炼在权限变更后的
    # 传播延迟，实测 1–2 分钟自愈，运维文档 §3.1 明确标为「不是配置错误」。
    # 把它算成致命会让一次 3.5 小时的全量重跑因为一个会自己好的错误中断。
    # 位置必须在 _FATAL_AUTH 之前——那条正则的 Access[ _.-]*Denied 会命中它。
    if _TRANSIENT_AUTH.search(error_text) is not None:
        return False
    if status in {401, 402, 403} or _FATAL_AUTH.search(error_text) is not None:
        return True
    quota_fatal = _FATAL_QUOTA.search(error_text) is not None
    # 供应商会把速率配额写成 RateQuotaExceeded；它仍是瞬时限流。
    # 只有同时出现 Allocation/月度/计费/余额等硬限定词时才覆盖。
    if (quota_fatal and _RATE_QUOTA.search(error_text)
            and not _HARD_QUOTA_QUALIFIER.search(error_text)):
        return False
    return quota_fatal


def _is_retryable(error: BaseException, error_text: str,
                  status: int | None = None) -> bool:
    if status is not None:
        return status == 429 or 500 <= status <= 599
    return (
        isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))
        or _RETRYABLE.search(error_text) is not None
    )


def _post(path: str, payload: dict, timeout: int = C.LLM_TIMEOUT) -> dict:
    _raise_if_circuit_open()
    try:
        api_key = C.require("BAILIAN_API_KEY", C.BAILIAN_KEY)
    except Exception as error:  # noqa: BLE001 - 缺 key 必须统一升格为致命错误
        _raise_fatal(f"{type(error).__name__}: {error}")

    url = C.BAILIAN_BASE.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    last = ""
    attempts = max(1, C.LLM_RETRY)
    for attempt in range(attempts):
        _raise_if_circuit_open()
        # 每次重试重新构造 Request：urllib 的 Request 对象重复使用会带上
        # 上一次的状态，退避重试时容易出怪问题
        request = urllib.request.Request(url, data=body, headers=headers)
        caught: BaseException
        status: int | None
        try:
            with _GATE:                   # 令牌在手才发车
                # 等待令牌期间另一线程可能已熔断；发车前必须再查。
                _raise_if_circuit_open()
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    result = json.load(response)
                # 请求在飞期间，另一生命周期可能已发布 fatal。结果不得再进入
                # Stage/持久化；返回 transport 结果前再次检查共享熔断。
                _raise_if_circuit_open()
                return result
        except FatalLLMError:
            raise
        except urllib.error.HTTPError as error:
            caught = error
            status = error.code
            last = _http_error_text(error)
        except Exception as error:  # noqa: BLE001
            caught = error
            status = None
            last = f"{type(error).__name__}: {error}"

        if _is_fatal(last, status):
            _raise_fatal(last)
        if attempt == attempts - 1 or not _is_retryable(caught, last, status):
            break
        _raise_if_circuit_open()
        time.sleep(2 ** attempt)          # 1/2/4s 指数退避
    _raise_if_circuit_open()
    raise LLMError(last)


# ---------------------------------------------------------------- embedding
def embed(texts: Sequence[str], dim: int = C.EMBED_DIM) -> list[list[float]]:
    """批量向量化。自动按实测上限 10 分批。"""
    out: list[list[float]] = []
    for i in range(0, len(texts), C.EMBED_BATCH):
        chunk = list(texts[i:i + C.EMBED_BATCH])
        r = _post("/embeddings", {"model": C.EMBED_MODEL, "input": chunk, "dimensions": dim})
        _account(r.get("usage", {}).get("total_tokens", 0))
        data = r.get("data")
        if not isinstance(data, list):
            raise LLMError("Embedding 响应缺少 data 数组")
        indexes = [item.get("index") for item in data if isinstance(item, dict)]
        valid_indexes = (
            len(indexes) == len(data)
            and all(isinstance(index, int) and not isinstance(index, bool)
                    for index in indexes)
        )
        if (len(data) != len(chunk) or not valid_indexes
                or sorted(indexes) != list(range(len(chunk)))):
            raise LLMError(
                f"Embedding 响应索引不完整: expected={len(chunk)}, "
                f"actual={len(data)}, indexes={indexes}")
        ordered = sorted(data, key=lambda item: item["index"])
        for item in ordered:
            vector = item.get("embedding")
            if not isinstance(vector, list) or len(vector) != dim:
                actual = len(vector) if isinstance(vector, list) else None
                raise LLMError(
                    f"Embedding 维度不匹配: expected={dim}, actual={actual}")
            out.append(vector)
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


def parallel_map(fn: Callable[[Any], Any], items: Iterable[Any],
                 workers: int | None = None) -> list[Any]:
    """受控并发，**保持输入顺序**。

    结果与输入位置一一对应。任一任务失败时立即取消未开始任务，
    并以原异常类型抛出；不再将异常伪装成普通返回值。
    """
    items = list(items)
    if not items:
        return []

    executor = ThreadPoolExecutor(max_workers=workers or C.LLM_CONCURRENCY)
    futures = []
    try:
        for item in items:
            futures.append(executor.submit(fn, item))
        indexes = {future: index for index, future in enumerate(futures)}
        results: list[Any] = [None] * len(futures)
        for future in as_completed(futures):
            # result() 保留 worker 中的原异常类型与 traceback。
            results[indexes[future]] = future.result()
    except BaseException as error:
        try:
            # 普通校验/transport 耗尽也代表本次生成不可完整。先发布取消，
            # 再等待已启动 worker，让它们在下一安全点停止而非继续整桶。
            cancel(f"并行任务失败：{type(error).__name__}: {error}")
        except Exception as cancel_error:
            if hasattr(error, "add_note"):
                error.add_note(f"发布并行取消也失败：{cancel_error}")
        for future in futures:
            future.cancel()
        # 等所有已开始任务退出后再把异常交给上层，保证 run-log 计数不再被后台线程改写。
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
        return results


def parallel_imap(fn: Callable[[Any], Any], items: Iterable[Any],
                  workers: int | None = None) -> Iterator[Any]:
    """同上，但**谁先完成先产出**，顺序不保证。

    用于长跑任务的增量落库：parallel_map 是个 barrier，要等最后一项完成才返回，
    冷启动跑一个多小时中途崩溃则全部丢失。任一任务失败时取消未开始任务，
    并以原异常类型抛出。
    """
    items = list(items)
    if not items:
        return iter(())

    def generate() -> Iterator[Any]:
        executor = ThreadPoolExecutor(max_workers=workers or C.LLM_CONCURRENCY)
        futures = []
        try:
            for item in items:
                futures.append(executor.submit(fn, item))
            for future in as_completed(futures):
                yield future.result()
        except BaseException as error:
            try:
                cancel(f"并行任务失败：{type(error).__name__}: {error}")
            except Exception as cancel_error:
                if hasattr(error, "add_note"):
                    error.add_note(f"发布并行取消也失败：{cancel_error}")
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)

    return generate()

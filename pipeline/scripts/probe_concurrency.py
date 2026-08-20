#!/usr/bin/env python3
"""并发拐点实测：扫描 LLM_CONCURRENCY，找吞吐停止上升 / 开始撞限流的点。

背景：llm.py:291 的注释称「实测拐点 64」，但没有留下测量记录，而
rerun_both.sh 阶段一是每进程 48 × 两进程 = 96 在飞，已在该值之上。
上一轮因为老品落库串行，实际在飞数远低于设置值，所以没暴露问题；
落库并行化之后两个进程都会全力发车，需要重新定这个值。

走生产同一条调用路径（llm.chat_json → _post → _GATE），只把信号量换掉，
因此测到的就是生产会遇到的行为。拦 urllib.request.urlopen 统计 HTTP
状态码——429 在 _post 内部会被重试掉，不拦就看不见。

会消耗真实额度（约 1,200 次小调用，≈¥1）。不写库。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python scripts/probe_concurrency.py
"""
from __future__ import annotations

import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voc_analytics import llm  # noqa: E402

import os
LEVELS = [int(x) for x in os.environ.get("LEVELS", "16,32,48,64,96,128").split(",")]
BIG = os.environ.get("BIG") == "1"

# 与落库关键路径同形态的调用：L3 同义判定，max_tokens=400。
BIG_PROMPT = """把下面这批用户反馈按失效模式切分。只输出 JSON。

""" + "\n".join(f"{i}. 支架用了一段时间之后出现松动、晃动、无法锁紧的情况，"
                 f"具体表现为伸缩杆节段下滑、云台角度保持不住、底座支撑腿展开后不稳。"
                 for i in range(1, 41)) + """

输出格式：{"modes":[{"mode_name":"...","evidence_idx":[1,2],"mechanism":"..."}],
"unclassified":[]}"""

PROMPT = """判断下面两个问题模式是否指同一个失效。只输出 JSON。

A：伸缩杆节段在正常拉伸过程中意外脱出
B：伸缩杆段间阻尼失效导致节段滑脱

输出格式：{"same": true 或 false, "merged_name": "合并后的名称"}"""

http_counts: Counter = Counter()
_orig_urlopen = urllib.request.urlopen
_lock = threading.Lock()


def counting_urlopen(*args, **kwargs):
    try:
        return _orig_urlopen(*args, **kwargs)
    except urllib.error.HTTPError as error:
        with _lock:
            http_counts[f"HTTP {error.code}"] += 1
        raise
    except Exception as error:                       # noqa: BLE001
        with _lock:
            http_counts[type(error).__name__] += 1
        raise


urllib.request.urlopen = counting_urlopen


def one_call() -> bool:
    try:
        if BIG:
            llm.chat_json(BIG_PROMPT, max_tokens=6000, required=["modes"])
        else:
            llm.chat_json(PROMPT, max_tokens=400, required=["same"])
        return True
    except Exception:                                # noqa: BLE001
        return False


def run_level(level: int, calls: int) -> dict:
    llm._GATE = threading.Semaphore(level)           # 生产同一个闸门，只换容量
    http_counts.clear()
    llm.reset_usage()
    started = time.time()
    ok = 0
    with ThreadPoolExecutor(max_workers=level) as pool:
        futures = [pool.submit(one_call) for _ in range(calls)]
        for fut in as_completed(futures):
            ok += 1 if fut.result() else 0
    elapsed = time.time() - started
    usage = llm.usage()
    return {
        "level": level, "calls": calls, "ok": ok, "failed": calls - ok,
        "elapsed": elapsed, "tps": ok / elapsed if elapsed else 0,
        "avg_latency": level / (ok / elapsed) if ok else 0,
        "http": dict(http_counts),
        "tokens": usage.get("tokens", 0),
    }


def main() -> None:
    print(f"{'并发':>5}{'调用':>6}{'成功':>6}{'失败':>5}"
          f"{'耗时s':>8}{'吞吐/s':>9}{'均延迟s':>9}  异常")
    print("-" * 68)
    results = []
    for level in LEVELS:
        # 每档至少 3 波，否则测到的是「一波并发跑多久」而不是稳态吞吐：
        # 单波会被最慢的那个请求主导，出现并发越高延迟越低的假象。
        calls = min(max(60, level * 3), 200) if not BIG else level * 3
        r = run_level(level, calls)
        results.append(r)
        print(f"{r['level']:>5}{r['calls']:>6}{r['ok']:>6}{r['failed']:>5}"
              f"{r['elapsed']:>8.1f}{r['tps']:>9.2f}{r['avg_latency']:>9.1f}"
              f"  {r['http'] or '—'}")
        time.sleep(3)                                # 让上一档的在飞请求排空

    best = max(results, key=lambda r: r["tps"])
    print("\n" + "=" * 68)
    print(f"吞吐峰值出现在并发 {best['level']}：{best['tps']:.2f} 次/秒")
    clean = [r for r in results if not r["http"] and r["failed"] == 0]
    if clean:
        top = max(clean, key=lambda r: r["level"])
        print(f"无异常的最高并发：{top['level']}（吞吐 {top['tps']:.2f} 次/秒）")
    print("rerun_both.sh 阶段一是两进程并行，PER_PROC_CONCURRENCY "
          "应设为上述在飞总数的一半。")
    print("=" * 68)


if __name__ == "__main__":
    main()

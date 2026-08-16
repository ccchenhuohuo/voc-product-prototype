#!/usr/bin/env python3
"""M0 验收：百炼 QPS / 批量上限 / 延迟压测（PRD v8 §11.1）。

输出用于反推 voc/llm 并发限值与单周作业时长。

用法：
  cd /home/sdy/voc-analytics
  set -a; . /opt/agent-runtime/.env; set +a
  .venv/bin/python scripts/m0_qps_probe.py

前置条件：
  在 ulanzicloud 上使用 Python 3.11+；BAILIAN_API_KEY 已导出（可选覆盖
  BAILIAN_EMBEDDING_BASE_URL、HISTORY_RAG_EMBEDDING_MODEL）；允许访问百炼 API。
  密钥只从受控环境文件加载，不得写入仓库。脚本会发起真实请求并消耗模型额度，
  但不访问数据库。
"""
import json, os, time, urllib.request, urllib.error, statistics as st
from concurrent.futures import ThreadPoolExecutor

KEY = os.environ["BAILIAN_API_KEY"]
EMB_BASE = os.environ.get("BAILIAN_EMBEDDING_BASE_URL",
                          "https://dashscope.aliyuncs.com/compatible-mode/v1")
EMB_MODEL = os.environ.get("HISTORY_RAG_EMBEDDING_MODEL", "text-embedding-v4")
CHAT_MODEL = "qwen-plus"


def _post(url, payload, timeout=90):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=timeout))
        return True, time.time() - t0, r
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        return False, time.time() - t0, f"HTTP {e.code} {body}"
    except Exception as e:
        return False, time.time() - t0, str(e)[:200]


def embed(texts, dim=1024):
    return _post(EMB_BASE.rstrip("/") + "/embeddings",
                 {"model": EMB_MODEL, "input": texts, "dimensions": dim})


def chat(prompt, max_tokens=200):
    return _post(EMB_BASE.rstrip("/") + "/chat/completions",
                 {"model": CHAT_MODEL, "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens, "temperature": 0})


def section(t):
    print("\n" + "=" * 62); print(t); print("=" * 62)


# ---------- 1. embedding 批量上限 ----------
section("1. embedding 批量上限探测")
batch_cap = 0
for n in (10, 25, 50, 100, 200):
    ok, dt, r = embed(["测试文本%d" % i for i in range(n)])
    if ok:
        batch_cap = n
        print(f"   batch={n:<4} OK    {dt:.2f}s   {n/dt:.1f} 条/秒")
    else:
        print(f"   batch={n:<4} FAIL  {r[:110]}")
        break
    time.sleep(0.5)
print(f"   => 实测可用批量上限 >= {batch_cap}")

# ---------- 2. embedding 串行延迟 ----------
section("2. embedding 串行延迟（batch=10 × 10 次）")
lat = []
for i in range(10):
    ok, dt, _ = embed(["产品原型问题模式测试文本 %d" % i] * 10)
    if ok:
        lat.append(dt)
    time.sleep(0.2)
if lat:
    print(f"   n={len(lat)}  mean={st.mean(lat):.2f}s  p50={st.median(lat):.2f}s  "
          f"min={min(lat):.2f}s  max={max(lat):.2f}s")

# ---------- 3. chat 串行延迟（模拟 Stage2/L3 调用）----------
section("3. chat 串行延迟（qwen-plus, max_tokens=200 × 8 次）")
clat, ctok = [], []
PROMPT = ("你是产品分析师。判断下面两个问题描述是否指向同一个问题，"
          "只回答 same 或 different，并给一句理由。\n"
          "A: 吸盘仅在玻璃镜面等绝对光滑面有效，其他材质吸不住\n"
          "B: 低温环境下吸盘硅胶变硬，冬季车内吸附失效")
for i in range(8):
    ok, dt, r = chat(PROMPT)
    if ok:
        clat.append(dt)
        ctok.append(r.get("usage", {}).get("total_tokens", 0))
    else:
        print("   FAIL", r[:110])
    time.sleep(0.3)
if clat:
    print(f"   n={len(clat)}  mean={st.mean(clat):.2f}s  p50={st.median(clat):.2f}s  "
          f"max={max(clat):.2f}s  平均 tokens={st.mean(ctok):.0f}")

# ---------- 4. 并发探测：是否触发限流 ----------
section("4. 并发探测（chat 并发 1 / 2 / 4 / 8）")
conc_result = {}
for c in (1, 2, 4, 8):
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=c) as ex:
        res = list(ex.map(lambda i: chat(PROMPT, 120), range(c * 2)))
    wall = time.time() - t0
    okn = sum(1 for ok, _, _ in res if ok)
    err = [r for ok, _, r in res if not ok]
    rate_limited = sum(1 for e in err if "429" in str(e) or "Throttl" in str(e) or "limit" in str(e).lower())
    conc_result[c] = (okn, len(res), wall, rate_limited)
    print(f"   并发={c}  成功 {okn}/{len(res)}  用时 {wall:.1f}s  "
          f"吞吐 {okn/wall:.2f} 次/秒  限流 {rate_limited}")
    if err:
        print(f"      首个错误: {str(err[0])[:120]}")
    time.sleep(1)

# ---------- 5. 推算 ----------
section("5. 据实测推算（PRD §11.1）")
if clat:
    per = st.mean(clat)
    best_c = max(conc_result, key=lambda c: conc_result[c][0] / conc_result[c][2])
    best_tp = conc_result[best_c][0] / conc_result[best_c][2]
    print(f"   单次 chat 平均 {per:.2f}s")
    print(f"   最佳并发 {best_c}，吞吐 {best_tp:.2f} 次/秒")
    for label, calls in (("周度增量", 1300), ("冷启动 26 分区", 34000)):
        ser = calls * per / 3600
        par = calls / best_tp / 3600
        print(f"   {label:<16} {calls:>6} 次  串行 {ser:>6.1f}h   并发{best_c} {par:>6.1f}h")
    print("\n   建议：voc/llm 并发限值 =", best_c if not any(
        conc_result[c][3] for c in conc_result) else "1（实测触发限流）")

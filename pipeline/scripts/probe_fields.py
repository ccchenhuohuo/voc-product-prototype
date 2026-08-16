#!/usr/bin/env python3
"""字段探针：验证云听导出里产品挂靠字段的真实填充率（不写库、不调 LLM）。

要回答的问题：
  1. 社媒数据到底有没有 SPU_/型号_/SKU_ 挂靠？——决定社媒老品迭代能否落到具体产品
  2. 电商侧 SPU_/SKU_ 的填充率有多高？——决定「迭代对象」主键取 SPU 还是型号
  3. 品名_ 等 list 字段的真实形态（现在被当标量存，库里已出现两品名挤一格的脏值）

走生产同一条导出路径（yunting.export_slice + read_xlsx），否则探到的不算数。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python scripts/probe_fields.py

前置条件：
  Python 3.11+ 及项目依赖已安装；.env 中已配置云听导出所需凭据且已按上例导出
  到进程环境；运行主机可访问云听 API。探测窗口由本文件的 WINDOW 常量指定；
  脚本不写库、不调用 LLM。
"""
from __future__ import annotations
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import config as C, yunting  # noqa: E402
from voc_analytics.ingest import read_xlsx  # noqa: E402

# 关注的字段：产品挂靠 + 分类 + 属性
WATCH = ["SPU_", "SKU_", "型号_", "商品ID_", "SKUID_", "品名_", "品类_", "模型_",
         "唯迹一级类目_", "适用主机类型_", "新品上市时间_", "产品定级_",
         "本竞品_", "品牌_", "价格_", "全局标签"]

# 短窗口即可——探的是「字段有没有值」，不是统计分布
WINDOW = ("2026-08-01 00:00:00", "2026-08-08 00:00:00")


def probe(label: str, qtype: str, tf: dict | None) -> None:
    print(f"\n{'='*66}\n{label}（{qtype}） 窗口 {WINDOW[0][:10]} ~ {WINDOW[1][:10]}\n{'='*66}")
    # backfill 模式：零命中只告警不抛，探针不该因为某窗口没数据就崩
    raw, meta = yunting.export_slice(qtype, WINDOW[0], WINDOW[1], tf, mode="backfill")
    print(f"命中 {meta['matched']} 条 / 导出 {meta['exported']} 条")
    if raw is None:
        print("!! 无数据，换窗口再试")
        return
    blobs = raw if isinstance(raw, tuple) else (raw,)
    rows = [r for b in blobs if b for r in read_xlsx(b)]
    if not rows:
        print("!! 解析后 0 行")
        return

    n = len(rows)
    print(f"解析 {n} 行\n")
    print(f"{'字段':<16}{'填充率':>10}   样例值")
    print("-" * 66)
    for f in WATCH:
        vals = [str(r.get(f) or "").strip() for r in rows]
        filled = [v for v in vals if v and v.lower() not in ("none", "nan", "[]")]
        rate = 100 * len(filled) / n
        uniq = len(set(filled))
        sample = filled[0][:40] if filled else "—"
        multi = sum(1 for v in filled if "," in v or ";" in v)
        flag = f"  ⚠多值{multi}" if multi else ""
        print(f"{f:<16}{rate:>7.1f}%  {uniq:>4}种  {sample}{flag}")

    # 挂靠深度：一行同时有几层产品标识
    print(f"\n{'产品挂靠深度':<20}行数    占比")
    print("-" * 40)
    depth = Counter()
    for r in rows:
        has = tuple(f for f in ("SPU_", "SKU_", "型号_", "品名_", "品类_")
                    if str(r.get(f) or "").strip() not in ("", "None", "nan", "[]"))
        depth[has or ("无任何挂靠",)] += 1
    for k, v in depth.most_common(6):
        print(f"{' + '.join(k):<20}{v:>5}  {100*v/n:>5.1f}%")


if __name__ == "__main__":
    print(f"探针启动 {datetime.now():%H:%M:%S} · 只读导出，不写库、不调 LLM")
    probe("电商评论", "COMMENT", C.COMMENT_FILTER)
    probe("社交媒体", "SOCIAL", C.SOCIAL_FILTER)
    print("\n完成。")

#!/usr/bin/env python3
"""check_quotes / check_count_claim 单测。

用例全部取自冷启动实测数据，不是构造的：
  · OPP-E1DCDBD5DA —— evi_total=91 而现象段写「全量证据仅此1条」，旧正则漏判
  · 社媒 13.2% 引文对不上原文，多为「译成中文后仍打引号」
  · 电商的假阳性风险：混引号跨句抓取、省略号压缩、外语原声

用法：
  cd pipeline && python3 tests/validate_quotes.py

前置条件：
  Python 3.11+ 及项目依赖已安装；无需数据库、环境变量或网络。
"""
from __future__ import annotations
import sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from voc_analytics.stages import validate  # noqa: E402

P = F = 0


def chk(name: str, ok: bool, detail: str = "") -> None:
    global P, F
    if ok:
        P += 1; print(f"  PASS  {name}")
    else:
        F += 1; print(f"  FAIL  {name}  {detail}")


ITEMS = [
    {"snippet": "se partió en dos al segundo día de utilizarlo"},
    {"snippet": "broke after 2 uses"},
    {"content": "装上后手机壳太厚完全吸不住，试了三次都掉下来", "content_zh": ""},
    {"content": "the magnet is not strong enough to hold my phone",
     "content_zh": "磁力不足以吸住我的手机"},
]
IDX = [0, 1, 2, 3]


def ph(t: str) -> dict:
    return {"desc_phenomenon": t}


print("=== check_quotes ===")

chk("逐字引用外语原声通过",
    validate.check_quotes(ph("有用户称‘broke after 2 uses’。"), ITEMS, IDX) == [])

chk("逐字引用中文原声通过",
    validate.check_quotes(ph("有用户称‘装上后手机壳太厚完全吸不住’。"), ITEMS, IDX) == [])

chk("引用 content_zh 译文字段通过（该字段是上游译文，不是模型编造）",
    validate.check_quotes(ph("有用户称‘磁力不足以吸住我的手机’。"), ITEMS, IDX) == [])

chk("省略号压缩、片段均可溯源则通过",
    validate.check_quotes(ph("有用户称‘装上后手机壳太厚…都掉下来’。"), ITEMS, IDX) == [])

chk("【核心】把外语原声译成中文后仍打引号 -> 打回",
    len(validate.check_quotes(ph("有用户称‘用了两次就断了’。"), ITEMS, IDX)) == 1)

chk("凭空编造的原声 -> 打回",
    len(validate.check_quotes(ph("有用户称‘QC4+设备只能跑5V/2A，标称65W形同虚设’。"),
                              ITEMS, IDX)) == 1)

chk("把品名字段当原声引用 -> 打回（product_name 不进语料，故必然对不上）",
    len(validate.check_quotes(ph("有用户称‘登山三脚架-大三脚架’存在问题。"), ITEMS, IDX)) == 1)

chk("混引号相邻两句不被跨句抓取（假阳性回归）",
    validate.check_quotes(ph("有用户称‘broke after 2 uses’、‘the magnet is not strong enough’。"),
                          ITEMS, IDX) == [])

chk("无引号时不报错",
    validate.check_quotes(ph("多款产品在极短周期内发生断裂。"), ITEMS, IDX) == [])

chk("四字以内不视为原声引用（术语、代号）",
    validate.check_quotes(ph("所谓‘断裂’是指…"), ITEMS, IDX) == [])

# 阈值 5 字是刻意的取舍：短引号多是术语强调（‘断裂’‘吸不住’‘S支架类’）而非
# 原声引用，一并拦会造成大量假阳性。代价是 4 字以内的字段名引用漏过——
# 那是观感问题，不构成伪造用户原声。
chk("阈值边界：4 字字段名漏过属已知取舍，不算回归",
    validate.check_quotes(ph("有用户称‘S支架类’存在问题。"), ITEMS, IDX) == [])

chk("错误信息里带上出问题的那句，重试 prompt 才有的放矢",
    "用了两次就断了" in validate.check_quotes(ph("有用户称‘用了两次就断了’。"), ITEMS, IDX)[0])

chk("大量幻觉引文时截断到 6 条，避免重试 prompt 爆长",
    len(validate.check_quotes(
        ph("".join(f"‘编造原声第{i}条内容占位’" for i in range(12))), ITEMS, IDX)) == 6)

print("\n=== check_count_claim ===")

chk("数字在前、总数相符 -> 通过",
    validate.check_count_claim(ph("全部 4 条证据均指向同一失效。"), 4) == [])

chk("数字在前、总数不符 -> 打回",
    len(validate.check_count_claim(ph("全部 1 条证据均指向同一诉求。"), 15)) == 1)

chk("【核心】数字在后「全量证据仅此1条」而实际 91 条 -> 打回（旧正则漏判）",
    len(validate.check_count_claim(ph("全量证据仅此1条，内容为正向情绪表达。"), 91)) >= 1)

chk("「证据共 3 条」语序 -> 打回",
    len(validate.check_count_claim(ph("证据共 3 条，均涉及磁力衰减。"), 8)) >= 1)

chk("「仅此 1 条」无「证据」二字 -> 打回",
    len(validate.check_count_claim(ph("有效反馈仅此 1 条。"), 20)) >= 1)

chk("数字在后且相符 -> 通过",
    validate.check_count_claim(ph("全量证据仅此 5 条。"), 5) == [])

chk("增量表述「另有 19 条」不误判（电商真实文案回归）",
    validate.check_count_claim(
        ph("包括四条原声。另有19条类似反馈明确提及‘broke’‘snapped’等断裂表述。"), 24) == [])

print("\n=== check_problem_mode ===")
# 左边这些取值来自冷启动实测：593 条机会点里 532 条（89.7%）落在前四个值上，
# 导致 mode_vec 只有 63 个不同向量，去重层全线失效。
for pm, should_pass in [("需求缺口类", False), ("需求缺口", False),
                        ("失效模式类", False), ("失效模式", False),
                        ("性能不足类", False), ("未命名模式", False),
                        ("尺寸大小", False),           # 过短，撑不起去重语义
                        ("", False),
                        ("支撑腿在正常承重下从根部断裂", True),
                        ("吸盘在磨砂与曲面材质上无法建立初始吸附", True),
                        ("缺少可单独购买的快挂带环配件", True)]:
    ok = not validate.check_problem_mode({"problem_mode": pm})
    chk(f"problem_mode {pm!r} -> {'通过' if should_pass else '打回'}", ok == should_pass)

print(f"\n验收: PASS={P} FAIL={F}")
sys.exit(1 if F else 0)

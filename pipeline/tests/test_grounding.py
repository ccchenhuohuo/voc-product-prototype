#!/usr/bin/env python3
"""Stage 4 grounding 闸门的纯内存回归测试。"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import config as C  # noqa: E402
from voc_analytics import llm  # noqa: E402
from voc_analytics.config import RunCtx  # noqa: E402
from voc_analytics.pipeline import generation_reconciliation  # noqa: E402
from voc_analytics.stages import generate, validate  # noqa: E402


def _item(text: str, **values: object) -> dict:
    return {"content": text, **values}


def _grounding_errors(obj: dict, item: dict) -> list[str]:
    items, idx_map = [item], [0]
    return (
        validate.check_orphan_claims(obj, items, idx_map)
        + validate.check_polarity(obj, items, idx_map)
        + validate.check_title_subject(obj, items, idx_map)
        + validate.check_short_evidence(obj, items, idx_map)
    )


_C_CASES = [
    pytest.param(
        _item(
            "did a deep dive on these tripods for needing a run and gun light weight travel\n"
            "tripod, landed on the Ulanzi F38 Quick Release Video Travel Tripod 3318 - very\n"
            "happy I made that choice. very fast setup",
            sentiment="正面",
        ),
        {"title": "新增快速展开能力（单手3秒内完成收拢态到稳定支撑态）",
         "problem_mode": "展开耗时过长导致错过关键画面"},
        id="C1-positive-fast-setup-reversed-with-3-seconds",
    ),
    pytest.param(
        _item(
            "优篮子这款磁吸的三脚架…超级无敌方便…卓尔最高34cm，优篮子最高不到20cm…"
            "小小的也比较方便",
            sentiment="正面",
        ),
        {"title": "推出小型化便携形态（整机尺寸≤15cm）",
         "problem_mode": "体积过大导致收纳与快速部署不便"},
        id="C2-positive-small-tripod-reversed-with-15cm",
    ),
    pytest.param(
        _item("索尼 Nex 5N转接器加优篮子闪光灯，按快门也不闪"),
        {"title": "新增按快门不闪功能（实时取景无频闪干扰）"},
        id="C3-flash-failure-reversed-into-feature",
    ),
    pytest.param(
        _item("优篮子的灯，磁吸的很好用3档调光…好用的…就是发现阅星瞳后背的磁吸咋好像不居中"),
        {"title": "磁吸后背配件：新增居中定位能力（磁吸阵列中心对齐）"},
        id="C4-third-party-back-magnet",
    ),
    pytest.param(
        _item("还好我买了小隼TagBatt电池 超过100米自动提醒，手机直接弹窗"),
        {"title": "摄影灯光品类：新增超过100米自动提醒能力"},
        id="C5-third-party-battery-cross-category",
        marks=pytest.mark.xfail(strict=True, reason=(
            "【已知缺口，非退化】C5 原先唯一的命中来自短证据规则罚标题主体前缀"
            "「摄影灯光品类」。但真库 3934 条标定显示，该前缀是标题格式强制要求的"
            "归类词，罚它会误杀 658 条（占短证据命中的 64.8%），因此 2026-08-17 起"
            "标题只扫冒号之后（见 validate._title_body）。"
            "C5 的其余部分无法机械识别：「100米」在证据里确有来源，标题也没点名"
            "「小隼」，第三方分支同样不响；而「提醒能力」的词干「提醒」在证据中存在。"
            "要真正拦住这类跨品类误归，需要产品→品类的结构化映射（社媒无 product_name"
            "时不可得），靠字符串规则无法把 C5 与 P1「摄影灯配件品类：推出 P4P 专用"
            "保护壳」区分开——后者的品类同样不在证据字面里，却是正确的归类推断。"
            "保留本用例作为该缺口的活记录；若日后实现了品类映射使其通过，"
            "strict xfail 会转红，届时删掉本标记。")),
    ),
    pytest.param(
        _item("唯一扣分的是這個la08，快拆板要用工具鎖"),
        {"title": "填补LA08专属快装扣分系统",
         "problem_mode": "带刻度调节的扣分底座"},
        id="C6-rating-word-misread-as-component",
    ),
    pytest.param(
        _item("Tive um problemão com ele, me deixou na mão demais!"),
        {"title": "修复表面涂层附着力不足（易刮花、掉漆）"},
        id="C7-portuguese-vague-failure-invented-coating",
    ),
    pytest.param(
        _item("Comprei uma led dessa, não deu 4 meses, o plástico ressecou e furou"),
        {"title": "推出可替换灯头组件（含色温/亮度调节模块）"},
        id="C8-portuguese-plastic-failure-invented-module",
    ),
    pytest.param(
        _item("@ulanzi_europe have 100w version - that's a must have! I have 40w and it's not enough"),
        {"title": "新增40W以上功率档位（支持50W/60W可选输出）"},
        id="C9-unsupported-50w-60w",
    ),
    pytest.param(
        _item("电竞用不了，办公用着还着急"),
        {"title": "推出专为电竞场景优化的轻量化快拆支撑系统（含低重心三轴云台+防滑脚垫）"},
        id="C10-short-evidence-invented-gimbal-and-feet",
    ),
    pytest.param(
        _item("为什么不出续航手柄啊"),
        {"title": "推出可扩展电池的摄影手柄（支持热插拔+双电协同）"},
        id="C11-short-evidence-invented-hot-swap",
    ),
    pytest.param(
        _item("背胶在天热的时候会脱胶，能解决这个吗"),
        {"title": "新增耐高温背胶（适配35℃以上环境）"},
        id="C12-unsupported-35c",
    ),
]


@pytest.mark.parametrize(("item", "obj"), _C_CASES)
def test_confirmed_bad_outputs_are_reported(item: dict, obj: dict) -> None:
    assert _grounding_errors(obj, item), obj["title"]


_P_CASES = [
    pytest.param(
        _item("p4p的保护壳什么时候搞出来"),
        {"title": "推出 P4P 专用保护壳"},
        id="P1-p4p-case",
    ),
    pytest.param(
        _item("360度カメラ用に1/4ネジでもお願いします"),
        {"title": "1/4英寸螺纹配件：新增360相机兼容支持"},
        id="P2-japanese-quarter-inch-thread",
    ),
    pytest.param(
        _item("我急需这个Luna的银色三脚架磁吸背板处加三个东西：1/4螺口，折叠2爪，Action卡口"),
        {"title": "新增1/4英寸标准螺纹接口"},
        id="P3-quarter-inch-thread",
    ),
    pytest.param(
        _item("iPhoneのpro maxだと少し不安定かなぁ"),
        {"title": "新增大尺寸机型稳定适配能力（iPhone Pro Max 等）"},
        id="P4-japanese-iphone-pro-max",
    ),
    pytest.param(
        _item("缺少20L灰色版本的Ulanzi F08轻量出行双肩包"),
        {"title": "Ulanzi F08：新增 20L 灰色版本"},
        id="P5-20l-grey-f08",
    ),
    pytest.param(
        _item("これ以上の明るさにすると点滅"),
        {"title": "修复亮度调节异常引发的闪烁"},
        id="P6-japanese-brightness-flicker",
    ),
]


@pytest.mark.parametrize(("item", "obj"), _P_CASES)
def test_confirmed_good_outputs_are_not_blocked(item: dict, obj: dict) -> None:
    assert _grounding_errors(obj, item) == []


def test_length_unit_equivalence_m_cm() -> None:
    assert validate._num_tokens("1.8m") == validate._num_tokens("1.8米")
    assert validate._num_tokens("1.8m") == validate._num_tokens("180cm")


def test_length_unit_equivalence_inch_mm() -> None:
    assert validate._num_tokens("1/4英寸") == validate._num_tokens("6.35mm")


def test_volume_unit_equivalence_litre() -> None:
    assert validate._num_tokens("20L") == validate._num_tokens("20升")


@pytest.mark.parametrize(
    "text",
    [
        "40W", "20瓦", "1kW", "35℃", "35度", "5600K", "1.8m", "1.7米",
        '1英寸', '1inch', '1"', "20L", "20升", "800lm", "800流明", "5V",
        "2A", "5000mAh", "3Nm", "3档", "3轴", "3爪", "800目", "2kg",
        "380g", "25.4mm",
    ],
)
def test_expanded_unit_table_is_tokenized(text: str) -> None:
    assert validate._num_tokens(text), text


def test_social_content_and_translation_are_both_grounding_sources() -> None:
    items = [{"content": "this light has a 40W output", "content_zh": "线长是1.8米"}]
    obj = {"title": "摄影灯：新增40W功率档位",
           "problem_mode": "电源线长度达到180cm便于布置"}
    assert validate.check_orphan_claims(obj, items, [0]) == []


def test_valid_portuguese_to_chinese_paraphrase_is_not_rejected_by_spelling() -> None:
    item = _item("Tive um problemão com ele, me deixou na mão demais!")
    obj = {"title": "摄影配件：修复使用中失效问题",
           "problem_mode": "产品在使用过程中发生笼统失效问题"}
    assert _grounding_errors(obj, item) == []


@pytest.mark.parametrize(
    ("brand", "evidence"),
    [
        ("阅星瞳", "发现阅星瞳后背的磁吸不居中"),
        ("永诺", "永诺闪光灯按快门不闪"),
        ("哈苏", "哈苏相机无法安装"),
        ("卓尔", "卓尔三脚架最高34cm"),
        ("神牛", "神牛摄影灯亮度高"),
    ],
)
def test_third_party_subjects_are_extracted_from_evidence_not_a_fixed_table(
    brand: str, evidence: str,
) -> None:
    errors = validate.check_title_subject(
        {"title": f"{brand}：修复主体问题"}, [_item(evidence)], [0])
    assert errors and "第三方" in errors[0]


@pytest.mark.parametrize("brand", ["VIJIM", "ULANZI", "优篮子",
                                  "宙比", "JOBY", "小隼", "FALCAM"])
def test_own_brand_aliases_are_not_third_party(brand: str) -> None:
    evidence = f"{brand}摄影灯需要优化"
    assert validate.check_title_subject(
        {"title": f"{brand}：优化摄影灯"}, [_item(evidence)], [0]) == []


def _report_mode_obj() -> dict:
    return {
        "title": "背胶：新增35℃以上耐高温能力",
        "problem_mode": "背胶在炎热天气下发生脱胶问题",
        "desc_phenomenon": (
            "有用户反馈背胶在天热时会脱胶，并询问能否解决。该反馈只说明炎热天气下的"
            "脱胶现象，没有提供可量化的温度阈值、材料规格或具体使用时长。"
        ),
        "desc_attribution": "推断这一反馈指向背胶在炎热环境下的粘接稳定性需要进一步确认与评估。",
        "citations": [],
    }


def _stub_stage2(monkeypatch, obj: dict) -> list[str]:
    prompts: list[str] = []

    def fake_chat(prompt: str, **_kwargs):
        prompts.append(prompt)
        return dict(obj), {"tokens": 0}

    monkeypatch.setattr(generate.llm, "chat_json", fake_chat)
    return prompts


def test_report_mode_records_but_does_not_reject(monkeypatch) -> None:
    monkeypatch.setattr(C, "GROUNDING_ENFORCE", False)
    obj = _report_mode_obj()
    prompts = _stub_stage2(monkeypatch, obj)
    ctx = RunCtx("grounding-report", "2026-W34")

    result = generate.write_prototype(
        [_item("背胶在天热的时候会脱胶，能解决这个吗")],
        [0], "高温脱胶", "新品创新", {"category": "摄影配件"}, ctx)

    metrics = ctx.metrics["generation"]
    assert result["_needs_review"] is False
    assert len(prompts) == 1
    assert metrics["grounding_checked_attempts"] == 1
    assert metrics["grounding_flagged_attempts"] == 1
    # 本用例验的是「报告模式只计数、不作废」这一行为，不钉死具体命中条数——
    # 后者随规则集调整而变（2026-08-17 收紧抽象后缀后本夹具由 2 条变 1 条，
    # 保留的是更准的 orphan「35℃ 未溯源」）。规则覆盖面由 C 组/P 组用例负责。
    assert metrics["grounding_issue_count"] >= 1
    assert metrics.get("grounding_rejected_groups", 0) == 0


def test_enforce_mode_retries_then_uses_existing_rejection_path(monkeypatch) -> None:
    monkeypatch.setattr(C, "GROUNDING_ENFORCE", True)
    prompts = _stub_stage2(monkeypatch, _report_mode_obj())
    ctx = RunCtx("grounding-enforce", "2026-W34")

    with pytest.raises(llm.LLMError, match="grounding 三次校验仍失败"):
        generate.write_prototype(
            [_item("背胶在天热的时候会脱胶，能解决这个吗")],
            [0], "高温脱胶", "新品创新", {"category": "摄影配件"}, ctx)

    metrics = ctx.metrics["generation"]
    assert len(prompts) == 3
    assert metrics["grounding_checked_attempts"] == 3
    assert metrics["grounding_enforced_attempts"] == 3
    assert metrics["grounding_flagged_attempts"] == 3
    # 同上：只断言「三轮都被判有问题」这一行为，不钉死每轮命中条数。
    assert metrics["grounding_issue_count"] >= 3
    assert metrics["grounding_rejected_groups"] == 1


def test_grounding_rejection_closes_existing_generation_ledger() -> None:
    ctx = RunCtx("grounding-reconcile", "2026-W34")
    ctx.metric_update(
        ("generation",),
        planned_buckets=1, completed_buckets=1, failed_buckets=0,
        cancelled_buckets=0, planned_groups=1, completed_groups=0,
        failed_groups=1, cancelled_groups=0, planned_persistence=0,
        completed_persistence=0, failed_persistence=0, cancelled_persistence=0,
        grounding_checked_attempts=3, grounding_enforced_attempts=3,
        grounding_flagged_attempts=3, grounding_issue_count=6,
        grounding_orphan_issues=3, grounding_polarity_issues=0,
        grounding_title_subject_issues=0, grounding_short_evidence_issues=3,
        grounding_rejected_groups=1,
    )

    result = generation_reconciliation(ctx)

    assert result["planned_groups"] == (
        result["completed_groups"] + result["failed_groups"]
        + result["cancelled_groups"])
    assert result["grounding_rejected_groups"] <= result["failed_groups"]
    assert result["grounding_complete"] is True
    assert result["complete"] is True

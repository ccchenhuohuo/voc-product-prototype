"""清洗与证据炸开（PRD v8 §4.2）。

三个云听侧的已知数据缺陷在此修复：
  · 原声片段尾部字符重复（悪い悪い / 消えるえる / ましたした）—— 按语种分支，
    早期只处理非 ASCII 导致英德西完全不生效
  · 标签错标（5星"发货很快"被打成"磁吸/负面"）—— 高星+负面交叉校验为 low_conf
  · 原声片段数组里的 JSON 空值哨兵（`[null, 支架松动, null]`）—— 按逗号切开后
    是字符串 "null"，会被当成片段正文一路带进提示词
"""
from __future__ import annotations
import re
from typing import Any

CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
_WORD = re.compile(r"[A-Za-zÀ-ÿ]+")
_SPU_SEPARATORS = re.compile(
    r"[\s\-\u00ad\u2010-\u2015\u2212\ufe63\uff0d]+"
)


def _is_cjk_text(s: str) -> bool:
    return bool(CJK.search(s))


# 云听把 JSON 空值原样打进平行数组：`[null, 支架松动, null]`。parse_array 按逗号
# 切开后得到字符串 "null"，非空、非空白，于是 NULLIF(btrim(...), '') 挡不住、
# COALESCE(snippet, content) 也不会回落到正文——LLM 收到的证据正文就是四个字母
# 的 "null"。2026-08-17 实测 148,934 条证据里有 23,608 条（15.9%）中招，
# 「需求缺口」渠道 64% 的入池行受影响，而其中 99.9% 的消息正文原本就在。
# 只出现在原声片段一列；情感、标签、tax_path、brands、content_type、spu 均干净。
_NULL_TOKENS = frozenset({"null", "none", "nil", "nan", "undefined"})


def null_token(v: Any) -> str | None:
    """把 JSON 空值哨兵还原成真正的空值，保留位置语义。

    平行数组按下标一一对应，所以只能就地置空，绝不能把元素删掉——
    删一个元素会让其后所有标签的情感与片段整体错位一格。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in _NULL_TOKENS:
        return None
    return s


def fix_tail_repeat(s: str | None) -> str | None:
    """修复尾部 1–4 字符/词的重复截取缺陷。CJK 按字符，拉丁按词与词尾。"""
    if not s:
        return s
    t = s.strip()
    if len(t) < 4:
        return t

    if _is_cjk_text(t):
        for k in (4, 3, 2, 1):
            if len(t) > 2 * k and t[-k:] == t[-2 * k:-k]:
                return t[:-k]
        return t

    # 拉丁语系：先查整词重复（"lange lange"），再查词尾重复（"abgebrochenn"）
    words = t.split()
    if len(words) >= 2 and words[-1].lower() == words[-2].lower():
        return " ".join(words[:-1])
    last = words[-1] if words else ""
    m = _WORD.fullmatch(last)
    if m and len(last) > 4:
        for k in (3, 2, 1):
            if len(last) > 2 * k and last[-k:].lower() == last[-2 * k:-k].lower():
                words[-1] = last[:-k]
                return " ".join(words)
    return t


def parse_array(v: Any) -> list[str]:
    """云听导出里的 '[a, b]' 是字符串不是真数组。

    已知限制：值本身若含英文逗号也会被切开；云听当前格式没有转义信息，
    这里保持简单规则，避免为无法可靠判定的边界引入猜测。
    """
    if v is None:
        return []
    s = str(v).strip()
    if not s or s == "[]":
        return []
    return [x.strip() for x in s.strip("[]").split(",") if x.strip()]


def normalize_spu_code(code: Any) -> str | None:
    """统一 SPU 的空白、连字符与大小写，不覆盖可回溯的源 token。"""
    if code is None:
        return None
    normalized = _SPU_SEPARATORS.sub("", str(code)).upper()
    return normalized or None


def clean_spu_codes(v: Any) -> dict[str, list[str]]:
    """规范化 SPU，并把规范化后为空的原始 token 单独留痕。

    **不按任何名单筛选。** 云听的 SPU 是依照本公司产品体系打的标，
    实测社媒侧 2,561 条带 SPU 的消息 brands 全部含本品，零例外；
    而 ``NP-FZ100`` ``EN-EL15`` 这类看似第三方的编码，是本品兼容电池
    沿用相机电池规格命名（如 Tagbatt），并非竞品混入。曾按「电商出现过的
    SPU」做白名单，会为拦 6 条而误删约 1,380 条本品社媒消息。

    「社媒独有 SPU 要不要生成产品卡」是展示层口径，由 ``voc_spu``
    物化视图决定；抽取层只忠实记录，不在此销毁数据。

    结果按首次出现顺序去重，``spu_raw`` 保留 ``parse_array`` 后的原始 token。
    """
    raw_values = parse_array(v)
    matched: list[str] = []
    unmatched: list[str] = []
    seen: set[str] = set()

    for raw in raw_values:
        normalized = normalize_spu_code(raw)
        if normalized is None:
            unmatched.append(raw)
            continue
        if normalized not in seen:
            seen.add(normalized)
            matched.append(normalized)

    return {
        "spu": matched,
        "spu_raw": raw_values,
        "spu_unmatched": unmatched,
    }


_COUNTRY_NORM = {
    "美国": "US", "日本": "JP", "德国": "DE", "英国": "GB", "法国": "FR",
    "西班牙": "ES", "墨西哥": "MX", "加拿大": "CA", "意大利": "IT",
    "澳大利亚": "AU", "中国": "CN", "巴西": "BR", "韩国": "KR",
    "荷兰": "NL", "波兰": "PL", "瑞典": "SE", "印度": "IN",
}


def norm_country(v: Any) -> str | None:
    """云听的 country 混用 ISO 代码与中文名（JP / 美国 / 墨西哥同批出现），
    入库统一为 ISO 代码，避免下游按字面比对时把同一国家当成两个。"""
    s = scalar(v)
    if not s:
        return None
    s = s.split(",")[0].strip()
    return _COUNTRY_NORM.get(s, s.upper() if len(s) <= 3 else s)


def scalar(v: Any) -> str | None:
    """带方括号的单值字段，如 品类_ = '[S支架类]'。"""
    if v is None:
        return None
    s = str(v).strip().strip("[]").strip()
    return s or None


def first_value(v: Any) -> str | None:
    """多值导出字段需要单值语义时取首值，不能把列表逗号拼进 text 列。"""
    values = parse_array(v)
    return values[0] if values else None


def own_brand(v: Any) -> bool | None:
    """本竞品未标注时必须留 NULL：社媒约九成没有归属，一旦当 false 就把
    「不知道」写成了「竞品」，本品筛选会静默丢掉这批数据。"""
    values = parse_array(v)
    if not values:
        return None
    return any("本品" in value for value in values)


_GRADE_ORDER = ("PS", "S", "A", "B", "C", "D", "其他")


def highest_grade(v: Any) -> str | None:
    """产品定级多值时按 PS > S > A > B > C > D > 其他取最高级。"""
    values = parse_array(v)
    if not values:
        return None
    rank = {grade: i for i, grade in enumerate(_GRADE_ORDER)}

    def grade_key(value: str) -> int:
        # 云听当前输出带「级」后缀；同时兼容无后缀值，结果仍保留源值。
        normalized = value[:-1] if value.endswith("级") else value
        return rank.get(normalized, len(rank))

    return min(enumerate(values), key=lambda pair: (grade_key(pair[1]), pair[0]))[1]


def explode(row: dict, tax, src_line: str) -> tuple[list[dict], int]:
    """把三个平行数组按位置炸开成证据三元组。

    返回 (证据列表, 是否错位)。seq 即数组下标，是幂等键。
    实测 3 个月内错位为 0，若出现即说明上游变更（§9.1 阻断项）。
    """
    tags = parse_array(row.get("全局标签"))
    sents = parse_array(row.get("标签情感,与全局标签一一对应"))
    snips = parse_array(row.get("标签原声片段,与全局标签一一对应"))
    misaligned = int(bool(tags) and len(tags) != len(sents))

    star = row.get("评论星级")
    try:
        star_f = float(star) if star not in (None, "") else None
    except (TypeError, ValueError):
        star_f = None

    out = []
    from .taxonomy import split_tax
    for i, raw_tag in enumerate(tags):
        info = tax.info(raw_tag)
        # 固定按路径位置拆层，不能过滤空段，否则缺层时后续层级会整体左移。
        tax_stage, tax_domain, tax_sub, tax_leaf = split_tax(info["tax_path"])
        sent = null_token(sents[i] if i < len(sents) else None)
        # 哨兵要在留痕之前还原：把 "null" 存进 snippet_raw 不是留痕，
        # 是把「这条标签没有原声片段」记成了「原声片段的内容是 null」。
        snip_raw = null_token(snips[i] if i < len(snips) else None)
        # 交叉校验：负面标签 + 4-5 星 => 疑似误标
        low_conf = bool(sent == "负面" and star_f is not None and star_f >= 4)
        out.append({
            "message_id": row.get("消息ID"),
            "seq": i,
            "tag_raw": raw_tag,
            "tag": info["tag"],
            "sentiment": sent,
            "snippet": fix_tail_repeat(snip_raw),
            "snippet_raw": snip_raw,
            "tax_path": info["tax_path"],
            "tax_l1": info["tax_l1"],
            "tax_stage": tax_stage,
            "tax_domain": tax_domain,
            "tax_sub": tax_sub,
            "tax_leaf": tax_leaf,
            "is_product": info["is_product"],
            "is_scene": info["is_scene"],
            "low_conf": low_conf,
        })
    return out, misaligned


def to_message(row: dict, src_line: str, batch_id: str, dewater_all: set[str]) -> dict:
    """把一行导出映射成 voc_message。"""
    star = row.get("评论星级")
    try:
        star_f = float(star) if star not in (None, "") else None
    except (TypeError, ValueError):
        star_f = None
    ctype = [t for t in parse_array(row.get("全局标签")) if t in dewater_all]
    inter = row.get("互动量(点赞+分享+评论)")
    try:
        inter_i = int(float(inter)) if inter not in (None, "") else None
    except (TypeError, ValueError):
        inter_i = None
    spu_fields = clean_spu_codes(row.get("SPU_"))
    return {
        "message_id": row.get("消息ID"),
        "src_line": src_line,
        "platform": scalar(row.get("平台")),
        "publish_time": row.get("评论时间,格式: yyyy-MM-dd HH:mm:ss"),
        "content": (row.get("评论内容") or "")[:20000] or None,
        "content_zh": (row.get("翻译后正文") or None),
        "url": scalar(row.get("商品链接")) or scalar(row.get("链接")),
        "star": star_f,
        "category": first_value(row.get("品类_")),
        "product_name": first_value(row.get("品名_")),
        "product_id": scalar(row.get("商品ID_")),
        "product_grade": highest_grade(row.get("产品定级_")),
        **spu_fields,
        "sku": parse_array(row.get("SKU_")),
        "model": parse_array(row.get("型号_")),
        "prod_line": first_value(row.get("模型_")),
        "launch_period": first_value(row.get("新品上市时间_")),
        "is_own_brand": own_brand(row.get("本竞品_")),
        "country": norm_country(row.get("国家_")),
        "lang": scalar(row.get("语种")),
        "interactions": inter_i,
        "brands": parse_array(row.get("品牌_")),
        "content_type": ctype,
        "pull_batch_id": batch_id,
    }

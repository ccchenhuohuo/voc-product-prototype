"""Stage 4 程序化校验（PRD v8 §5.9）。

反幻觉的硬闸门。早期设计让模型自报 cited_numbers 字符串再回查证据全文，
但语料是中英日德西混合——"2 个月"在 nach 2 Monaten / 2ヶ月で 里没有字面对应，
整串比对几乎全部误判、只比裸数字又形同虚设。
改为结构化溯源：模型给出 evidence_idx + quote_span，程序只校验
"该序号存在" 与 "quote_span 是那条证据原文的连续子串"，彻底避开跨语种比对。
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from .. import config, prompts

# 标题：主体 + 冒号 + 含受控动作词的对象（动作词可在冒号后任意位置）
_TITLE_RE = re.compile(
    r"^.{2,40}[：:]\s*.{0,28}(" + "|".join(prompts.ACTIONS) + r").{0,28}$")

# 描述中疑似需要溯源的片段
_MODEL_PAT = re.compile(
    r"(iPhone\s*(?:の\s*)?(?:\d+[A-Za-z ]*|Pro\s*Max)|Galaxy\s*S?\d+[A-Za-z ]*|Mate\s*\d+[A-Za-z ]*|"
    r"Pixel\s*\d+[A-Za-z ]*|DJI\s*[A-Za-z0-9 ]+|Osmo\s*[A-Za-z0-9 ]+|Action\s*\d+\w*|"
    r"Pocket\s*\d+\w*|Insta360\s*[A-Za-z0-9 ]+|A\d{4}|ZV-?E?\d+\w*|"
    r"(?=[A-Z0-9-]*\d)[A-Z][A-Z0-9-]{2,})", re.I)

# 数值必须带量纲才进入闸门，避免把型号里的数字误当参数。最长单位放前面，
# 防止 ``mAh`` 被 ``m`` 抢先匹配。螺口/螺纹/ネジ是 1/4 英寸接口的常见省略写法，
# 只在数字（通常为分数）紧邻它们时作为英寸量纲处理。
_NUM_PAT = re.compile(
    r"(?P<value>\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?)\s*"
    r"(?P<unit>个月|个星期|小时|分钟|mAh|inch|英寸|流明|螺纹|螺口|ネジ|"
    r"kg|kW|mm|cm|Nm|lm|°C|℃|瓦|周|天|秒|年|W|g|K|m|米|[\"″]|L|升|V|A|%|星|度|档|轴|爪|目)",
    re.I,
)

_NUM_UNIT = {
    "个月": ("month", Decimal("1")),
    "个星期": ("time_s", Decimal("604800")),
    "周": ("time_s", Decimal("604800")),
    "天": ("time_s", Decimal("86400")),
    "小时": ("time_s", Decimal("3600")),
    "分钟": ("time_s", Decimal("60")),
    "秒": ("time_s", Decimal("1")),
    "年": ("year", Decimal("1")),
    "kg": ("mass_g", Decimal("1000")),
    "g": ("mass_g", Decimal("1")),
    "kw": ("power_w", Decimal("1000")),
    "w": ("power_w", Decimal("1")),
    "瓦": ("power_w", Decimal("1")),
    "℃": ("temperature_c", Decimal("1")),
    "°c": ("temperature_c", Decimal("1")),
    "度": ("degree", Decimal("1")),
    "k": ("color_temperature_k", Decimal("1")),
    "m": ("length_mm", Decimal("1000")),
    "米": ("length_mm", Decimal("1000")),
    "cm": ("length_mm", Decimal("10")),
    "mm": ("length_mm", Decimal("1")),
    "英寸": ("length_mm", Decimal("25.4")),
    "inch": ("length_mm", Decimal("25.4")),
    '"': ("length_mm", Decimal("25.4")),
    "″": ("length_mm", Decimal("25.4")),
    "螺纹": ("length_mm", Decimal("25.4")),
    "螺口": ("length_mm", Decimal("25.4")),
    "ネジ": ("length_mm", Decimal("25.4")),
    "l": ("volume_ml", Decimal("1000")),
    "升": ("volume_ml", Decimal("1000")),
    "lm": ("luminous_flux_lm", Decimal("1")),
    "流明": ("luminous_flux_lm", Decimal("1")),
    "v": ("voltage_v", Decimal("1")),
    "a": ("current_a", Decimal("1")),
    "mah": ("capacity_mah", Decimal("1")),
    "nm": ("torque_nm", Decimal("1")),
    "%": ("percent", Decimal("1")),
    "星": ("star", Decimal("1")),
    "档": ("gear_count", Decimal("1")),
    "轴": ("axis_count", Decimal("1")),
    "爪": ("claw_count", Decimal("1")),
    "目": ("mesh_count", Decimal("1")),
}


def _norm(s: str) -> str:
    """全角转半角 + 去空白，用于宽松子串比对。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).lower()


def _model_norm(s: str) -> str:
    """型号比较忽略日语属格连接词；不对一般自然语言做跨语种猜测。"""
    return _norm(s).replace("の", "")


def _decimal(text: str) -> Decimal | None:
    """解析小数或分数；无效输入不进入数值 token。"""
    try:
        compact = re.sub(r"\s+", "", text)
        if "/" in compact:
            numerator, denominator = compact.split("/", 1)
            denominator_value = Decimal(denominator)
            if not denominator_value:
                return None
            return Decimal(numerator) / denominator_value
        return Decimal(compact)
    except (InvalidOperation, ValueError):
        return None


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _num_match_token(match: re.Match[str]) -> tuple[str, str] | None:
    value = _decimal(match.group("value"))
    unit = unicodedata.normalize("NFKC", match.group("unit")).casefold()
    normalized = _NUM_UNIT.get(unit)
    if value is None or normalized is None:
        return None
    dimension, multiplier = normalized
    return _decimal_text(value * multiplier), dimension


def _num_tokens(text: str) -> set[tuple[str, str]]:
    """返回 ``(归一数值, 量纲)``，等价单位在同一空间比较。

    长度统一到 mm、质量统一到 g、体积统一到 ml、功率统一到 W；因此
    ``1.8m``/``180cm`` 与 ``1/4英寸``/``6.35mm`` 会得到同一 token。
    """
    return {
        token for match in _NUM_PAT.finditer(text or "")
        if (token := _num_match_token(match)) is not None
    }


def _evidence_parts(items: list[dict], idx_map: list[int]) -> list[str]:
    """按首次出现顺序返回该组可引文本三字段的去重并集。"""
    parts: list[str] = []
    seen: set[str] = set()
    for i in idx_map:
        item = items[i]
        for key in ("snippet", "content", "content_zh"):
            value = str(item.get(key) or "").strip()
            normalized = _norm(value)
            if value and normalized not in seen:
                seen.add(normalized)
                parts.append(value)
    return parts


def check_citations(obj: dict, items: list[dict], idx_map: list[int]) -> list[str]:
    """校验每条 citation 可溯源。idx_map 把 1-based 展示序号映射回 items 下标。"""
    errs: list[str] = []
    for c in obj.get("citations") or []:
        j = c.get("evidence_idx")
        if not isinstance(j, int) or not (1 <= j <= len(idx_map)):
            errs.append(f"citation evidence_idx={j} 越界"); continue
        src = items[idx_map[j - 1]]
        text = _norm(src.get("snippet") or src.get("content") or "")
        span = _norm(c.get("quote_span") or "")
        if not span:
            errs.append(f"citation[{j}] 缺 quote_span"); continue
        if span not in text:
            errs.append(f"citation[{j}] quote_span 不是该证据原文的子串: "
                        f"{str(c.get('quote_span'))[:40]}")
    return errs


def check_orphan_claims(obj: dict, items: list[dict] | None = None,
                        idx_map: list[int] | None = None) -> list[str]:
    """标题、问题模式与现象段中的机型/数字必须可溯源。

    合法来源包括 citations 的 value、证据行的 product_name，以及该组
    snippet/content/content_zh 三字段并集。数值在归一化的值+量纲空间比较，
    不再用裸数字子串（后者会把产品编码 3318 当成「3 秒」的来源）。
    """
    sources: list[str] = []
    for citation in obj.get("citations") or []:
        value = str(citation.get("value") or "")
        unit = str(citation.get("unit") or "")
        sources.extend((value, value + unit))
    if items is not None and idx_map is not None:
        sources.extend(str(items[i].get("product_name") or "") for i in idx_map)
        sources.extend(_evidence_parts(items, idx_map))

    cited_texts = {_norm(source) for source in sources if _norm(source)}
    cited_models = {
        _model_norm(model)
        for source in sources for model in _MODEL_PAT.findall(source)
    }
    cited_numbers = set().union(*(_num_tokens(source) for source in sources)) if sources else set()
    errs: list[str] = []
    fields = ("title", "problem_mode", "desc_phenomenon")
    for field in fields:
        text = str(obj.get(field) or "")
        for model in sorted(set(_MODEL_PAT.findall(text)), key=_norm):
            normalized = _model_norm(model)
            if not any(
                normalized in source
                or (len(source) >= 3 and re.search(r"[a-z]", source)
                    and source in normalized)
                for source in cited_texts | cited_models
            ):
                errs.append(f"{field} 出现未溯源机型: {model}")
        seen_numbers: set[tuple[str, str]] = set()
        for match in _NUM_PAT.finditer(text):
            token = _num_match_token(match)
            if token is None or token in seen_numbers:
                continue
            seen_numbers.add(token)
            if token not in cited_numbers:
                errs.append(f"{field} 出现未溯源数值: {match.group(0)}")
    return errs


def check_format(obj: dict) -> list[str]:
    errs = []
    title = (obj.get("title") or "").strip()
    if not _TITLE_RE.match(title):
        errs.append(f"标题不符合公式（主体：动作词+对象）: {title[:50]}")

    ph = (obj.get("desc_phenomenon") or "").strip()
    at = (obj.get("desc_attribution") or "").strip()
    # 现象段允许较长：Prompt 明确要求引用原声，而语料是中英日德西阿混合，
    # 引 4-6 条外语原声必然超过百字。引用原声是核心价值，不为长度牺牲。
    if not (60 <= len(ph) <= 1200):
        errs.append(f"现象段长度 {len(ph)} 不在 60–1200（超长视为失控输出）")
    if not (30 <= len(at) <= 220):
        errs.append(f"归因段长度 {len(at)} 不在 30–220")
    if at and not any(at.startswith(w) or w in at[:12] for w in prompts.ATTR_STARTERS):
        errs.append("归因段未以推断类词开头")

    joined = ph + at
    for w in prompts.BANNED_WORDS:
        if w in joined:
            errs.append(f"出现无支撑程度词: {w}")
    return errs


# 描述里提到某国用户时，该国必须在该组证据的 country 集合内。
# citations 只保证引文是原文子串，不保证国别标注正确——实测出现过
# 把西班牙语原声标成「日本用户」的事实错误。
#
# 关键：证据里的 country 既有 ISO 代码（US/DE/JP）又有中文名（美国/德国），
# 必须双向归一；且只匹配【已知国家词】，不能用「任意汉字+用户」——
# 那样会把「另一用户」「还有美用户」误判成国别。
_COUNTRY_MAP = {
    "美国": "US", "美": "US", "us": "US", "united states": "US",
    "日本": "JP", "日": "JP", "jp": "JP",
    "德国": "DE", "德": "DE", "de": "DE", "germany": "DE",
    "英国": "GB", "英": "GB", "gb": "GB", "uk": "GB",
    "法国": "FR", "法": "FR", "fr": "FR",
    "西班牙": "ES", "es": "ES",
    "墨西哥": "MX", "墨": "MX", "mx": "MX",
    "加拿大": "CA", "加": "CA", "ca": "CA",
    "意大利": "IT", "it": "IT",
    "澳大利亚": "AU", "澳": "AU", "au": "AU",
    "中国": "CN", "国内": "CN", "cn": "CN", "中": "CN",
    "巴西": "BR", "br": "BR", "韩国": "KR", "kr": "KR",
    "荷兰": "NL", "nl": "NL", "波兰": "PL", "pl": "PL",
}
# 只匹配已知国家词 + 「用户」，按长度降序避免「美」抢在「美国」前
_COUNTRY_NAMES = sorted({k for k in _COUNTRY_MAP if any("\u4e00" <= c <= "\u9fff" for c in k)},
                        key=len, reverse=True)
_COUNTRY_PAT = re.compile("(" + "|".join(_COUNTRY_NAMES) + r")(?:国)?用户")
_LEAK_PAT = re.compile(r"evidence_idx\s*=|quote_span\s*=|\bcitations?\b", re.I)
_ZERO_MORE = re.compile(r"另有\s*0\s*条")


def check_country_claims(obj: dict, items: list[dict], idx_map: list[int]) -> list[str]:
    """描述中不得标注国别。

    上游 country 实测不可靠：存在内容为西班牙语而 country='JP' 的记录，
    电商侧 lang 全为空、country 混用 ISO 代码与中文名。模型忠实复述这个
    字段反而会产出「日本用户称 el soporte ha cedido」这种事实错误。
    因此 Prompt 层已禁止标注国别，此处兜底拦截；国别由下钻页呈现真实字段。
    """
    text = (obj.get("desc_phenomenon") or "") + (obj.get("desc_attribution") or "")
    names = set(_COUNTRY_PAT.findall(text))
    if not names:
        return []
    return [f"描述中标注了国别（{', '.join(sorted(names))}用户）。"
            f"上游 country 字段不可靠，描述中一律不写国别，改用「有用户称…」"]


_COUNT_CLAIM = re.compile(r"(?:全部|共|总计)?\s*(\d+)\s*条(?:证据|反馈|评价)")
# 数字在「证据」之后的语序：「全量证据仅此 1 条」「证据共 3 条」。
# 早期只有上面那条正则（数字在前），漏掉了这种写法——实测 OPP-E1DCDBD5DA
# 挂了 91 条证据，现象段写「全量证据仅此1条」却judge通过，产出整段凭空规格。
_COUNT_CLAIM_REV = re.compile(
    r"(?:全量|全部|所有)?\s*(?:证据|反馈|评价)\s*(?:仅|只|共|总计|合计)?(?:此|有)?\s*(\d+)\s*条")
# 「仅此/只有 N 条」这类总数断言（前面不一定有「证据」二字）
_COUNT_ONLY = re.compile(r"(?:仅此|仅有|只有|唯一)\s*(\d+)\s*条")


def check_count_claim(obj: dict, n_evidence: int) -> list[str]:
    """现象段声称的证据条数必须与实际一致。

    实测出现过：实际挂载 15 条，现象段却写「全部 1 条证据均指向同一诉求」——
    模型被开头一条很长的内容主导，忽略了其余 14 条。计数矛盾是这类
    「只看了部分证据」问题最容易程序化捕获的信号。

    三条正则覆盖三种语序，缺一不可：数字在前（「全部 1 条证据」）、
    数字在后（「全量证据仅此 1 条」）、无「证据」二字（「仅此 1 条」）。
    """
    text = obj.get("desc_phenomenon") or ""
    errs = []
    for m in set(_COUNT_CLAIM.findall(text)):
        claimed = int(m)
        # 允许「另有 N 条」这类增量表述（N < 总数），只拦总数声明明显不符的
        if claimed > n_evidence or (claimed < n_evidence and
                                    re.search(rf"(?:全部|共|总计)\s*{claimed}\s*条", text)):
            errs.append(f"现象段声称「{claimed} 条证据」但该组实际有 {n_evidence} 条")
    # 后两条都是明确的【总数】断言，与实际不符一律拦
    for pat in (_COUNT_CLAIM_REV, _COUNT_ONLY):
        for m in set(pat.findall(text)):
            if int(m) != n_evidence:
                errs.append(f"现象段断言证据总数为 {m} 条，但该组实际有 {n_evidence} 条")
    return sorted(set(errs))


# 描述里的引号原声必须逐字来自证据。成对匹配，不用 [‘'"] 混配——
# 混配会跨引号抓取（‘太重了’、‘有点沉’ 被抓成一条），制造大量假阳性。
_QUOTE_PATS = (re.compile(r"‘([^‘’]{5,150}?)’"),
               re.compile(r"“([^“”]{5,150}?)”"),
               re.compile(r"「([^「」]{5,150}?)」"))
# 模型压缩长原声时会用省略号/顿号断开，这不是编造：拆开后每段都能溯源即放行
_ELLIPSIS = re.compile(r"…+|\.{3,}|、|，|\|")


def _evidence_corpus(items: list[dict], idx_map: list[int]) -> str:
    """该组证据的全部可引文本。三个字段都要并进来：
    电商的原声在 snippet，社媒的在 content，译文在 content_zh——
    漏掉 content_zh 会把「引用了译文」误判成幻觉。"""
    return _norm(" ".join(_evidence_parts(items, idx_map)))


def check_quotes(obj: dict, items: list[dict], idx_map: list[int]) -> list[str]:
    """现象段引号内的每一句都必须能在该组证据原文里找到。

    这是 check_citations 盖不住的缺口：后者只校验 citations 数组里的
    quote_span，而模型在【散文里】另起的引号原声完全不过闸。实测冷启动
    社媒有 13.2% 的引文无法溯源（电商 3.1%），大多是模型把外语原声译成
    中文后仍打引号，或直接转述成自己的话——对 PM 而言这就是伪造的用户原声。
    """
    text = obj.get("desc_phenomenon") or ""
    quotes = [q for p in _QUOTE_PATS for q in p.findall(text)]
    if not quotes:
        return []
    corpus = _evidence_corpus(items, idx_map)
    errs = []
    for q in quotes:
        if _norm(q) in corpus:
            continue
        frags = [f for f in _ELLIPSIS.split(q) if len(f.strip()) >= 5]
        if frags and all(_norm(f) in corpus for f in frags):
            continue                      # 省略号压缩，片段均可溯源
        errs.append(f"引号原声『{q[:48]}』不在该组证据原文中。"
                    f"引号内必须逐字照抄原文（外语原声保留原文，需要时在括号里补中文释义），"
                    f"不得转述或译写后仍打引号")
    return errs[:6]                       # 多于 6 条时截断，避免重试 prompt 过长


# problem_mode 被向量化为 mode_vec，是 L2 召回 / L3 判定 / 墓碑匹配 / 跨线汇聚
# 的唯一语义载体。冷启动实测：593 个机会点只有 63 个不同的 problem_mode，
# 其中 532 个（89.7%）是下面这些分类名——去重层等于在拿同一个向量互相比，
# L3 拿到「需求缺口类」vs「需求缺口类」必然判 same，错并大量无关条目。
_GENERIC_MODE = {
    "失效模式", "失效模式类", "需求缺口", "需求缺口类", "性能不足", "性能不足类",
    "竞品对标", "竞品对标类", "问题类型", "体验问题", "体验问题类", "未命名模式",
    "其他", "其他类", "产品问题", "用户诉求",
}


def check_problem_mode(obj: dict) -> list[str]:
    """problem_mode 必须是具体问题陈述，不能是分类名。"""
    pm = (obj.get("problem_mode") or "").strip()
    if not pm:
        return ["problem_mode 为空。该字段用于跨周去重，必须填具体问题陈述"]
    if pm in _GENERIC_MODE or pm.rstrip("类") in _GENERIC_MODE:
        return [f"problem_mode『{pm}』是分类名不是问题本身。改写成"
                f"「什么部件+在什么条件下+出什么问题」，如「支撑腿在正常承重下从根部断裂」"]
    if len(pm) < 8:
        return [f"problem_mode『{pm}』过短（{len(pm)} 字），无法承载去重语义，需 15–40 字"]
    return []


def check_polarity(obj: dict, items: list[dict], idx_map: list[int]) -> list[str]:
    """结构化证据全为非负面时，拦截产出擅自断言存在缺口。"""
    if not idx_map:
        return []
    sentiments = [str(items[i].get("sentiment") or "").strip() for i in idx_map]
    if any(not sentiment for sentiment in sentiments):
        return []                         # 结构化极性不完整时不猜
    nonnegative = sum(
        sentiment in config.POLARITY_NONNEGATIVE for sentiment in sentiments)
    if nonnegative / len(sentiments) < config.POLARITY_NONNEGATIVE_RATIO:
        return []
    low_conf = any(
        items[i].get("low_conf") is True or items[i].get("_low_conf") is True
        for i in idx_map
    )
    if low_conf:
        return []
    text = " ".join(str(obj.get(field) or "") for field in ("title", "problem_mode"))
    hits = sorted({word for word in config.POLARITY_GAP_WORDS if word in text})
    if not hits:
        return []
    return [
        f"title/problem_mode 命中缺口断言词（{'、'.join(hits)}），但组内结构化极性"
        f"全部为正面/中性且非 low_conf；请改为中性转述，或降级为「用户已认可，无缺口」"
    ]


def check_no_leak(obj: dict) -> list[str]:
    """JSON 字段名不得出现在给人看的描述里。"""
    errs = []
    for seg in ("desc_phenomenon", "desc_attribution"):
        t = obj.get(seg) or ""
        if _LEAK_PAT.search(t):
            errs.append(f"{seg} 泄漏了 JSON 字段名（evidence_idx/quote_span），"
                        f"这些只能出现在 citations 里")
        if _ZERO_MORE.search(t):
            errs.append(f"{seg} 出现「另有 0 条」——全部引用时不要写这句")
    return errs


_STD_PAT = re.compile(r"\b(ISO|GB/?T?|EN|ASTM|IEC|JIS|ANSI)\s?\d{3,}[-:\d.]*", re.I)

_PRODUCT_CUES = (
    "电池|三脚架|闪光灯|摄影灯|灯|相机|支架|背板|后背|云台|手柄|保护壳|"
    "快拆板|底座|麦克风|镜头|充电器"
)
_BRAND_BEFORE_PRODUCT = re.compile(
    rf"([A-Za-z][A-Za-z0-9_-]{{2,}}|[\u4e00-\u9fff]{{2,10}}?)(?:的)?(?={_PRODUCT_CUES})",
    re.I,
)
_CJK_BEFORE_LATIN_PRODUCT = re.compile(
    rf"([\u4e00-\u9fff]{{2,10}}?)(?=[A-Za-z][A-Za-z0-9_-]{{2,}}(?:的)?(?:{_PRODUCT_CUES}))",
    re.I,
)
_BRAND_PREFIX = re.compile(
    r"^.*(?:就是发现|发现|还好我买了|我买了|买了|这个|那个|这款|那款|换成|使用)")


def _evidence_brand_candidates(text: str) -> set[str]:
    """从产品词前的命名片段抽品牌候选，不维护会过时的竞品表。"""
    candidates: set[str] = set()
    raw_candidates = (
        _BRAND_BEFORE_PRODUCT.findall(text or "")
        + _CJK_BEFORE_LATIN_PRODUCT.findall(text or "")
    )
    for raw in raw_candidates:
        value = _BRAND_PREFIX.sub("", raw).strip("的这那款个")
        # 中英混排名称（如「小隼TagBatt电池」）由正则稳定取到尾部 Latin token。
        latin = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", value)
        if latin:
            value = latin[-1]
        if any(character.isdigit() for character in value):
            continue                    # 型号不是品牌；归属由下方上下文分支判断
        if len(value) >= 2 and not any(
                value in cue for cue in _PRODUCT_CUES.split("|")):
            candidates.add(value)
    return candidates


def _own_brand_norms() -> set[str]:
    aliases = getattr(config, "BRAND_OWN_ALIASES", (config.BRAND_OWN,))
    return {_norm(str(alias)) for alias in aliases if alias}


def check_title_subject(obj: dict, items: list[dict], idx_map: list[int]) -> list[str]:
    """标题若点名具体品名或第三方主体，必须与组内证据归属一致。"""
    title = (obj.get("title") or "").split("：")[0].split(":")[0].strip()
    if not title:
        return []
    names = [items[i].get("product_name") for i in idx_map if items[i].get("product_name")]
    if not names:
        source_text = " ".join(_evidence_parts(items, idx_map))
        own = _own_brand_norms()
        candidates = {
            candidate for candidate in _evidence_brand_candidates(source_text)
            if _norm(candidate) not in own
        }
        title_norm = _norm(title)
        mentioned = sorted(
            candidate for candidate in candidates if _norm(candidate) in title_norm)
        if mentioned:
            return [
                f"title 主体『{title}』点名证据正文抽取出的第三方品牌/产品 token"
                f"『{'、'.join(mentioned)}』，不属于本品品牌及别名；请勿将第三方问题"
                f"张冠李戴到本品机会点"
            ]

        # 标题中的型号若只在第三方命名片段附近出现，也按第三方主体处理。
        normalized_source = _norm(source_text)
        for model in sorted(set(_MODEL_PAT.findall(title)), key=_norm):
            model_norm = _norm(model)
            pos = normalized_source.find(model_norm)
            if pos < 0:
                continue
            context = normalized_source[max(0, pos - 24):pos + len(model_norm) + 24]
            nearby = sorted(
                candidate for candidate in candidates if _norm(candidate) in context)
            if nearby and not any(alias in context for alias in own):
                return [
                    f"title 型号『{model}』在证据中仅与第三方 token"
                    f"『{'、'.join(nearby)}』同现，主体归属疑似张冠李戴"
                ]
        return []
    hit = sum(1 for n in names if _norm(n) in _norm(title) or _norm(title) in _norm(n))
    # 标题主体像是某个具体品名（能在证据品名里找到对应）却占比不足 => 张冠李戴
    if hit and hit / len(names) < 0.6:
        top = max(set(names), key=names.count)
        return [f"标题主体『{title}』只覆盖 {hit}/{len(names)} 条证据的品名"
                f"（组内主要品名为『{top}』），跨品名组应改用「品类+部件」作主体"]
    return []


_PAREN_DETAIL = re.compile(r"[（(]([^（）()]{2,100})[）)]")
# 只保留【具体部件】后缀。2026-08-17 真库 3934 条标定：抽象后缀罚的是句式而非
# 编造——「能力」217 次、「接口」201 次、「功能」70 次命中，而「新增 X 能力」正是
# prompts.ACTIONS 规定的标准标题句式，「品类」则来自强制的主体前缀。它们与
# 「时因接口」「在无功能」「理与协议」这类从词中间切出的碎片一起，构成了
# 短证据规则 80% 命中率（1134 条符合条件里命中 907）的主要来源。
# 具体部件后缀（系统/模块/组件/底座/云台/脚垫/协议/阵列）罚的是真实编造，保留。
# 移除抽象后缀后 C 组无一丢失：C6 仍由「扣分系统」「扣分底座」命中，
# C3/C4/C7/C8/C10/C11 由括号扩写通道命中。
_ENTITY_SUFFIX_PREFIX = {
    "系统": 2, "模块": 2, "组件": 2, "底座": 2, "云台": 2,
    "脚垫": 2, "协议": 2, "阵列": 2,
}
_ENTITY_STANDALONE = ("保护壳", "热插拔", "涂层", "刮花", "掉漆", "频闪", "实时取景")
_SOFT_ENTITY_SUFFIXES = ("功能", "能力", "版本")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HAN = re.compile(r"[\u3400-\u9fff]")


def _entity_tokens(text: str) -> set[str]:
    """抽取短证据下可稳定识别的具体技术实体，不做开放式分词。"""
    tokens = {term for term in _ENTITY_STANDALONE if term in text}
    for suffix, prefix_len in _ENTITY_SUFFIX_PREFIX.items():
        start = 0
        while (pos := text.find(suffix, start)) >= 0:
            prefix = re.search(
                rf"[\u4e00-\u9fff]{{0,{prefix_len}}}$", text[:pos])
            token = (prefix.group(0) if prefix else "") + suffix
            if len(token) >= len(suffix):
                tokens.add(token)
            start = pos + len(suffix)
    return tokens


def _entity_supported(token: str, corpus: str) -> bool:
    normalized = _norm(token)
    normalized = re.sub(r"^(?:含|支持|适配|例如|比如|如)", "", normalized).rstrip("等")
    if normalized and (normalized in corpus or normalized in corpus.replace("の", "")):
        return True
    if "螺纹接口" in normalized and any(alias in corpus for alias in ("螺口", "螺纹", "ネジ")):
        return True
    for suffix in _SOFT_ENTITY_SUFFIXES:
        suffix_norm = _norm(suffix)
        if normalized.endswith(suffix_norm):
            stem = normalized[:-len(suffix_norm)]
            if len(stem) >= 2 and stem in corpus:
                return True
    return False


def _title_body(title: str) -> str:
    """去掉标题的主体前缀，只留冒号之后的动作与对象。

    标题格式「主体：动作」是 ``_TITLE_RE`` 与提示词强制要求的，主体几乎总是
    「X品类 / X配件」这类归类词，本就不会逐字出现在证据里。2026-08-17 真库
    3934 条标定：短证据命中 1015 条中有 658 条（64.8%）罚的正是这个前缀
    （如「摄影手柄品类：」里的「手柄品类」）——那是在罚格式本身。
    主体是否被证据支撑属于 ``check_title_subject`` 的职责，不在本函数。
    无冒号时整串都是动作描述，原样返回。
    """
    head, sep, body = title.partition("：")
    if not sep:
        head, sep, body = title.partition(":")
    return body if sep else title


def check_short_evidence(obj: dict, items: list[dict], idx_map: list[int]) -> list[str]:
    """低信息证据只允许转述，不得补写不存在的具体实体。

    数值和型号仍由既有 ``check_orphan_claims`` 单点负责；本函数只补它覆盖不到的
    模块、组件、协议、场景部件等名词，避免形成第二套参数闸门。
    标题只看冒号之后的部分，理由见 ``_title_body``。
    """
    parts = _evidence_parts(items, idx_map)
    corpus = _norm(" ".join(parts))
    if len(corpus) >= config.SHORT_EVIDENCE_CHARS:
        return []

    raw_corpus = " ".join(parts)
    has_content_zh = any(items[i].get("content_zh") for i in idx_map)
    same_language_lexical = has_content_zh or (
        bool(_HAN.search(raw_corpus)) and not _KANA.search(raw_corpus))
    errs: list[str] = []
    for field in ("title", "problem_mode"):
        text = str(obj.get(field) or "")
        if field == "title":
            text = _title_body(text)
        candidates: set[str] = set()
        if same_language_lexical:
            candidates |= _entity_tokens(text)
        # 外语到中文不能做裸字面比较；但括号里的新增规格/模块是明确的扩写边界。
        for detail in _PAREN_DETAIL.findall(text):
            if not _num_tokens(detail):       # 参数已由 check_orphan_claims 负责
                candidates.add(detail)
        unsupported = sorted(
            token for token in candidates if not _entity_supported(token, corpus))
        for token in unsupported:
            errs.append(
                f"{field} 在仅 {len(corpus)} 字的短证据上引入未出现的具体实体『{token}』；"
                f"短证据只能转述，不得补写模块、部件、协议、参数或场景细节"
            )
    return errs[:6]


def check_suggestion(text: str) -> list[str]:
    errs = []
    t = (text or "").strip()
    if not (40 <= len(t) <= 320):
        errs.append(f"建议段长度 {len(t)} 不在 40–320")
    for w in prompts.HEDGE_WORDS:
        if w in t:
            errs.append(f"建议段出现已验证类断言: {w}")
    for m in set(_STD_PAT.findall(t)):
        errs.append(f"建议段引用了具体标准号（凭记忆写极易张冠李戴，禁止）: {m}")
    return errs


def validate_stage2(obj: dict, items: list[dict], idx_map: list[int], ctx=None,
                    grounding_out: list[str] | None = None) -> list[str]:
    """汇总 Stage2 校验；新增 grounding 规则受报告/强制开关控制。

    顺序先跑既有结构与逐字溯源，再跑新增语义边界：前者错误更基础、重试提示
    应排在前面；后者在报告模式只计数，强制模式才并入同一错误列表。
    ``ctx`` 与 ``grounding_out`` 均为可选，保留原三参数调用的兼容性。
    """
    orphan_errors = check_orphan_claims(obj, items, idx_map)
    legacy_orphan = [
        error for error in orphan_errors if error.startswith("desc_phenomenon ")]
    new_orphan = [error for error in orphan_errors if error not in legacy_orphan]

    subject_errors = check_title_subject(obj, items, idx_map)
    has_product_names = any(items[i].get("product_name") for i in idx_map)
    legacy_subject = subject_errors if has_product_names else []
    new_subject = [] if has_product_names else subject_errors

    hard_errors = (
        check_format(obj)
        + check_count_claim(obj, len(idx_map))
        + check_problem_mode(obj)
        + check_citations(obj, items, idx_map)
        + check_quotes(obj, items, idx_map)
        + legacy_orphan
        + legacy_subject
        + check_country_claims(obj, items, idx_map)
        + check_no_leak(obj)
    )
    polarity_errors = check_polarity(obj, items, idx_map)
    short_errors = check_short_evidence(obj, items, idx_map)
    grounding_errors = new_orphan + polarity_errors + new_subject + short_errors

    if grounding_out is not None:
        grounding_out.extend(grounding_errors)
    if ctx is not None:
        ctx.metric_incr(
            ("generation",),
            grounding_checked_attempts=1,
            grounding_enforced_attempts=int(config.GROUNDING_ENFORCE),
            grounding_flagged_attempts=int(bool(grounding_errors)),
            grounding_issue_count=len(grounding_errors),
            grounding_orphan_issues=len(new_orphan),
            grounding_polarity_issues=len(polarity_errors),
            grounding_title_subject_issues=len(new_subject),
            grounding_short_evidence_issues=len(short_errors),
        )
    return hard_errors + (grounding_errors if config.GROUNDING_ENFORCE else [])

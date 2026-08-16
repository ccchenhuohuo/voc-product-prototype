"""Stage 4 程序化校验（PRD v8 §5.9）。

反幻觉的硬闸门。早期设计让模型自报 cited_numbers 字符串再回查证据全文，
但语料是中英日德西混合——"2 个月"在 nach 2 Monaten / 2ヶ月で 里没有字面对应，
整串比对几乎全部误判、只比裸数字又形同虚设。
改为结构化溯源：模型给出 evidence_idx + quote_span，程序只校验
"该序号存在" 与 "quote_span 是那条证据原文的连续子串"，彻底避开跨语种比对。
"""
from __future__ import annotations
import re
import unicodedata

from .. import prompts

# 标题：主体 + 冒号 + 含受控动作词的对象（动作词可在冒号后任意位置）
_TITLE_RE = re.compile(
    r"^.{2,40}[：:]\s*.{0,28}(" + "|".join(prompts.ACTIONS) + r").{0,28}$")

# 描述中疑似需要溯源的片段
_MODEL_PAT = re.compile(
    r"(iPhone\s*\d+[A-Za-z ]*|Galaxy\s*S?\d+[A-Za-z ]*|Mate\s*\d+[A-Za-z ]*|"
    r"Pixel\s*\d+[A-Za-z ]*|DJI\s*[A-Za-z0-9 ]+|Osmo\s*[A-Za-z0-9 ]+|Action\s*\d+\w*|"
    r"Pocket\s*\d+\w*|Insta360\s*[A-Za-z0-9 ]+|A\d{4}|ZV-?E?\d+\w*|"
    r"[A-Z]{2,}-?\d{2,}[A-Z]*)", re.I)
_NUM_PAT = re.compile(r"\d+(?:\.\d+)?\s*(?:个月|个星期|周|天|小时|分钟|秒|年|kg|g|mm|cm|%|星)")


def _norm(s: str) -> str:
    """全角转半角 + 去空白，用于宽松子串比对。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).lower()


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
    """描述中出现的机型/数字必须可溯源。

    合法来源有两个：citations 里显式给出的，以及证据行自带的 product_name
    字段 —— 后者是结构化事实而非模型编造，早期只查 citations 造成大量误报。
    """
    cited = {_norm(str(c.get("value", ""))) for c in (obj.get("citations") or [])}
    if items and idx_map is not None:
        cited |= {_norm(items[i].get("product_name") or "") for i in idx_map}
        cited |= {_norm(items[i].get("snippet") or "") for i in idx_map}
    cited.discard("")
    errs = []
    text = (obj.get("desc_phenomenon") or "")
    for m in set(_MODEL_PAT.findall(text)):
        if not any(_norm(m) in c or c in _norm(m) for c in cited if c):
            errs.append(f"现象段出现未溯源机型: {m}")
    for m in set(_NUM_PAT.findall(text)):
        digits = re.sub(r"\D", "", m)
        if digits and not any(digits in c for c in cited if c):
            errs.append(f"现象段出现未溯源数值: {m}")
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
    parts = []
    for i in idx_map:
        it = items[i]
        parts += [it.get("snippet") or "", it.get("content") or "",
                  it.get("content_zh") or ""]
    return _norm(" ".join(parts))


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


def check_title_subject(obj: dict, items: list[dict], idx_map: list[int]) -> list[str]:
    """标题若点名具体品名，该品名必须在组内证据中占多数（防张冠李戴）。"""
    title = (obj.get("title") or "").split("：")[0].split(":")[0].strip()
    if not title:
        return []
    names = [items[i].get("product_name") for i in idx_map if items[i].get("product_name")]
    if not names:
        return []
    hit = sum(1 for n in names if _norm(n) in _norm(title) or _norm(title) in _norm(n))
    # 标题主体像是某个具体品名（能在证据品名里找到对应）却占比不足 => 张冠李戴
    if hit and hit / len(names) < 0.6:
        top = max(set(names), key=names.count)
        return [f"标题主体『{title}』只覆盖 {hit}/{len(names)} 条证据的品名"
                f"（组内主要品名为『{top}』），跨品名组应改用「品类+部件」作主体"]
    return []


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


def validate_stage2(obj: dict, items: list[dict], idx_map: list[int]) -> list[str]:
    return (check_format(obj) + check_count_claim(obj, len(idx_map))
            + check_problem_mode(obj)
            + check_citations(obj, items, idx_map)
            + check_quotes(obj, items, idx_map)
            + check_orphan_claims(obj, items, idx_map)
            + check_title_subject(obj, items, idx_map)
            + check_country_claims(obj, items, idx_map)
            + check_no_leak(obj))

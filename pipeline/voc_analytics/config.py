"""集中配置。所有密钥从环境变量注入，不落盘到仓库（PRD v8 §10.2）。"""
from __future__ import annotations
import os
import re
import threading
from dataclasses import dataclass, field

# ---- 云听 ----
YUNTING_URL = os.environ.get("YUNTING_MCP_URL", "https://ytcem.cn/mcp")
YUNTING_KEY = os.environ.get("YUNTING_MCP_KEY", "")
PROJECT_ID = os.environ.get("VOC_PROJECT_ID", "bd7496c9a5a6410eab699bcc2276566a")
EXPORT_CAP = 5000  # 云听单次导出硬上限

# ---- 百炼 ----
BAILIAN_KEY = os.environ.get("BAILIAN_API_KEY", "")
BAILIAN_BASE = os.environ.get(
    "BAILIAN_EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
EMBED_MODEL = os.environ.get("VOC_EMBED_MODEL", "text-embedding-v4")
EMBED_DIM = 1024
EMBED_BATCH = 10          # M0 实测上限：25 报 batch size is invalid
CHAT_MODEL = os.environ.get("VOC_CHAT_MODEL", "qwen-plus")
LLM_TIMEOUT = 60
LLM_RETRY = 4
# 并发实测（账户恢复后逐档探测，每档 3×并发数 次调用）：
#     4 -> 2.09/s      24 ->10.10/s      96 ->12.57~34.78/s  干净但吞吐波动大
#     8 -> 3.28/s      32 ->16.25/s     128 -> 429 Throttling.Concurrency（32/384 失败）
#    16 -> 8.79/s      48 ->21.99/s
#                      64 ->27.42/s  ← 采用
# 取 64 而非 96/128：128 已触发限流，96 虽未报错但吞吐在 12~35 之间剧烈波动，
# 说明已在边界上。超过阈值会触发 429 + 指数退避重试，实际反而更慢。
# 64 实测稳定 27.4 次/秒，是当时的吞吐/稳定性拐点。
#
# 2026-08-17 复测（embedding，10 条/批，每档 3×并发数 次调用）：
#     64 -> 2.42 次/秒  0 失败
#     96 -> 3.67 次/秒  0 失败   ← 改用
#    128 -> 全部失败：Connection reset by peer
#    160 -> 全部失败：Remote end closed connection without response
# 拐点已上移到 96，比旧值高约 50%。128 起连接被对端直接掐断，重试也救不回来。
# 注：本次口径是「调用/秒」而每次调用带 10 条文本，与上面那组旧数字不可直接比较。
LLM_CONCURRENCY = int(os.environ.get("VOC_LLM_CONCURRENCY", "96"))

# ---- 计费（元 / 百万 token，百炼华北2·北京，2026-08-17 抄录）----
# 只作估算：真实账单以百炼控制台为准，这里的用途是让「跑一轮要花多少钱」
# 在开跑之前就能看见，而不是跑完看账单才知道。
#
# 数据来源与已知不确定性：
#   · text-embedding-v4 —— help.aliyun.com 模型页给的是人民币原价，直接抄。
#   · qwen-plus —— 中文定价页正文过长被截断，改抄 alibabacloud.com 同页的
#     美元价（华北2·北京：输入 $0.115/M，非思考输出 $0.287/M）。输入按当时
#     汇率折回 0.8 元/M，与多个第三方汇总口径吻合，故输出按同一比例取
#     2.0 元/M。注意有第三方站点写 4.8 元/M——那与官方美元价对不上，未采用。
#   · qwen-plus 是阶梯计价，单价取决于【单次请求的输入 token 总量】：
#     ≤128K 走第一档。本管道单次输入远低于 128K（最大 max_tokens 也才 8000），
#     所以只需第一档。若将来出现长上下文调用，这张表要补档。
#   · 思考模式输出单价另计（约 8 元/M），本管道未启用。
#
# 改价时连同上面的日期与来源一起更新，不要只改数字。
PRICE_PER_MTOK = {
    "qwen-plus":          {"input": 0.8,  "output": 2.0},
    "text-embedding-v4":  {"input": 0.5,  "output": 0.0},
}
# 未登记的模型按这个兜底，并在报表里标出来，避免悄悄按 0 计费。
PRICE_FALLBACK = {"input": 0.8, "output": 2.0}

# ---- PostgreSQL ----
PG = dict(
    host=os.environ.get("VOC_PG_HOST", "127.0.0.1"),
    port=int(os.environ.get("VOC_PG_PORT", "5434")),
    dbname=os.environ.get("VOC_PG_DB", "voc"),
    user=os.environ.get("VOC_PG_USER", "voc_writer"),
    password=os.environ.get("VOC_PG_PASSWORD", ""),
)

# ---- 业务口径（PRD v8 §1.2 / §4）----
BRAND_OWN = "VIJIM"                    # 系统内本品代号
# 库内本品白名单：分别是 Ulanzi / Joby / Falcam 的库内写法。
# 2026-08-18 已对全库 brands 十个取值核实；G1 只认这三个值。
OWN_BRANDS = ("VIJIM", "宙比", "小隼")
# 证据正文里会同时出现公司品牌、英文名和中文名；主体归属校验统一视为本品。
# 三个自有品牌各自的中英文写法都要在内：库内 brands 用中文（宙比/小隼），
# 但作者名与正文里大量出现英文（JOBY Official / FALCAM小隼）。2026-08-18 实测
# 缺英文名导致 403 条官号内容绕过 G3 漏进 G4。
BRAND_OWN_ALIASES = (BRAND_OWN, "ULANZI", "优篮子", "宙比", "JOBY", "小隼", "FALCAM")
# 社媒导出必须带齐的列；缺任一列都会让帖子线索字段整片为 NULL。
SOCIAL_REQUIRED_COLUMNS = ("消息ID", "消息组ID", "消息类型", "父ID",
                           "用户名称", "消息标题")
SOCIAL_KEEP_TAGS = ("用户咨询", "用户使用体验")
SOCIAL_DROP_TAGS = ("产品种草广告", "产品评测", "竞品拉踩", "二手转让")
# G3 按作者名判官号；必须从上方同一份本品别名派生，
# 避免白名单与官号口径各自漂移。PostgreSQL 使用 !~* 做大小写无关匹配。
OFFICIAL_AUTHOR_PATTERN = "(?:" + "|".join(
    re.escape(alias) for alias in BRAND_OWN_ALIASES
) + ")"
COMMENT_FILTER = {"本竞品": ["本品"]}    # 电商
SOCIAL_FILTER = {"品牌": list(OWN_BRANDS)}  # 社媒（本竞品字段恒空）

# 当前云听连接器的已注册抽取计划。生成与分类不读这个名单；
# 新来源可以使用其他连接器写入同一事实层，只需在
# voc_source_policy 注册 requires_spu。若也由云听抽取，则在此追加计划。
INGEST_SOURCE_PLANS = (
    {"src_line": "电商", "query_type": "COMMENT",
     "tag_filter": COMMENT_FILTER, "slice_days": 30},
    {"src_line": "社媒", "query_type": "SOCIAL",
     "tag_filter": SOCIAL_FILTER, "slice_days": 15},
)

# Stage 1 分批与投票（§5.5）
BATCH_SIZE = 50
# 单个桶内允许跳过的 Stage1 批次占比。个别批次因 LLM 输出瑕疵失败是常态，
# 记进批次账本后跳过即可；超过此比例说明是系统性问题（提示词失效、模型
# 行为变化），继续跑只会产出残缺结果，应中止让人来看。
STAGE1_MAX_FAILED_RATIO = float(os.environ.get("VOC_STAGE1_MAX_FAILED_RATIO", "0.2"))
# 同理用于「一个机会点」这一层：Stage2/3/4 的质量校验没过只作废该条，
# 记账后继续；超过此比例说明是系统性问题，中止让人来看。
GENERATION_MAX_FAILED_RATIO = float(
    os.environ.get("VOC_GENERATION_MAX_FAILED_RATIO", "0.2"))
# 比例阈值的绝对下限：失败数不超过它就一律放行，不看比例。
# 小桶（2~5 条）坏 1 条在比例上是 20%~50%，但那只是 1 条，不是系统性失败。
GENERATION_MIN_FAILED_ABS = int(
    os.environ.get("VOC_GENERATION_MIN_FAILED_ABS", "2"))
# 新增 grounding 闸门先以报告模式标定；显式开关后才参与重试/作废。
GROUNDING_ENFORCE = os.environ.get(
    "VOC_GROUNDING_ENFORCE", "0").strip().casefold() in {
        "1", "true", "yes", "on",
    }
# C10/C11 的原文只有 13/10 字；C7/C8 的外语原文虽有 51/67 个字符，
# 实际也只提供一个笼统故障。取 80 可覆盖这批已确认的低信息单证据，同时只
# 限制标题/problem_mode 的新增实体，不限制忠实转述，也不改变 MIN_EVIDENCE。
SHORT_EVIDENCE_CHARS = int(os.environ.get("VOC_SHORT_EVIDENCE_CHARS", "80"))
# 只有全组结构化极性均达到此比例、且没有 low_conf，才启用极性反转提示。
POLARITY_NONNEGATIVE_RATIO = 1.0
# 【只认「正面」，不含「中性」】2026-08-17 真库 3934 条标定：把「中性」计入
# 非负面时，polarity 命中 1062 条（27.0%），其中 769 条（72%）的证据极性全为
# 中性，且 610 条来自需求缺口渠道——用户问「什么时候出 X」在情绪上就是中性，
# 但那正是真实的诉求缺口。中性 ≠ 用户已满意，把两者等同是口径错误。
# 收紧到只认「正面」后预计降到约 293 条（7.4%）。
# 若将来要重新纳入中性，必须先给出「中性证据里确实不存在缺口」的独立证据。
POLARITY_NONNEGATIVE = frozenset({"正面"})
POLARITY_GAP_WORDS = (
    "缺乏", "缺少", "不足", "过长", "过大", "过重", "无法", "不能",
    "不支持", "失效", "断裂", "不居中", "偏移", "脱落", "损坏",
)
# 自洽性投票：M2 A/B 实测后【关闭】。两个 240+ 证据桶的对照结果——
#   整体质量: 投票 47组/30%覆盖/15调用  vs  单次 33组/28%覆盖/5调用
#   耐用性  : 投票 69组/51%覆盖/15调用  vs  单次 25组/49%覆盖/5调用
# 覆盖率仅差 2pp（噪声范围内），但组数膨胀 2.0–2.8 倍导致机会点碎片化，
# 成本 3 倍。PRD §5.5 预设判据"未显著提升即砍掉"，据此关闭。
# 保留实现与开关，M5 标定后可重新评估。
VOTE_ENABLED = False
MAX_GROUP_SIZE = 40        # 防最大团粘连
# 仅作为 Stage1 提示词变量下发，**落库前没有任何程序校验**。
# 2026-08-18 实测：老品迭代 159/875（18%）的机会点 evi_total=1，说明它从未
# 真正生效。业务已确认不做程序筛选（单证据信号要保留、并在产品页可见），
# 故此处保持现状；改动它不会改变落库结果，别误以为这是一道闸。
MIN_EVIDENCE = {"老品迭代": 2, "新品创新": 1}    # §5.2（提示词用，非闸门）

# G4 统一价值门：首票低置信或判为产品缺陷时追加两票。
GATE_VOTE_CONF = float(os.environ.get("VOC_GATE_VOTE_CONF", "0.8"))
GATE_VOTE_ENABLED = os.environ.get(
    "VOC_GATE_VOTE_ENABLED", "1"
).strip().casefold() not in {"0", "false", "no", "off"}

# 新品创新诉求预聚类。阈值由三组实验共同标定：
#   · mode_vec 全量扫描：0.10 最大簇 11 且语义纯净，0.20 炸成 986；
#   · 196 条小样：Luna 同义对距离 0.018–0.075；
#   · 5,015 条全量探针：0.06–0.15 区间结果稳定。
# 2026-08-17 用探针那 777 条「需求缺口」真实 claim 的真实向量复算，逐簇复现：
#   696 簇 / 37 个多成员簇 / 最大簇 24 / 659 单例。阈值扫描（k=20）显示
#   0.04–0.15 是一整片平台（最大簇恒为 24，簇数 720→653 平滑下降），
#   悬崖在 0.20（最大簇 76）与 0.25（最大簇 198）。0.10 落在平台中部。
# 注：同一 claim 文本重复调用 embedding 不是逐位确定的（实测分量偏差
#   ~6e-4、余弦距离偏差 ~1e-6），比阈值低五个数量级，不影响分簇。
# 改 PRECLUSTER_COS 时必须同时更新上述依据，不得只改数字。
PRECLUSTER_ENABLED = os.environ.get(
    "VOC_PRECLUSTER_ENABLED", "1").strip().casefold() not in {
        "0", "false", "no", "off",
    }
PRECLUSTER_COS = 0.10
PRECLUSTER_K = 20
PRECLUSTER_MAX = MAX_GROUP_SIZE

# 去重（§6.3）
L2_TOPK_MIN, L2_TOPK_MAX, L2_TOPK_RATIO = 3, 10, 0.3
MODE_MERGE_COS = 0.75      # 三档探针选定；仅召回，判定仍交 L3

# 冷启动放行（§12.2）
BACKLOG_TOP_N = 50
WEEKLY_PUSH_CAP = 15

# 保留期（§10.3）
RETENTION_MONTHS = 24


@dataclass
class RunCtx:
    """一次运行的上下文，贯穿各 stage 并最终写入 voc_run_log。"""
    run_id: str
    week: str
    mode: str = "incremental"        # incremental | backfill
    llm_calls: int = 0
    llm_tokens: int = 0
    llm_failed_modes: int = 0
    metrics: dict = field(default_factory=dict)
    _metrics_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False)

    def bump(self, calls: int = 0, tokens: int = 0, failed: int = 0) -> None:
        with self._metrics_lock:
            self.llm_calls += calls
            self.llm_tokens += tokens
            self.llm_failed_modes += failed

    def set_llm_usage(self, calls: int, tokens: int) -> None:
        """以客户端权威计数覆盖旧调用点的零散累计。"""
        with self._metrics_lock:
            self.llm_calls = calls
            self.llm_tokens = tokens

    def metric_update(self, path: tuple[str, ...], **values: object) -> None:
        """线程安全地更新运行账本中的一个嵌套节点。"""
        with self._metrics_lock:
            node = self.metrics
            for part in path:
                node = node.setdefault(part, {})
            node.update(values)

    def metric_incr(self, path: tuple[str, ...], **deltas: int) -> None:
        """线程安全地累加运行账本计数。"""
        with self._metrics_lock:
            node = self.metrics
            for part in path:
                node = node.setdefault(part, {})
            for key, delta in deltas.items():
                node[key] = int(node.get(key, 0)) + delta


def require(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"缺少必需的环境变量：{name}")
    return value

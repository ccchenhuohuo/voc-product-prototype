"""集中配置。所有密钥从环境变量注入，不落盘到仓库（PRD v8 §10.2）。"""
from __future__ import annotations
import os
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
# 64 实测稳定 27.4 次/秒，是吞吐/稳定性的拐点。周度 ~1300 次调用约 50 秒。
LLM_CONCURRENCY = int(os.environ.get("VOC_LLM_CONCURRENCY", "64"))

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
COMMENT_FILTER = {"本竞品": ["本品"]}    # 线A
SOCIAL_FILTER = {"品牌": [BRAND_OWN]}   # 线B（本竞品字段在社媒恒为空，见 §1.2）

# 社媒去水标签分流（§4.4）
DEWATER_GAP = {"用户咨询", "其他"}          # 需求缺口通道（"其他"首月抽检后再定）
DEWATER_COMP = {"产品评测", "竞品拉踩"}     # 竞品对标通道

# Stage 1 分批与投票（§5.5）
BATCH_SIZE = 50
# 自洽性投票：M2 A/B 实测后【关闭】。两个 240+ 证据桶的对照结果——
#   整体质量: 投票 47组/30%覆盖/15调用  vs  单次 33组/28%覆盖/5调用
#   耐用性  : 投票 69组/51%覆盖/15调用  vs  单次 25组/49%覆盖/5调用
# 覆盖率仅差 2pp（噪声范围内），但组数膨胀 2.0–2.8 倍导致机会点碎片化，
# 成本 3 倍。PRD §5.5 预设判据"未显著提升即砍掉"，据此关闭。
# 保留实现与开关，M5 标定后可重新评估。
VOTE_ENABLED = False
MAX_GROUP_SIZE = 40        # 防最大团粘连
MIN_EVIDENCE = {"线A": 2, "线B": 1}    # §5.2

# 诉求门（§5.6）
INTENT_PASS = 0.6
INTENT_REVIEW = 0.4

# 去重（§6.3）
L2_TOPK_MIN, L2_TOPK_MAX, L2_TOPK_RATIO = 3, 10, 0.3
MODE_MERGE_COS = 0.85      # 跨批模式归并候选阈值（仅召回，判定仍交 L3）

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

    def bump(self, calls: int = 0, tokens: int = 0, failed: int = 0) -> None:
        self.llm_calls += calls
        self.llm_tokens += tokens
        self.llm_failed_modes += failed


def require(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"缺少必需的环境变量：{name}")
    return value

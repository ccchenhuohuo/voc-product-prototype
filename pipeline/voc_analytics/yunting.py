"""云听 MCP 客户端 + xlsx 抽取。

三个已知的生产坑，全部在此处理（PRD v8 §4.1）：
  1. 单次导出硬上限 5000 条 —— 切片 + truncated 检查 + 自动二分重切
  2. matched_count=0 且 status=succeeded —— 与"本周无数据"无法区分，按模式区分处置
  3. 导出 xlsx 含非法 XML 控制字符 —— openpyxl 直接 ParseError，需先剥离
"""
from __future__ import annotations
import io, json, re, time, urllib.request, urllib.error, zipfile
from datetime import datetime, timedelta
from typing import Any, Iterator

from . import config as C

_BAD_XML = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MCP_HEADERS = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}


class YuntingError(RuntimeError):
    pass


class ZeroMatch(YuntingError):
    """分片零命中。增量模式下应阻断，回填模式下仅告警。"""


def _rpc(method: str, params: dict | None = None, timeout: int = 90,
         notify: bool = False) -> dict:
    # JSON-RPC 通知不带 id，也没有响应体；带了 id 会被当成请求并校验 params
    body: dict = {"jsonrpc": "2.0", "method": method}
    if not notify:
        body["id"] = 1
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        C.YUNTING_URL, data=json.dumps(body, ensure_ascii=False).encode(),
        headers={**_MCP_HEADERS, "X-MCP-Api-Key": C.require("YUNTING_MCP_KEY", C.YUNTING_KEY)})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if notify or not raw.strip():
        return {}
    payload = json.loads(raw)
    if "error" in payload:
        raise YuntingError(f"{method}: {payload['error']}")
    return payload["result"]


def call_tool(name: str, args: dict) -> dict:
    res = _rpc("tools/call", {"name": name, "arguments": args})
    return json.loads(res["content"][0]["text"])


def _handshake() -> None:
    _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "voc-analytics", "version": "1"}})
    _rpc("notifications/initialized", {}, notify=True)


# ---------------------------------------------------------------- 元数据
def list_topic_values(topic: str) -> list[str]:
    return call_tool("list_project_topic_value_names",
                     {"project_id": C.PROJECT_ID, "topic_name": topic})["result"]


def tag_tree_details(names: list[str]) -> list[dict]:
    return call_tool("list_project_tag_tree_details_by_name",
                     {"project_id": C.PROJECT_ID, "tree_names": names})["result"]


def tag_tree_names() -> list[dict]:
    return call_tool("list_project_tag_tree_names", {"project_id": C.PROJECT_ID})["result"]


# ---------------------------------------------------------------- 导出
def _create_task(qtype: str, start: str, end: str, total: int, tf: dict | None) -> str:
    args = {"project_id": C.PROJECT_ID, "query_task_type": qtype,
            "start_time": start, "end_time": end, "total": total}
    if tf:
        args["topic_filter"] = tf
    r = call_tool("query_project_raw_messages", args)
    if not r.get("ok"):
        raise YuntingError(f"创建任务失败: {r}")
    return r["data"]["task_id"]


def _await_task(task_id: str, tries: int = 12) -> dict:
    for _ in range(tries):
        r = call_tool("get_query_task_result", {"task_id": task_id, "wait_seconds": 25})
        st = r["data"]["status"]
        if st == "succeeded":
            return r["data"].get("result") or {}
        if st == "failed":
            raise YuntingError(f"任务失败: {r['data'].get('message')}")
    raise YuntingError(f"任务 {task_id} 超时未完成")


def _download(url: str, timeout: int = 180) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def repair_xlsx(raw: bytes) -> bytes:
    """剥离 xlsx 内 XML 的非法控制字符。实测 3 个月内 2 个文件命中。"""
    zin = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    fixed = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith(".xml"):
                new = _BAD_XML.sub(b"", data)
                fixed += len(data) - len(new)
                data = new
            zout.writestr(item, data)   # 每个条目都要写回，非 xml 原样复制
    zin.close()
    repair_xlsx.last_stripped = fixed  # type: ignore[attr-defined]
    return out.getvalue()


def _slice_windows(start: datetime, end: datetime, days: int) -> Iterator[tuple[str, str]]:
    fmt = "%Y-%m-%d %H:%M:%S"
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=days), end)
        yield cur.strftime(fmt), nxt.strftime(fmt)
        cur = nxt


def export_slice(qtype: str, start: str, end: str, tf: dict | None,
                 mode: str = "incremental", depth: int = 0) -> tuple[bytes | None, dict]:
    """导出一个时间片。触发上限时自动二分重切；零命中按模式区分处置。"""
    tid = _create_task(qtype, start, end, C.EXPORT_CAP, tf)
    res = _await_task(tid)
    meta = {"start": start, "end": end,
            "matched": res.get("matched_count", 0),
            "exported": res.get("exported_count", 0),
            "truncated": bool(res.get("truncated"))}

    if meta["matched"] == 0:
        if mode == "incremental":
            raise ZeroMatch(f"分片零命中 {qtype} {start}~{end}：过滤条件可能写错，"
                            f"与'本周无数据'无法区分，增量模式下阻断")
        meta["zero_match_warned"] = True
        return None, meta

    if meta["truncated"]:
        if depth >= 4:
            raise YuntingError(f"分片 {start}~{end} 二分 4 层后仍触发 5000 上限")
        mid = (datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
               + (datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
                  - datetime.strptime(start, "%Y-%m-%d %H:%M:%S")) / 2)
        ms = mid.strftime("%Y-%m-%d %H:%M:%S")
        a_raw, a_meta = export_slice(qtype, start, ms, tf, mode, depth + 1)
        b_raw, b_meta = export_slice(qtype, ms, end, tf, mode, depth + 1)
        meta["split_into"] = [a_meta, b_meta]
        return (a_raw, b_raw), meta  # type: ignore[return-value]

    url = res.get("file_url")
    if not url:
        return None, meta
    return repair_xlsx(_download(url)), meta


def export_window(qtype: str, start: datetime, end: datetime, tf: dict | None,
                  slice_days: int, mode: str = "incremental") -> tuple[list[bytes], list[dict]]:
    """按天数切片导出整个窗口，返回所有 xlsx 字节与分片元数据。"""
    _handshake()
    blobs: list[bytes] = []
    metas: list[dict] = []

    def _collect(x: Any) -> None:
        if x is None:
            return
        if isinstance(x, tuple):
            for i in x:
                _collect(i)
        else:
            blobs.append(x)

    for s, e in _slice_windows(start, end, slice_days):
        raw, meta = export_slice(qtype, s, e, tf, mode)
        _collect(raw)
        metas.append(meta)
        time.sleep(0.3)
    return blobs, metas

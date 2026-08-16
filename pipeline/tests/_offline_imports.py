"""让纯函数测试在未安装 PostgreSQL 驱动的最小环境也能收集。"""
from __future__ import annotations

import sys
import types


def ensure_psycopg_importable() -> None:
    """只补齐被测模块的 import 表面，任何连库尝试都立即失败。"""
    try:
        __import__("psycopg")
        return
    except ModuleNotFoundError:
        pass

    psycopg = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    types_pkg = types.ModuleType("psycopg.types")
    json_module = types.ModuleType("psycopg.types.json")

    def forbidden_connect(*args, **kwargs):
        del args, kwargs
        raise AssertionError("离线单测禁止连接 PostgreSQL")

    class Jsonb:
        def __init__(self, value):
            self.obj = value

    psycopg.connect = forbidden_connect
    rows.dict_row = object()
    json_module.Jsonb = Jsonb
    psycopg.rows = rows
    psycopg.types = types_pkg
    types_pkg.json = json_module
    sys.modules.update(
        {
            "psycopg": psycopg,
            "psycopg.rows": rows,
            "psycopg.types": types_pkg,
            "psycopg.types.json": json_module,
        }
    )


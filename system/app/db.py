"""PostgreSQL 访问层：应用始终以 voc_human 对应环境变量连接。"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

try:  # 让不安装运行时依赖的纯单元测试仍可导入领域函数。
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
except ModuleNotFoundError:  # pragma: no cover - 部署环境由 pyproject 保证依赖
    psycopg = None
    dict_row = None
    ConnectionPool = None


class DatabaseUnavailable(RuntimeError):
    """数据库驱动或连接配置不可用。"""


class DatabaseWriteError(RuntimeError):
    """可安全展示给 PM 的数据库约束错误。"""


def connection_kwargs() -> dict[str, Any]:
    """只读取 human 身份配置；不提供机器身份或硬编码地址的退路。"""
    names = {
        "host": "VOC_PG_HOST",
        "port": "VOC_PG_PORT",
        "dbname": "VOC_PG_DB",
        "user": "VOC_PG_HUMAN_USER",
        "password": "VOC_PG_HUMAN_PASSWORD",
    }
    missing = [env for env in names.values() if not os.environ.get(env)]
    if missing:
        raise DatabaseUnavailable("缺少数据库环境变量：" + ", ".join(missing))
    cfg = {key: os.environ[env] for key, env in names.items()}
    cfg["port"] = int(cfg["port"])
    return cfg


def readable_db_error(exc: Exception) -> str:
    """提取 PostgreSQL 触发器/约束的主错误，不吞掉业务提示。"""
    diag = getattr(exc, "diag", None)
    message = getattr(diag, "message_primary", None) if diag else None
    if message:
        return str(message)
    text = str(exc).strip()
    return text.splitlines()[0] if text else "数据库拒绝了这次状态变更"


class Database:
    def __init__(self, min_size: int = 1, max_size: int = 8):
        self.min_size = min_size
        self.max_size = max_size
        self._pool: Any = None

    def open(self) -> None:
        if self._pool is not None:
            return
        if ConnectionPool is None:
            raise DatabaseUnavailable("未安装 psycopg；请先安装 system/pyproject.toml 依赖")
        self._pool = ConnectionPool(
            kwargs={**connection_kwargs(), "row_factory": dict_row},
            min_size=self.min_size,
            max_size=self.max_size,
            open=True,
        )

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @contextmanager
    def conn(self) -> Iterator[Any]:
        if self._pool is None:
            self.open()
        with self._pool.connection() as connection:
            yield connection

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        with self.conn() as connection:
            return connection.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        try:
            with self.conn() as connection:
                cursor = connection.execute(sql, params)
                connection.commit()
                return cursor.rowcount
        except Exception as exc:
            if psycopg is not None and isinstance(exc, psycopg.Error):
                raise DatabaseWriteError(readable_db_error(exc)) from exc
            raise


db = Database()

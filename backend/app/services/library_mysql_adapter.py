"""Disposable, TLS-authenticated, read-only MySQL metadata adapter."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ipaddress
import socket
import ssl
from threading import BoundedSemaphore
import time

import pymysql

from ..config import get_settings


_RESOLVER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="library-dns")
_RESOLUTION_SLOTS = BoundedSemaphore(2)
MAX_TABLES = 40
MAX_COLUMNS = 80


class LibraryConfigurationError(ValueError):
    """Safe deployment guidance; never includes connection values."""


class RequiredTLSConnection(pymysql.Connection):
    def _request_authentication(self):
        if not self.server_capabilities & pymysql.constants.CLIENT.SSL:
            raise ValueError("MySQL 服务端必须支持受信 TLS")
        return super()._request_authentication()


def _resolve(host: str, port: int) -> tuple:
    allowed = {item.strip().casefold() for item in get_settings().library_mysql_allowed_hosts.split(",") if item.strip()}
    if host.casefold() not in allowed:
        raise LibraryConfigurationError("请管理员将此 MySQL 主机加入 LIBRARY_MYSQL_ALLOWED_HOSTS 部署允许名单")
    if not _RESOLUTION_SLOTS.acquire(blocking=False):
        raise ValueError("数据库地址解析繁忙，请稍后重试")
    try:
        future = _RESOLVER.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except RuntimeError:
        _RESOLUTION_SLOTS.release()
        raise ValueError("数据库地址解析暂不可用") from None
    future.add_done_callback(lambda _future: _RESOLUTION_SLOTS.release())
    addresses = future.result(timeout=5)
    if not addresses or any(ipaddress.ip_address(item[4][0]).is_unspecified or ipaddress.ip_address(item[4][0]).is_multicast for item in addresses):
        raise ValueError("数据库地址不可用")
    return addresses[0]


def mysql_schema(config: dict, *, timeout_seconds: float = 20, sample=None) -> list[dict] | dict:
    deadline = time.monotonic() + min(20, timeout_seconds)
    family, kind, protocol, _canonical, address = _resolve(config["host"], config["port"])
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("metadata deadline")
    # Connect the socket to the already resolved address, while keeping host for
    # TLS SNI/hostname verification; DNS cannot change between check and connect.
    stream = socket.socket(family, kind, protocol)
    connection = None
    try:
        stream.settimeout(min(5, remaining))
        stream.connect(address)
        connection = RequiredTLSConnection(host=config["host"], port=config["port"],
            user=config["user"], password=config.get("password", ""), database=config["database"],
            ssl=ssl.create_default_context(), ssl_verify_cert=True, ssl_verify_identity=True,
            connect_timeout=5, read_timeout=3, write_timeout=3, local_infile=False,
            charset="utf8mb4", autocommit=False, defer_connect=True)
        connection.connect(sock=stream)
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("SET SESSION MAX_EXECUTION_TIME=3000")
            cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME LIMIT 41", (config["database"],))
            names = cursor.fetchall()
            if len(names) > MAX_TABLES:
                raise ValueError("资料库超过 40 张表，请缩小资料范围")
            tables = []
            for (name,) in names:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("metadata deadline")
                connection._read_timeout = max(0.001, min(3, remaining))
                cursor.execute("SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION LIMIT 81", (config["database"], name))
                columns = cursor.fetchall()
                if len(columns) > MAX_COLUMNS:
                    raise ValueError("资料表超过 80 列，请缩小资料范围")
                tables.append({"name": name, "row_count": -1, "columns": [
                    {"name": column, "type": kind, "pk": key == "PRI"} for column, kind, key in columns]})
            if sample is not None:
                from .library_sample_query import query, result
                statement, values, metadata = query(tables, sample, "mysql")
                cursor.execute("SELECT TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (config["database"], metadata["name"]))
                if cursor.fetchone() != ("BASE TABLE",):
                    raise ValueError("只允许读取持久表样本")
                cursor.execute(statement, values)
                return result(cursor.fetchmany(sample.limit + 1), metadata, sample)
            return tables
    finally:
        if connection is not None:
            connection.close()
        stream.close()

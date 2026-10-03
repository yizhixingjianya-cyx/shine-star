"""Global database configuration API.

Exposes the ``database`` section of the AstrBot config so the dashboard can
switch the whole bot between SQLite and MySQL, verify connectivity and inspect
the live backend and its schema. Changes take effect after an AstrBot restart
because the engine is created during process start-up.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request

from astrbot.dashboard.api.auth import AuthContext
from astrbot.dashboard.api.config_profiles import require_config_scope
from astrbot.dashboard.responses import error, ok
from astrbot.dashboard.schemas import DatabaseConfigRequest, DatabaseTestRequest

router = APIRouter(prefix="/database", tags=["Database"])

_SCHEMA_KEY = "database"
_ALLOWED_TYPES = {"sqlite", "mysql"}


def _get_config_manager(request: Request):
    return request.app.state.core_lifecycle.astrbot_config_mgr


def _get_db(request: Request):
    return request.app.state.core_lifecycle.db


def _redact_url(url: str) -> str:
    """Mask the password inside a SQLAlchemy database URL.

    Args:
        url: The raw ``DATABASE_URL`` value.

    Returns:
        The URL with the password replaced by ``***``.
    """
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if not rest:
        return url
    credentials, _, host = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def _build_url(settings: dict) -> str:
    """Build a MySQL URL from database settings.

    Args:
        settings: Database configuration mapping.

    Returns:
        An ``mysql+aiomysql`` SQLAlchemy URL.
    """
    from astrbot.core.db.mysql import build_mysql_url

    return build_mysql_url(
        host=str(settings.get("mysql_host", "") or ""),
        port=int(settings.get("mysql_port", 3306) or 3306),
        user=str(settings.get("mysql_user", "") or ""),
        password=str(settings.get("mysql_password", "") or ""),
        database=str(settings.get("mysql_database", "") or ""),
        charset=str(settings.get("mysql_charset", "utf8mb4") or "utf8mb4"),
    )


@router.get("")
async def get_database_config(
    request: Request,
    _auth: AuthContext = Depends(require_config_scope),
):
    """Return the configured database settings and the live backend state."""
    config_mgr = _get_config_manager(request)
    default_config = config_mgr.confs["default"]
    settings = dict(default_config.get(_SCHEMA_KEY) or {})
    settings.pop("mysql_password", None)

    db = _get_db(request)
    live_url = str(getattr(db, "DATABASE_URL", "") or "")
    live_dialect = "mysql" if live_url.startswith("mysql") else "sqlite"

    tables: list[str] = []
    try:
        from sqlalchemy import text

        async with db.get_db() as session:
            result = await session.execute(
                text(
                    "SHOW TABLES"
                    if live_dialect == "mysql"
                    else "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
            for row in result.all():
                name = row[0]
                if isinstance(name, str) and (name.startswith("mem_")):
                    tables.append(name)
    except Exception:
        tables = []

    return ok(
        {
            "settings": settings,
            "live": {
                "dialect": live_dialect,
                "url": _redact_url(live_url),
                "memory_tables": sorted(tables),
                "restart_required": bool(settings.get("type") == "mysql")
                and live_dialect != "mysql",
            },
        }
    )


@router.put("")
async def update_database_config(
    request: Request,
    payload: DatabaseConfigRequest,
    _auth: AuthContext = Depends(require_config_scope),
):
    """Persist the global database settings.

    The payload is merged into the ``database`` section of the default config
    profile. A restart is required for the change to take effect.
    """
    data = payload.model_dump(exclude_none=True)
    db_type = data.get("type")
    if db_type is not None and db_type not in _ALLOWED_TYPES:
        return error(f"不支持的数据库类型: {db_type}")

    if db_type == "mysql":
        host = str(data.get("mysql_host", "") or "").strip()
        user = str(data.get("mysql_user", "") or "").strip()
        database = str(data.get("mysql_database", "") or "").strip()
        if not host or not user or not database:
            return error("选择 MySQL 时必须填写主机、用户名和数据库名")

    config_mgr = _get_config_manager(request)
    default_config = config_mgr.confs["default"]
    section = dict(default_config.get(_SCHEMA_KEY) or {})
    section.update(data)
    default_config[_SCHEMA_KEY] = section
    try:
        save = getattr(default_config, "save_config_async", None)
        if callable(save):
            await save()
        else:
            default_config.save_config()
    except Exception as exc:  # noqa: BLE001
        return error(f"保存失败: {exc}")
    return ok({"message": "已保存，重启 AstrBot 后生效", "settings": section})


@router.post("/test")
async def test_database_connection(
    request: Request,
    payload: DatabaseTestRequest,
    _auth: AuthContext = Depends(require_config_scope),
):
    """Test a MySQL connection without persisting anything."""
    data = payload.model_dump()
    if (
        not data.get("mysql_host")
        or not data.get("mysql_user")
        or not data.get("mysql_database")
    ):
        return error("请填写主机、用户名和数据库名")

    url = _build_url(data)
    try:
        from sqlalchemy import text

        from astrbot.core.db.mysql import MySQLDatabase

        probe = MySQLDatabase(url)

        async def _run() -> None:
            async with probe.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await probe.engine.dispose()

        await asyncio.wait_for(_run(), timeout=12)
    except Exception as exc:  # noqa: BLE001
        return error(f"连接失败: {exc}")
    return ok(
        {
            "message": f"连接成功: {data['mysql_host']}:{data.get('mysql_port', 3306)}/{data['mysql_database']}"
        }
    )


@router.get("/schema")
async def get_database_schema(
    _auth: AuthContext = Depends(require_config_scope),
):
    """Return the configurable database fields for the dashboard form."""
    return ok(
        {
            "fields": [
                {
                    "key": "type",
                    "label": "数据库类型",
                    "type": "select",
                    "options": [
                        {"value": "sqlite", "label": "SQLite（本地文件，默认）"},
                        {"value": "mysql", "label": "MySQL / MariaDB"},
                    ],
                    "hint": "切换后需要重启 AstrBot 才能生效。",
                },
                {"key": "mysql_host", "label": "MySQL 主机", "type": "string"},
                {
                    "key": "mysql_port",
                    "label": "MySQL 端口",
                    "type": "int",
                    "default": 3306,
                },
                {"key": "mysql_user", "label": "MySQL 用户名", "type": "string"},
                {"key": "mysql_password", "label": "MySQL 密码", "type": "password"},
                {"key": "mysql_database", "label": "MySQL 数据库名", "type": "string"},
                {
                    "key": "mysql_charset",
                    "label": "字符集",
                    "type": "string",
                    "default": "utf8mb4",
                },
            ]
        }
    )

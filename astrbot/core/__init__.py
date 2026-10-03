import os

from astrbot.core.config import AstrBotConfig
from astrbot.core.config.default import DB_PATH
from astrbot.core.db import BaseDatabase
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.file_token_service import FileTokenService
from astrbot.core.utils.pip_installer import (
    DependencyConflictError as DependencyConflictError,
)
from astrbot.core.utils.pip_installer import (
    PipInstaller,
)
from astrbot.core.utils.requirements_utils import (
    RequirementsPrecheckFailed as RequirementsPrecheckFailed,
)
from astrbot.core.utils.requirements_utils import (
    find_missing_requirements as find_missing_requirements,
)
from astrbot.core.utils.requirements_utils import (
    find_missing_requirements_or_raise as find_missing_requirements_or_raise,
)
from astrbot.core.utils.shared_preferences import SharedPreferences
from astrbot.core.utils.t2i.renderer import HtmlRenderer

from .log import LogBroker, LogManager  # noqa
from .utils.astrbot_path import get_astrbot_data_path

# 初始化数据存储文件夹
os.makedirs(get_astrbot_data_path(), exist_ok=True)

DEMO_MODE = os.getenv("DEMO_MODE", "False").strip().lower() in ("true", "1", "t")

astrbot_config = AstrBotConfig()
t2i_base_url = astrbot_config.get("t2i_endpoint", "https://t2i.soulter.top/text2img")
html_renderer = HtmlRenderer(t2i_base_url)
logger = LogManager.GetLogger(log_name="astrbot")
LogManager.configure_logger(logger, astrbot_config)
LogManager.configure_trace_logger(astrbot_config)


def _build_database() -> BaseDatabase:
    """Build the configured database backend.

    The backend is chosen from ``database.type`` in the AstrBot config. When the
    key is absent or set to ``sqlite``/``follow_core`` the built-in SQLite file
    under the data directory is used, preserving backwards compatibility.

    Returns:
        A ready-to-initialize database helper.
    """
    db_settings = astrbot_config.get("database") or {}
    db_type = str(db_settings.get("type", "sqlite") or "sqlite").strip().lower()
    if db_type == "mysql":
        from astrbot.core.db.mysql import MySQLDatabase, build_mysql_url

        url = build_mysql_url(
            host=str(db_settings.get("mysql_host", "") or ""),
            port=int(db_settings.get("mysql_port", 3306) or 3306),
            user=str(db_settings.get("mysql_user", "") or ""),
            password=str(db_settings.get("mysql_password", "") or ""),
            database=str(db_settings.get("mysql_database", "") or ""),
            charset=str(db_settings.get("mysql_charset", "utf8mb4") or "utf8mb4"),
        )
        return MySQLDatabase(url)
    return SQLiteDatabase(DB_PATH)


db_helper = _build_database()


def _build_image_host() -> None:
    """Configure the shared image host from the global ``image_host`` config.

    The image host is process-wide: plugins register their own image directory
    under a URL prefix and reuse this single read-only static server, so the
    public address / bind / port live in exactly one place in the AstrBot config.
    """
    settings = astrbot_config.get("image_host") or {}
    if not settings.get("enable", True):
        return
    base_url = str(settings.get("base_url", "") or "").strip()
    if not base_url:
        return
    from astrbot.core.utils import image_host

    image_host.configure(
        base_url=base_url,
        bind=str(settings.get("bind", "0.0.0.0") or "0.0.0.0"),
        port=int(settings.get("port", 11453) or 11453),
    )


_build_image_host()
# 简单的偏好设置存储, 这里后续应该存储到数据库中, 一些部分可以存储到配置中
sp = SharedPreferences(db_helper=db_helper)
# 文件令牌服务
file_token_service = FileTokenService()
pip_installer = PipInstaller(
    astrbot_config.get("pip_install_arg", ""),
    astrbot_config.get("pypi_index_url", None),
)

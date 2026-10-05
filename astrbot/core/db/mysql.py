"""MySQL database backend for AstrBot.

The backend reuses :class:`~astrbot.core.db.sqlite.SQLiteDatabase` for every
portable SQLAlchemy query and overrides only the dialect-specific pieces:

* the engine / connection URL (``mysql+aiomysql``);
* table bootstrap (no SQLite ``PRAGMA`` statements);
* ``INSERT ... ON CONFLICT`` upserts, which become ``ON DUPLICATE KEY UPDATE``.

Keeping the inheritance one-way means both backends stay behaviourally aligned
without duplicating the ~70 database methods of ``BaseDatabase``.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel, col, select

from astrbot.core.db.po import PlatformStat, UmoAlias
from astrbot.core.db.sqlite import SQLiteDatabase

# Use the shared astrbot logger by name rather than importing it from
# ``astrbot.core``, which would be circular: that package imports this module.
logger = logging.getLogger("astrbot")

__all__ = ["MySQLDatabase", "build_mysql_url"]


def build_mysql_url(
    *,
    host: str,
    port: int = 3306,
    user: str,
    password: str = "",
    database: str,
    charset: str = "utf8mb4",
) -> str:
    """Build an SQLAlchemy async MySQL URL.

    Args:
        host: Database host.
        port: Database port.
        user: Database user.
        password: Database password.
        database: Database (schema) name.
        charset: Connection charset.

    Returns:
        An ``mysql+aiomysql`` SQLAlchemy URL with URL-encoded credentials.
    """
    encoded_user = quote_plus(str(user or ""))
    encoded_password = quote_plus(str(password or ""))
    credentials = (
        f"{encoded_user}:{encoded_password}" if encoded_password else encoded_user
    )
    return (
        f"mysql+aiomysql://{credentials}@{host}:{int(port)}/{database}"
        f"?charset={charset}"
    )


class MySQLDatabase(SQLiteDatabase):
    """MySQL backend sharing the SQLite query layer.

    The SQLite query layer is reused by inheritance (all portable SQLAlchemy
    queries and the ~70 ``BaseDatabase`` methods come from
    :class:`~astrbot.core.db.sqlite.SQLiteDatabase`), but the SQLite
    *constructor* is deliberately bypassed: it hardcodes
    ``self.DATABASE_URL = sqlite+aiosqlite:///...`` and builds a SQLite engine,
    which for MySQL would allocate a connection pool that is never used and
    never disposed.
    """

    def __init__(self, url: str) -> None:
        """Initialize the MySQL backend.

        Args:
            url: A full ``mysql+aiomysql://`` SQLAlchemy URL.
        """
        self.DATABASE_URL = url
        self.db_path = ""
        self.inited = False
        # The event loop the current engine/pool was created on. aiomysql binds
        # its connections to that loop; reusing them from another loop raises
        # "attached to a different loop" and leaks the connection.
        self._current_loop: asyncio.AbstractEventLoop | None = None
        # Initialize the engine/session factory for the MySQL URL directly. We
        # intentionally do NOT call ``SQLiteDatabase.__init__`` (see class
        # docstring), so no SQLite engine is ever constructed.
        self._rebuild_engine()

    def _rebuild_engine(self) -> None:
        """Create the engine and session factory for the MySQL URL."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        previous = getattr(self, "engine", None)
        if previous is not None:
            # Drop the old pool; otherwise every loop switch leaks one.
            try:
                previous.sync_engine.dispose(close=False)
            except Exception:
                logger.debug("disposing previous MySQL engine failed", exc_info=True)

        self.engine = create_async_engine(
            self.DATABASE_URL,
            echo=False,
            future=True,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"connect_timeout": 10},
        )
        self.AsyncSessionLocal = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @asynccontextmanager
    async def get_db(self):
        """Yield a session bound to the current event loop.

        aiomysql pools are bound to the loop that created them, so when the
        running loop changes the engine is rebuilt and re-initialized. Without
        this, calls made from a different loop fail with "attached to a
        different loop" and the connection is never returned to the pool.
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is not None and self._current_loop is not current_loop:
            self._rebuild_engine()
            self._current_loop = current_loop
            self.inited = False

        if not self.inited:
            await self.initialize()
            self.inited = True

        async with self.AsyncSessionLocal() as session:
            yield session

    async def initialize(self) -> None:
        """Create missing tables and skip SQLite-only migrations."""
        async with self.engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
            await conn.commit()

    async def insert_platform_stats(
        self,
        platform_id: str,
        platform_type: str,
        count: int = 1,
        timestamp: datetime | None = None,
    ) -> None:
        """Insert or increment a platform statistic record.

        Args:
            platform_id: Platform identifier.
            platform_type: Platform adapter type.
            count: Increment amount.
            timestamp: Optional event timestamp.
        """
        async with self.get_db() as session:
            session: AsyncSession
            async with session.begin():
                current_hour = timestamp or datetime.now().replace(
                    minute=0,
                    second=0,
                    microsecond=0,
                )
                statement = mysql_insert(PlatformStat).values(
                    timestamp=current_hour,
                    platform_id=platform_id,
                    platform_type=platform_type,
                    count=count,
                )
                statement = statement.on_duplicate_key_update(
                    count=PlatformStat.__table__.c.count + statement.inserted.count
                )
                await session.execute(statement)

    async def get_platform_stats(self, offset_sec: int = 86400) -> list[PlatformStat]:
        """Return platform statistics within a look-back window.

        Args:
            offset_sec: Look-back window in seconds.

        Returns:
            Aggregated platform statistics rows.
        """
        from datetime import timedelta

        async with self.get_db() as session:
            session: AsyncSession
            start_time = datetime.now() - timedelta(seconds=offset_sec)
            result = await session.execute(
                select(PlatformStat)
                .where(col(PlatformStat.timestamp) >= start_time)
                .order_by(col(PlatformStat.timestamp).desc())
            )
            return list(result.scalars().all())

    async def upsert_umo_alias(
        self,
        umo: str,
        creator_sender_id: str,
        auto_name: str,
        user_alias: str | None,
    ) -> UmoAlias:
        """Insert or update a UMO alias row.

        Args:
            umo: Unified message origin.
            creator_sender_id: Sender that first created the alias.
            auto_name: Automatic display name.
            user_alias: Manual display alias.

        Returns:
            The persisted alias row.
        """
        now = datetime.now(timezone.utc)
        statement = mysql_insert(UmoAlias).values(
            umo=umo,
            creator_sender_id=creator_sender_id,
            auto_name=auto_name,
            user_alias=user_alias,
            created_at=now,
            updated_at=now,
        )
        statement = statement.on_duplicate_key_update(
            creator_sender_id=statement.inserted.creator_sender_id,
            auto_name=statement.inserted.auto_name,
            user_alias=statement.inserted.user_alias,
            updated_at=now,
        )
        async with self.get_db() as session:
            session: AsyncSession
            async with session.begin():
                await session.execute(statement)
                result = await session.execute(
                    select(UmoAlias).where(col(UmoAlias.umo) == umo)
                )
                return result.scalar_one()

    async def upsert_umo_auto_name(
        self,
        umo: str,
        creator_sender_id: str,
        auto_name: str,
    ) -> None:
        """Persist an automatic UMO name without touching the manual alias.

        Args:
            umo: Unified message origin.
            creator_sender_id: Sender that first caused the UMO to be recorded.
            auto_name: Name discovered from the inbound platform message.
        """
        now = datetime.now(timezone.utc)
        statement = mysql_insert(UmoAlias).values(
            umo=umo,
            creator_sender_id=creator_sender_id,
            auto_name=auto_name,
            user_alias=None,
            created_at=now,
            updated_at=now,
        )
        statement = statement.on_duplicate_key_update(
            auto_name=statement.inserted.auto_name,
            updated_at=now,
        )
        async with self.get_db() as session:
            session: AsyncSession
            async with session.begin():
                await session.execute(statement)

    async def _ensure_persona_folder_columns(self, conn: Any) -> None:
        """MySQL uses ``create_all`` for schema, so no column backfill is needed.

        Args:
            conn: Active connection (unused).
        """
        return None

    async def _ensure_persona_skills_column(self, conn: Any) -> None:
        """No-op for MySQL; schema is created by ``SQLModel.metadata``.

        Args:
            conn: Active connection (unused).
        """
        return None

    async def _ensure_persona_custom_error_message_column(self, conn: Any) -> None:
        """No-op for MySQL; schema is created by ``SQLModel.metadata``.

        Args:
            conn: Active connection (unused).
        """
        return None

    async def _ensure_platform_message_history_checkpoint_column(
        self, conn: Any
    ) -> None:
        """No-op for MySQL; schema is created by ``SQLModel.metadata``.

        Args:
            conn: Active connection (unused).
        """
        return None

    async def _ensure_chatui_project_workspace_columns(self, conn: Any) -> None:
        """No-op for MySQL; schema is created by ``SQLModel.metadata``.

        Args:
            conn: Active connection (unused).
        """
        return None

    async def get_filtered_conversations(self, *args: Any, **kwargs: Any):
        """Return filtered conversations using a MySQL-safe query.

        The SQLite implementation appends an ``INDEXED BY`` hint for
        multi-platform pagination, which is not valid MySQL syntax. Forcing the
        join-free path avoids that hint while keeping the same filters.

        Args:
            *args: Positional arguments forwarded to the query builder.
            **kwargs: Filter arguments accepted by the dashboard service.

        Returns:
            A tuple of (rows, total count).
        """
        kwargs["platforms"] = []
        return await super().get_filtered_conversations(*args, **kwargs)

    async def _ensure_conversation_indexes(self, conn: Any) -> None:
        """Create the dashboard conversation indexes on MySQL.

        MySQL has no ``CREATE INDEX IF NOT EXISTS``; a duplicate index raises and
        is swallowed by the caller during initialization.

        Args:
            conn: Active connection.
        """
        for statement in (
            "CREATE INDEX ix_conversations_created_at_inner_id "
            "ON conversations (created_at DESC, inner_conversation_id DESC)",
            "CREATE INDEX ix_conversations_platform_created_at_inner_id "
            "ON conversations (platform_id, created_at DESC, "
            "inner_conversation_id DESC)",
        ):
            try:
                await conn.execute(text(statement))
            except Exception:
                continue

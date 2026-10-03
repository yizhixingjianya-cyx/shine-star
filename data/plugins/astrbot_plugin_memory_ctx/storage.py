"""SQLModel storage layer for the memory-context plugin.

The tables are added to the main AstrBot database so the plugin inherits the
same SQLite / MySQL persistence stack as the core. Tables are created lazily
through ``SQLModel.metadata.create_all`` which only creates missing tables.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import JSON, Text, inspect, text
from sqlmodel import Field, SQLModel, col, delete, func, select


def now_ts() -> int:
    """Return the current UTC timestamp in seconds.

    Returns:
        The current Unix timestamp as an integer.
    """
    return int(time.time())


class MemorySegment(SQLModel, table=True):
    """A compressed memory record.

    Tier 1 rows are per-day working memories; tier 2 rows are weekly long-term
    memories aggregated from tier 1 rows.
    """

    __tablename__ = "mem_segment"
    # Reuse the existing table when the plugin module is re-imported on reload;
    # otherwise SQLModel raises "Table 'mem_segment' is already defined".
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(
        default=None,
        primary_key=True,
        sa_column_kwargs={"autoincrement": True},
    )
    umo: str = Field(index=True)
    session_type: str = Field(default="unknown")
    tier: int = Field(default=1, index=True)
    period_key: str = Field(default="", index=True)
    seg_start_ts: int = Field(default=0)
    seg_end_ts: int = Field(default=0)
    summary: str = Field(sa_type=Text)
    summary_tokens: int = Field(default=0)
    msg_count: int = Field(default=0)
    model_used: str | None = Field(default=None)
    embedding: list[float] | None = Field(default=None, sa_type=JSON)
    created_at: int = Field(default_factory=now_ts, index=True)
    updated_at: int = Field(default_factory=now_ts)
    expires_at: int = Field(default=0, index=True)


class RawArchive(SQLModel, table=True):
    """Soft-deleted raw messages kept as a cold backup for recovery."""

    __tablename__ = "mem_raw_archive"
    __table_args__ = {"extend_existing": True}

    id: int | None = Field(
        default=None,
        primary_key=True,
        sa_column_kwargs={"autoincrement": True},
    )
    umo: str = Field(index=True)
    conversation_id: str = Field(default="")
    role: str = Field(default="unknown")
    payload: dict | None = Field(default=None, sa_type=JSON)
    created_at: int = Field(default=0)
    archived_at: int = Field(default_factory=now_ts, index=True)


class CompressCursor(SQLModel, table=True):
    """Per-session compression state."""

    __tablename__ = "mem_cursor"
    __table_args__ = {"extend_existing": True}

    umo: str = Field(primary_key=True)
    last_compress_at: int = Field(default=0)
    last_global_at: int = Field(default=0)
    last_error: str | None = Field(default=None, sa_type=Text)


class MemoryStore:
    """Async data access helper for the memory tables."""

    def __init__(self, db: Any) -> None:
        """Initialize the store.

        Args:
            db: The ``BaseDatabase`` instance exposed by the plugin context.
        """
        self.db = db

    @property
    def engine(self) -> Any:
        """Return the underlying SQLAlchemy async engine."""
        return self.db.engine

    async def ensure_tables(self, retries: int = 3) -> list[str]:
        """Create the plugin tables and report any that are still missing.

        Creation is retried because a transient database error would otherwise
        leave the plugin running without its tables, which silently disables
        long-term memory injection.

        Args:
            retries: Number of creation attempts before giving up.

        Returns:
            The names of tables that could not be created or verified. An empty
            list means every plugin table is present.

        Raises:
            Exception: The last error raised while creating or verifying the
                tables, when every attempt failed.
        """
        table_names = (
            MemorySegment.__tablename__,
            RawArchive.__tablename__,
            CompressCursor.__tablename__,
        )
        last_exc: Exception | None = None
        missing: list[str] = list(table_names)
        for _ in range(max(1, retries)):
            try:
                async with self.engine.begin() as conn:
                    await conn.run_sync(SQLModel.metadata.create_all)
                async with self.engine.connect() as conn:
                    existing = await conn.run_sync(
                        lambda sync_conn: set(inspect(sync_conn).get_table_names())
                    )
                missing = [name for name in table_names if name not in existing]
                last_exc = None
            except Exception as exc:
                last_exc = exc
                missing = list(table_names)
                continue
            if not missing:
                break
        if last_exc is not None:
            raise last_exc
        return missing

    async def list_session_umos(self) -> list[str]:
        """Return every distinct session identifier stored by AstrBot.

        Returns:
            A list of unified message origins.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                text("SELECT DISTINCT user_id FROM conversations")
            )
            return [row[0] for row in result.all() if row[0]]

    async def get_cursor(self, umo: str) -> CompressCursor | None:
        """Return the compression cursor for a session.

        Args:
            umo: Unified message origin.

        Returns:
            The cursor row, or None when it has never been written.
        """
        async with self.db.get_db() as session:
            return await session.get(CompressCursor, umo)

    async def upsert_cursor(self, umo: str, **fields: Any) -> None:
        """Create or update the compression cursor for a session.

        Args:
            umo: Unified message origin.
            **fields: Cursor columns to set.
        """
        async with self.db.get_db() as session:
            cursor = await session.get(CompressCursor, umo)
            if cursor is None:
                cursor = CompressCursor(umo=umo)
            for key, value in fields.items():
                setattr(cursor, key, value)
            session.add(cursor)
            await session.commit()

    async def upsert_segment(
        self,
        *,
        umo: str,
        session_type: str,
        tier: int,
        period_key: str,
        seg_start_ts: int,
        seg_end_ts: int,
        summary: str,
        summary_tokens: int,
        msg_count: int,
        model_used: str | None,
        ttl_days: int,
        embedding: list[float] | None = None,
    ) -> MemorySegment:
        """Insert or refresh a memory segment identified by (umo, tier, period).

        Args:
            umo: Unified message origin.
            session_type: ``group`` / ``friend`` / ``other``.
            tier: 1 for daily memory, 2 for weekly long-term memory.
            period_key: Day key (YYYY-MM-DD) or week key (YYYY-Www).
            seg_start_ts: Segment start timestamp.
            seg_end_ts: Segment end timestamp.
            summary: The compressed summary text.
            summary_tokens: Estimated token count of the summary.
            msg_count: Number of raw messages folded into the summary.
            model_used: Provider ID used to build the summary.
            ttl_days: Retention window in days.
            embedding: Optional embedding vector of the summary.

        Returns:
            The persisted segment row.
        """
        ts = now_ts()
        expires = ts + int(ttl_days) * 86400
        async with self.db.get_db() as session:
            row = (
                (
                    await session.execute(
                        select(MemorySegment).where(
                            col(MemorySegment.umo) == umo,
                            col(MemorySegment.tier) == tier,
                            col(MemorySegment.period_key) == period_key,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = MemorySegment(
                    umo=umo,
                    session_type=session_type,
                    tier=tier,
                    period_key=period_key,
                    created_at=ts,
                )
            row.session_type = session_type
            row.seg_start_ts = seg_start_ts
            row.seg_end_ts = seg_end_ts
            row.summary = summary
            row.summary_tokens = summary_tokens
            row.msg_count = msg_count
            row.model_used = model_used
            row.embedding = embedding
            row.updated_at = ts
            row.expires_at = expires
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def list_segments(
        self,
        *,
        tier: int | None = None,
        umo: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[MemorySegment], int]:
        """List memory segments with pagination.

        Args:
            tier: Optional tier filter.
            umo: Optional session filter.
            page: 1-based page number.
            page_size: Page size.

        Returns:
            A tuple of (rows, total_count).
        """
        conditions = []
        if tier is not None:
            conditions.append(col(MemorySegment.tier) == tier)
        if umo:
            conditions.append(col(MemorySegment.umo) == umo)
        async with self.db.get_db() as session:
            count_stmt = select(func.count()).select_from(MemorySegment)
            stmt = select(MemorySegment)
            for condition in conditions:
                count_stmt = count_stmt.where(condition)
                stmt = stmt.where(condition)
            total = int((await session.execute(count_stmt)).scalar() or 0)
            stmt = (
                stmt.order_by(col(MemorySegment.updated_at).desc())
                .offset(max(0, (page - 1) * page_size))
                .limit(page_size)
            )
            rows = list((await session.execute(stmt)).scalars().all())
            return rows, total

    async def stats(self, umo: str | None = None) -> dict[str, int]:
        """Return record counts grouped by tier.

        Args:
            umo: Optional session filter.

        Returns:
            A mapping with ``tier1``, ``tier2`` and ``total`` counts.
        """
        async with self.db.get_db() as session:
            result: dict[str, int] = {}
            for tier in (1, 2):
                stmt = (
                    select(func.count())
                    .select_from(MemorySegment)
                    .where(col(MemorySegment.tier) == tier)
                )
                if umo:
                    stmt = stmt.where(col(MemorySegment.umo) == umo)
                result[f"tier{tier}"] = int((await session.execute(stmt)).scalar() or 0)
            result["total"] = result.get("tier1", 0) + result.get("tier2", 0)
            return result

    async def archive_messages(
        self,
        umo: str,
        conversation_id: str,
        messages: list[dict],
    ) -> int:
        """Archive raw messages that are being dropped from the context.

        Args:
            umo: Unified message origin.
            conversation_id: Source conversation ID.
            messages: Raw OpenAI-format messages to archive.

        Returns:
            Number of archived messages.
        """
        archived = [m for m in messages if isinstance(m, dict)]
        if not archived:
            return 0
        ts = now_ts()
        async with self.db.get_db() as session:
            for message in archived:
                session.add(
                    RawArchive(
                        umo=umo,
                        conversation_id=conversation_id,
                        role=str(message.get("role", "unknown")),
                        payload=message,
                        created_at=ts,
                    )
                )
            await session.commit()
        return len(archived)

    async def pop_archive(self, umo: str, limit: int = 200) -> list[RawArchive]:
        """Return and delete archived raw messages for a session.

        Args:
            umo: Unified message origin.
            limit: Maximum number of rows to restore.

        Returns:
            The restored archive rows in chronological order.
        """
        async with self.db.get_db() as session:
            rows = list(
                (
                    await session.execute(
                        select(RawArchive)
                        .where(col(RawArchive.umo) == umo)
                        .order_by(col(RawArchive.id).asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            if rows:
                ids = [row.id for row in rows if row.id is not None]
                if ids:
                    await session.execute(
                        delete(RawArchive).where(col(RawArchive.id).in_(ids))
                    )
                    await session.commit()
            return rows

    async def delete_segments(
        self,
        *,
        umo: str | None = None,
        tier: int | None = None,
    ) -> int:
        """Delete memory segments by session and/or tier.

        Args:
            umo: Optional session filter. None means every session.
            tier: Optional tier filter. None means both tiers.

        Returns:
            Number of deleted rows.
        """
        conditions = []
        if umo:
            conditions.append(col(MemorySegment.umo) == umo)
        if tier is not None:
            conditions.append(col(MemorySegment.tier) == tier)
        async with self.db.get_db() as session:
            statement = delete(MemorySegment)
            for condition in conditions:
                statement = statement.where(condition)
            result = await session.execute(statement)
            await session.commit()
            return int(result.rowcount or 0)

    async def delete_archives(self, *, umo: str | None = None) -> int:
        """Delete archived raw messages.

        Args:
            umo: Optional session filter. None means every session.

        Returns:
            Number of deleted rows.
        """
        async with self.db.get_db() as session:
            statement = delete(RawArchive)
            if umo:
                statement = statement.where(col(RawArchive.umo) == umo)
            result = await session.execute(statement)
            await session.commit()
            return int(result.rowcount or 0)

    async def reset_cursors(self, *, umo: str | None = None) -> int:
        """Delete compression cursors.

        Args:
            umo: Optional session filter. None means every session.

        Returns:
            Number of deleted rows.
        """
        async with self.db.get_db() as session:
            statement = delete(CompressCursor)
            if umo:
                statement = statement.where(col(CompressCursor.umo) == umo)
            result = await session.execute(statement)
            await session.commit()
            return int(result.rowcount or 0)

    async def list_umo_summaries(self) -> list[dict]:
        """List per-session memory counts for the dashboard.

        Returns:
            One row per session with tier-1 and tier-2 record counts.
        """
        async with self.db.get_db() as session:
            stmt = select(
                MemorySegment.umo,
                MemorySegment.session_type,
                func.count().label("count"),
                func.max(MemorySegment.updated_at).label("latest"),
            ).group_by(MemorySegment.umo, MemorySegment.session_type)
            result = await session.execute(stmt)
            sessions: dict[str, dict] = {}
            for row in result.all():
                item = sessions.setdefault(
                    row.umo,
                    {
                        "umo": row.umo,
                        "session_type": row.session_type,
                        "daily": 0,
                        "long_term": 0,
                        "latest": 0,
                    },
                )
                item["latest"] = max(item["latest"], int(row.latest or 0))
            tier_stmt = select(
                MemorySegment.umo, MemorySegment.tier, func.count()
            ).group_by(MemorySegment.umo, MemorySegment.tier)
            for umo, tier, count in (await session.execute(tier_stmt)).all():
                item = sessions.setdefault(
                    umo,
                    {
                        "umo": umo,
                        "session_type": "",
                        "daily": 0,
                        "long_term": 0,
                        "latest": 0,
                    },
                )
                if tier == 2:
                    item["long_term"] = int(count)
                else:
                    item["daily"] = int(count)
            return sorted(sessions.values(), key=lambda x: x["latest"], reverse=True)

    async def list_cursors(self) -> dict[str, dict]:
        """Return per-session compression cursors keyed by UMO.

        Returns:
            Mapping of UMO to its cursor fields (timestamps and last error).
        """
        async with self.db.get_db() as session:
            rows = (await session.execute(select(CompressCursor))).scalars().all()
        return {
            row.umo: {
                "last_compress_at": int(row.last_compress_at or 0),
                "last_global_at": int(row.last_global_at or 0),
                "last_error": row.last_error or "",
            }
            for row in rows
        }

    async def purge_expired(self, now: int | None = None) -> int:
        """Delete memory segments whose retention window has elapsed.

        Args:
            now: Optional override of the current timestamp.

        Returns:
            Number of deleted rows.
        """
        ts = now if now is not None else now_ts()
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(MemorySegment).where(
                    col(MemorySegment.expires_at) > 0,
                    col(MemorySegment.expires_at) <= ts,
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

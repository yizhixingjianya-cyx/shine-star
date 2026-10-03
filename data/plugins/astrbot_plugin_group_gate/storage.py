"""SQLModel storage for the group-gate (registration) plugin.

Tables live in the main AstrBot database so the plugin inherits the same
SQLite / MySQL persistence stack as the core.
"""

from __future__ import annotations

import time

from sqlmodel import Field, SQLModel, col, delete, select


def now_ts() -> int:
    """Return the current UTC timestamp in seconds.

    Returns:
        The current Unix timestamp as an integer.
    """
    return int(time.time())


class GateGroup(SQLModel, table=True):
    """A known group chat and whether it is allowed to talk to the bot."""

    __tablename__ = "gate_group"
    __table_args__ = {"extend_existing": True}

    umo: str = Field(primary_key=True)
    allowed: bool = Field(default=False, index=True)
    note: str = Field(default="")
    source: str = Field(default="manual")
    created_at: int = Field(default_factory=now_ts)
    updated_at: int = Field(default_factory=now_ts)


class GateCode(SQLModel, table=True):
    """A registration code that grants a group access when redeemed."""

    __tablename__ = "gate_code"
    __table_args__ = {"extend_existing": True}

    code: str = Field(primary_key=True)
    note: str = Field(default="")
    max_uses: int = Field(default=1)
    uses: int = Field(default=0)
    enabled: bool = Field(default=True)
    created_at: int = Field(default_factory=now_ts)
    used_at: int = Field(default=0)
    used_by: str = Field(default="")


class GateStore:
    """Persistence helper for the group whitelist and registration codes."""

    def __init__(self, db: object) -> None:
        """Initialize the store.

        Args:
            db: The AstrBot database helper.
        """
        self.db = db

    async def ensure_tables(self) -> None:
        """Create the plugin tables when they are missing."""
        async with self.db.engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------
    async def get_group(self, umo: str) -> GateGroup | None:
        """Fetch one group row.

        Args:
            umo: Unified message origin.

        Returns:
            The row, or None when the group is unknown.
        """
        async with self.db.get_db() as session:
            return (
                (
                    await session.execute(
                        select(GateGroup).where(col(GateGroup.umo) == umo)
                    )
                )
                .scalars()
                .first()
            )

    async def is_allowed(self, umo: str) -> bool | None:
        """Return the whitelist state of a group.

        Args:
            umo: Unified message origin.

        Returns:
            True when allowed, False when explicitly denied, None when the
            group has never been seen.
        """
        row = await self.get_group(umo)
        if row is None:
            return None
        return bool(row.allowed)

    async def set_allowed(
        self, umo: str, allowed: bool, note: str = "", source: str = "manual"
    ) -> GateGroup:
        """Insert or update the whitelist state of a group.

        Args:
            umo: Unified message origin.
            allowed: Whether the group may talk to the bot.
            note: Free-form note stored with the row.
            source: How the state was set (``manual`` / ``code``).

        Returns:
            The persisted row.
        """
        ts = now_ts()
        async with self.db.get_db() as session:
            row = (
                (
                    await session.execute(
                        select(GateGroup).where(col(GateGroup.umo) == umo)
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = GateGroup(umo=umo, created_at=ts)
            row.allowed = bool(allowed)
            row.note = note or row.note
            row.source = source
            row.updated_at = ts
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def list_groups(self) -> list[GateGroup]:
        """List every group row.

        Returns:
            All rows ordered by the most recent update.
        """
        async with self.db.get_db() as session:
            rows = (await session.execute(select(GateGroup))).scalars().all()
        return sorted(rows, key=lambda row: row.updated_at, reverse=True)

    async def allowed_umos(self) -> set[str]:
        """Return the set of allowed group origins.

        Returns:
            A set of unified message origins.
        """
        async with self.db.get_db() as session:
            rows = (
                (
                    await session.execute(
                        select(GateGroup).where(col(GateGroup.allowed) == True)  # noqa: E712
                    )
                )
                .scalars()
                .all()
            )
        return {row.umo for row in rows}

    async def delete_group(self, umo: str) -> int:
        """Delete one group row.

        Args:
            umo: Unified message origin.

        Returns:
            Number of deleted rows.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(GateGroup).where(col(GateGroup.umo) == umo)
            )
            await session.commit()
            return int(result.rowcount or 0)

    # ------------------------------------------------------------------
    # Registration codes
    # ------------------------------------------------------------------
    async def create_code(
        self, code: str, note: str = "", max_uses: int = 1
    ) -> GateCode:
        """Create (or refresh) a registration code.

        Args:
            code: The literal code text.
            note: Free-form note.
            max_uses: How many groups may redeem it (0 = unlimited).

        Returns:
            The persisted row.
        """
        async with self.db.get_db() as session:
            row = (
                (
                    await session.execute(
                        select(GateCode).where(col(GateCode.code) == code)
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = GateCode(code=code, created_at=now_ts())
            row.note = note
            row.max_uses = int(max_uses)
            row.enabled = True
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def list_codes(self) -> list[GateCode]:
        """List every registration code.

        Returns:
            All codes ordered by creation time (newest first).
        """
        async with self.db.get_db() as session:
            rows = (await session.execute(select(GateCode))).scalars().all()
        return sorted(rows, key=lambda row: row.created_at, reverse=True)

    async def delete_code(self, code: str) -> int:
        """Delete one registration code.

        Args:
            code: The code text.

        Returns:
            Number of deleted rows.
        """
        async with self.db.get_db() as session:
            result = await session.execute(
                delete(GateCode).where(col(GateCode.code) == code)
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def consume_code(self, code: str, umo: str) -> bool:
        """Redeem a registration code for a group.

        Args:
            code: The code text sent by the user.
            umo: Unified message origin of the redeeming group.

        Returns:
            True when the code was valid and got consumed.
        """
        if not code:
            return False
        async with self.db.get_db() as session:
            row = (
                (
                    await session.execute(
                        select(GateCode).where(col(GateCode.code) == code)
                    )
                )
                .scalars()
                .first()
            )
            if row is None or not row.enabled:
                return False
            if row.max_uses > 0 and row.uses >= row.max_uses:
                return False
            row.uses += 1
            row.used_at = now_ts()
            row.used_by = umo
            if row.max_uses > 0 and row.uses >= row.max_uses:
                row.enabled = False
            session.add(row)
            await session.commit()
            return True

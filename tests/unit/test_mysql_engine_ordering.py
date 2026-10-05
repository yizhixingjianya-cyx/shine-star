"""Prove the MySQLDatabase engine-ordering defect.

MySQLDatabase.__init__ calls SQLiteDatabase.__init__ (which calls
BaseDatabase.__init__ -> create_async_engine(self.DATABASE_URL)) BEFORE assigning
the real MySQL URL. BaseDatabase therefore builds an engine for the *SQLite* URL
(sqlite+aiosqlite:///<empty path>); _rebuild_engine() then replaces it with the
MySQL engine, orphaning the first one.

Invariants asserted:
  1. every engine created during construction points at the MySQL URL
     (a sqlite:/// engine means a wasted, undisposed engine);
  2. AsyncSessionLocal is bound to the live engine.
"""

from sqlalchemy.ext.asyncio import create_async_engine as real_create_async_engine

MYSQL_URL = "mysql+aiomysql://u:p@127.0.0.1:3306/astrbot?charset=utf8mb4"


def _spy(created: list[str]):
    def spy(url, *args, **kwargs):
        created.append(str(url))
        return real_create_async_engine(url, *args, **kwargs)

    return spy


def test_no_throwaway_sqlite_engine_created():
    """Constructing a MySQLDatabase must not build a SQLite engine."""
    import astrbot.core.db as db_pkg
    import astrbot.core.db.mysql as mysql_mod

    created: list[str] = []
    spy = _spy(created)

    # Patch the two lookup sites actually used:
    #  - BaseDatabase.__init__ uses the module-global in astrbot.core.db
    #  - _rebuild_engine re-imports from sqlalchemy.ext.asyncio on each call
    orig_reexport = db_pkg.create_async_engine
    db_pkg.create_async_engine = spy

    import sqlalchemy.ext.asyncio as sa_asyncio

    orig_sa = sa_asyncio.create_async_engine
    sa_asyncio.create_async_engine = spy
    try:
        db = mysql_mod.MySQLDatabase(MYSQL_URL)
    finally:
        db_pkg.create_async_engine = orig_reexport
        sa_asyncio.create_async_engine = orig_sa

    print("engines created:", created)
    print("engine in use  :", db.engine.url)

    sqlite_engines = [u for u in created if u.startswith("sqlite")]
    assert not sqlite_engines, (
        f"a throwaway SQLite engine was built and discarded: {sqlite_engines}"
    )


def test_session_factory_bound_to_live_engine():
    """AsyncSessionLocal must be bound to the MySQL engine actually in use."""
    import astrbot.core.db.mysql as mysql_mod

    db = mysql_mod.MySQLDatabase(MYSQL_URL)

    bound = db.AsyncSessionLocal.kw["bind"]
    assert bound is db.engine, f"session factory bound to a different engine: {bound!r}"
    assert "mysql" in str(bound.url), f"session factory bound to {bound.url}"

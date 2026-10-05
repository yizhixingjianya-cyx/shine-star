"""Verify MySQLDatabase rebinds its engine when the event loop changes.

aiomysql binds pooled connections to the loop that created them. If the engine
is created on loop A and a query runs on loop B, the driver raises
"attached to a different loop" and the connection is never returned to the pool;
the pool then exhausts and every later call blocks forever.

``get_db`` must therefore rebuild the engine when the running loop changes.
"""

import asyncio
from unittest.mock import patch

MYSQL_URL = "mysql+aiomysql://u:p@127.0.0.1:3306/astrbot?charset=utf8mb4"


def _new_db():
    import astrbot.core.db.mysql as mysql_mod

    return mysql_mod.MySQLDatabase(MYSQL_URL)


def _noop_initialize():
    async def _init():
        return None

    return _init


async def _use_once(db):
    """Enter and leave get_db() once, with initialize() stubbed out."""
    with patch.object(db, "initialize", _noop_initialize()):
        async with db.get_db():
            pass


def test_engine_rebuilt_on_loop_change():
    """A call from a new loop must rebuild the engine (new identity)."""
    db = _new_db()
    engines = []

    async def use():
        await _use_once(db)
        engines.append(id(db.engine))

    # Loop 1
    asyncio.run(use())
    # Loop 2 - a fresh asyncio.run() creates a brand new loop
    asyncio.run(use())

    assert len(engines) == 2
    assert engines[0] != engines[1], (
        "engine was NOT rebuilt when the event loop changed; aiomysql would "
        "raise 'attached to a different loop' and leak the connection"
    )


def test_engine_bound_to_first_loop():
    """The engine records the loop it was created on."""
    db = _new_db()

    async def use():
        await _use_once(db)

    asyncio.run(use())
    assert db._current_loop is not None, "engine did not record its creating loop"


def test_same_loop_does_not_rebuild():
    """Repeated calls on one loop must reuse the same engine."""
    db = _new_db()
    engines = []

    async def main():
        for _ in range(3):
            await _use_once(db)
            engines.append(id(db.engine))

    asyncio.run(main())
    assert len(set(engines)) == 1, f"engine was rebuilt on the same loop: {engines}"


def test_rebuild_disposes_previous_engine():
    """Rebuilding must dispose the old engine instead of leaking its pool."""
    db = _new_db()
    old_engine = db.engine
    disposed = []

    real_dispose = old_engine.sync_engine.dispose

    def spy_dispose(*args, **kwargs):
        disposed.append(True)
        return real_dispose(*args, **kwargs)

    with patch.object(old_engine.sync_engine, "dispose", spy_dispose):
        db._rebuild_engine()

    assert disposed, "old engine pool was not disposed on rebuild"
    assert db.engine is not old_engine


def test_get_db_initializes_once_per_loop():
    """initialize() must run once per loop, not on every get_db()."""
    db = _new_db()
    calls = []

    async def counting_init():
        calls.append(1)

    async def main():
        with patch.object(db, "initialize", counting_init):
            for _ in range(3):
                async with db.get_db():
                    pass

    asyncio.run(main())
    assert len(calls) == 1, f"initialize() called {len(calls)} times on one loop"


def test_no_throwaway_sqlite_engine():
    """Constructing a MySQLDatabase must build only a MySQL engine."""
    import sqlalchemy.ext.asyncio as sa_asyncio
    from sqlalchemy.ext.asyncio import create_async_engine as real_create

    import astrbot.core.db as db_pkg

    created = []

    def spy(url, *args, **kwargs):
        created.append(str(url))
        return real_create(url, *args, **kwargs)

    orig_reexport = db_pkg.create_async_engine
    orig_sa = sa_asyncio.create_async_engine
    db_pkg.create_async_engine = spy
    sa_asyncio.create_async_engine = spy
    try:
        db = _new_db()
    finally:
        db_pkg.create_async_engine = orig_reexport
        sa_asyncio.create_async_engine = orig_sa

    assert created, "no engine was created at all"
    assert all(u.startswith("mysql") for u in created), (
        f"a non-MySQL engine was created: {created}"
    )
    assert "mysql" in str(db.engine.url)

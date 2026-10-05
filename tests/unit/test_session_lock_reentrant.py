"""Adversarial tests for the reentrant session lock (commit 2881dccf9).

The reentrant path added in `fix: release session locks ...` bypasses
asyncio.Lock entirely:

    if reentrant:
        try:
            yield
        finally:
            async with self._access_lock:
                self._lock_count[session_id] -= 1
                if self._lock_count[session_id] == 0:
                    self._locks.pop(session_id, None)   # <-- may evict live lock
        return

Concerns under test:
  A) eviction while an outer (non-reentrant) holder still holds the lock;
  B) a *different* task must never be treated as reentrant;
  C) mutual exclusion must still hold for unrelated tasks.
"""

import asyncio

import pytest

from astrbot.core.utils.session_lock import SessionLockManager


@pytest.mark.asyncio
async def test_different_task_is_not_reentrant():
    """A distinct task must block, not be let through as reentrant."""
    manager = SessionLockManager()
    sid = "s"
    order = []

    async def holder():
        async with manager.acquire_lock(sid):
            order.append("holder-in")
            await asyncio.sleep(0.1)
            order.append("holder-out")

    async def other():
        await asyncio.sleep(0.02)
        order.append("other-try")
        async with manager.acquire_lock(sid):
            order.append("other-in")

    await asyncio.gather(holder(), other())
    assert order.index("holder-out") < order.index("other-in"), order


@pytest.mark.asyncio
async def test_reentrant_same_task_then_waiter_still_excluded():
    """Inner reentrant exit must not let a queued task in early.

    Sequence:
      holder takes lock, then re-enters (reentrant), then a distinct waiter
      arrives and must wait until the *outermost* release.
    """
    manager = SessionLockManager()
    sid = "s"
    events = []

    async def holder():
        async with manager.acquire_lock(sid):
            events.append("outer-in")
            async with manager.acquire_lock(sid):  # reentrant
                events.append("inner-in")
                await asyncio.sleep(0.05)
                events.append("inner-out")
            events.append("after-inner")
            await asyncio.sleep(0.15)
            events.append("outer-out")

    async def waiter():
        await asyncio.sleep(0.08)  # arrives while outer still holds
        events.append("waiter-try")
        async with manager.acquire_lock(sid):
            events.append("waiter-in")

    await asyncio.gather(holder(), waiter())
    assert events.index("outer-out") < events.index("waiter-in"), events
    assert events.index("inner-out") < events.index("waiter-in"), events


@pytest.mark.asyncio
async def test_lock_object_not_evicted_while_outer_holds():
    """_locks[sid] must keep the same Lock object for the whole outer hold."""
    manager = SessionLockManager()
    per_loop = manager._get_loop_manager()
    sid = "s"
    seen = []

    async def holder():
        async with manager.acquire_lock(sid):
            async with per_loop._access_lock:
                seen.append(("outer", id(per_loop._locks[sid])))
            async with manager.acquire_lock(sid):  # reentrant
                async with per_loop._access_lock:
                    seen.append(("inner", id(per_loop._locks[sid])))
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.05)
            async with per_loop._access_lock:
                # If the lock was evicted, this is a brand-new object.
                seen.append(("after-inner", id(per_loop._locks[sid])))

    await holder()

    ids = {i for _, i in seen}
    assert len(ids) == 1, f"lock object changed during a single hold: {seen}"


@pytest.mark.asyncio
async def test_counters_balanced_after_reentrant_use():
    """Refcount must return to zero and leave no residue for the session."""
    manager = SessionLockManager()
    per_loop = manager._get_loop_manager()
    sid = "s"

    async with manager.acquire_lock(sid):
        async with manager.acquire_lock(sid):
            pass

    assert sid not in per_loop._locks, per_loop._locks
    assert sid not in per_loop._lock_count, per_loop._lock_count
    assert sid not in per_loop._holder_task, per_loop._holder_task


@pytest.mark.asyncio
async def test_holder_task_map_cleared_on_exception():
    """_holder_task must not leak when the body raises."""
    manager = SessionLockManager()
    per_loop = manager._get_loop_manager()
    sid = "s"

    with pytest.raises(ValueError):
        async with manager.acquire_lock(sid):
            raise ValueError("boom")

    assert sid not in per_loop._holder_task, per_loop._holder_task
    assert sid not in per_loop._locks, per_loop._locks


@pytest.mark.asyncio
async def test_mutual_exclusion_under_mixed_reentrant_load():
    """At most one task inside the session critical section at any time."""
    manager = SessionLockManager()
    sid = "s"
    inside = 0
    max_inside = 0

    async def worker(depth: int):
        nonlocal inside, max_inside
        async with manager.acquire_lock(sid):
            inside += 1
            max_inside = max(max_inside, inside)
            if depth:
                # reentrant on purpose
                async with manager.acquire_lock(sid):
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.01)
            inside -= 1

    await asyncio.gather(*[worker(i % 2) for i in range(8)])
    assert max_inside == 1, f"mutual exclusion violated: {max_inside}"

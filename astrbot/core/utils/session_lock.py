import asyncio
import threading
import weakref
from collections import defaultdict
from contextlib import asynccontextmanager


class _PerLoopSessionLockManager:
    """Per-event-loop session lock manager.

    The lock is reentrant per holding task. If the task that already holds a
    session lock acquires it again (for example a plugin hook fired inside the
    same conversation turn re-enters the session), it is let through instead of
    self-deadlocking on the non-reentrant asyncio.Lock.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._lock_count: dict[str, int] = defaultdict(int)
        self._access_lock = asyncio.Lock()
        self._holder_task: dict[str, asyncio.Task] = {}

    @asynccontextmanager
    async def acquire_lock(self, session_id: str):
        current = asyncio.current_task()
        async with self._access_lock:
            lock = self._locks[session_id]
            self._lock_count[session_id] += 1
            reentrant = (
                current is not None and self._holder_task.get(session_id) is current
            )

        if reentrant:
            # Same task already holds the lock: yield directly. Acquiring the
            # non-reentrant asyncio.Lock again here would deadlock forever and
            # the lock would never be released.
            try:
                yield
            finally:
                async with self._access_lock:
                    self._lock_count[session_id] -= 1
                    if self._lock_count[session_id] == 0:
                        self._locks.pop(session_id, None)
                        self._lock_count.pop(session_id, None)
            return

        try:
            async with lock:
                self._holder_task[session_id] = current
                try:
                    yield
                finally:
                    if self._holder_task.get(session_id) is current:
                        self._holder_task.pop(session_id, None)
        finally:
            async with self._access_lock:
                self._lock_count[session_id] -= 1
                if self._lock_count[session_id] == 0:
                    self._locks.pop(session_id, None)
                    self._lock_count.pop(session_id, None)


class SessionLockManager:
    """Thread-safe session lock manager with per-event-loop isolation."""

    def __init__(self) -> None:
        self._state_guard = threading.Lock()
        self._loop_managers: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, _PerLoopSessionLockManager
        ] = weakref.WeakKeyDictionary()

    def _get_loop_manager(self) -> _PerLoopSessionLockManager:
        """Get the lock manager for the current event loop."""
        loop = asyncio.get_running_loop()
        with self._state_guard:
            return self._loop_managers.setdefault(loop, _PerLoopSessionLockManager())

    @asynccontextmanager
    async def acquire_lock(self, session_id: str):
        manager = self._get_loop_manager()
        async with manager.acquire_lock(session_id):
            yield


session_lock_manager = SessionLockManager()

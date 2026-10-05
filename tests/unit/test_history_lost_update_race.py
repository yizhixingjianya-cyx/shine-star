"""Prove the lost-update race introduced by narrowing the session lock (b5f1673d).

Commit b5f1673d moved the session lock from "whole LLM request" to "build phase"
plus "save phase", releasing it across the LLM call:

    async with session_lock_manager.acquire_lock(umo):     # build only
        ... read history ...
    # lock RELEASED here
    ... await run_agent(...) ...                           # LLM call, seconds
    async with session_lock_manager.acquire_lock(umo):     # save only
        await self._save_to_history(...)

`_save_to_history` ends in:

    await self.conv_manager.update_conversation(
        umo, req.conversation.cid, history=message_to_save, ...
    )

and the DB layer implements that as a BLIND FULL-COLUMN UPDATE
(sqlite.py update_conversation -> values["content"] = content).

So two turns in the same session that both snapshot history during their build
phase will each write back their own snapshot plus their own turn. The second
write discards the first turn's messages.

This test models the real control flow and the real DB write semantics.
"""

import asyncio

import pytest

from astrbot.core.utils.session_lock import SessionLockManager


class FakeConversationStore:
    """Mirrors sqlite.py update_conversation: history is REPLACED wholesale."""

    def __init__(self):
        self.history = []
        self.writes = 0

    async def read(self):
        # Return a copy, as the real code deserializes JSON into a new list.
        return [dict(m) for m in self.history]

    async def write(self, history):
        self.writes += 1
        # Blind overwrite - exactly what update(ConversationV2).values(content=...)
        # does. No merge, no version check.
        self.history = [dict(m) for m in history]


async def _turn(
    store: FakeConversationStore,
    locks: SessionLockManager,
    umo: str,
    user_msg: str,
    reply: str,
    llm_delay: float,
    rebase: bool = True,
):
    """One conversation turn: build (locked) -> LLM (unlocked) -> save (locked).

    With ``rebase=True`` the save phase mirrors the hardened ``_save_to_history``:
    it re-reads the row under the lock and appends only the messages this turn
    produced, so concurrent turns compose instead of overwriting.

    With ``rebase=False`` it mirrors the pre-fix blind overwrite, which is the
    behaviour that loses a turn.
    """

    # --- build phase: locked. Snapshot the history. ---
    async with locks.acquire_lock(umo):
        snapshot = await store.read()

    # --- LLM call: UNLOCKED (this is what b5f1673d introduced). ---
    await asyncio.sleep(llm_delay)

    # --- save phase: locked ---
    async with locks.acquire_lock(umo):
        appended = [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": reply},
        ]
        if not rebase:
            await store.write(snapshot + appended)
            return
        current = await store.read()
        if current[: len(snapshot)] == snapshot:
            await store.write(current + appended)
        else:
            # Incompatible rewrite: keep stored history, append this turn.
            await store.write(current + appended)


@pytest.mark.asyncio
async def test_blind_overwrite_loses_a_turn():
    """Documents the bug: the pre-fix blind overwrite loses a concurrent turn.

    This is the behaviour of the narrowed-lock design WITHOUT the rebase fix. It
    is asserted explicitly so the reason the fix exists stays recorded.
    """
    store = FakeConversationStore()
    locks = SessionLockManager()
    umo = "platform:group:123"

    await asyncio.gather(
        _turn(store, locks, umo, "msg-A", "reply-A", llm_delay=0.10, rebase=False),
        _turn(store, locks, umo, "msg-B", "reply-B", llm_delay=0.02, rebase=False),
    )

    contents = [m["content"] for m in store.history]
    # The stale write clobbers the other turn's messages.
    assert not (
        {"msg-A", "reply-A"} <= set(contents) and {"msg-B", "reply-B"} <= set(contents)
    ), f"expected the blind overwrite to lose a turn, but nothing was lost: {contents}"


@pytest.mark.asyncio
async def test_concurrent_turns_in_same_session_lose_a_turn():
    """Two overlapping turns must BOTH survive.

    Expected (correct): history contains user-A, assistant-A, user-B, assistant-B.
    Actual without the rebase fix: whichever saves second overwrites the other.
    """
    store = FakeConversationStore()
    locks = SessionLockManager()
    umo = "platform:group:123"

    # Turn B starts while turn A is still inside its (unlocked) LLM call.
    await asyncio.gather(
        _turn(store, locks, umo, "msg-A", "reply-A", llm_delay=0.10),
        _turn(store, locks, umo, "msg-B", "reply-B", llm_delay=0.02),
    )

    contents = [m["content"] for m in store.history]

    assert "msg-A" in contents, f"turn A's user message was lost: {contents}"
    assert "reply-A" in contents, f"turn A's reply was lost: {contents}"
    assert "msg-B" in contents, f"turn B's user message was lost: {contents}"
    assert "reply-B" in contents, f"turn B's reply was lost: {contents}"


@pytest.mark.asyncio
async def test_sequential_turns_are_fine():
    """Sanity check: no overlap => no loss (isolates the race to concurrency)."""
    store = FakeConversationStore()
    locks = SessionLockManager()
    umo = "platform:group:123"

    await _turn(store, locks, umo, "msg-A", "reply-A", llm_delay=0.0)
    await _turn(store, locks, umo, "msg-B", "reply-B", llm_delay=0.0)

    contents = [m["content"] for m in store.history]
    assert contents == ["msg-A", "reply-A", "msg-B", "reply-B"], contents


@pytest.mark.asyncio
async def test_lock_held_for_whole_turn_prevents_loss():
    """Demonstrates the fix: holding the lock across the LLM call is lossless.

    This is the pre-b5f1673d behaviour, shown here as the control that proves the
    narrowed scope is what causes the loss.
    """
    store = FakeConversationStore()
    locks = SessionLockManager()
    umo = "platform:group:123"

    async def turn(user_msg, reply, llm_delay, acquired_event):
        async with locks.acquire_lock(umo):  # held across the whole turn
            snapshot = await store.read()
            await asyncio.sleep(llm_delay)
            await store.write(
                snapshot
                + [
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": reply},
                ]
            )

    await asyncio.gather(
        turn("msg-A", "reply-A", 0.05, None),
        turn("msg-B", "reply-B", 0.01, None),
    )

    contents = [m["content"] for m in store.history]
    assert contents == ["msg-A", "reply-A", "msg-B", "reply-B"], contents

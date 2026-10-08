"""Verify _merge_history_for_write prevents lost updates on conversation history.

`update_conversation` replaces the stored history wholesale
(sqlite.py: `values["content"] = content`). A turn that snapshots history during
its build phase and writes it back after an LLM call can therefore erase turns
that landed meanwhile.

`_merge_history_for_write` re-reads the row under the caller's lock and rebases
this turn's appended messages onto the current stored history.

These tests drive the REAL method, not a model of it.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
    InternalAgentSubStage,
)


def make_stage(stored_history):
    """Build a stage whose conversation manager returns `stored_history`."""
    conv_mgr = SimpleNamespace(
        get_conversation=AsyncMock(
            return_value=SimpleNamespace(history=json.dumps(stored_history))
        )
    )
    stage = InternalAgentSubStage.__new__(InternalAgentSubStage)
    stage.conv_manager = conv_mgr
    return stage


def msg(role, content):
    return {"role": role, "content": content}


@pytest.mark.asyncio
async def test_unchanged_history_writes_turn_snapshot():
    """When nobody else wrote, the turn's own snapshot is used as-is."""
    base = [msg("user", "a")]
    wanted = base + [msg("assistant", "reply-a")]
    stage = make_stage(list(base))  # stored == base

    out = await stage._merge_history_for_write("umo", "cid", base, wanted)
    assert out == wanted


@pytest.mark.asyncio
async def test_concurrent_turn_is_not_erased():
    """THE RACE: another turn appended first; it must survive.

    base      [a]
    stored    [a, b, reply-b]      <- turn B saved while turn A was in the LLM
    wanted    [a, reply-a]         <- turn A's stale snapshot
    expected  [a, b, reply-b, reply-a]  (B preserved, A appended)
    """
    base = [msg("user", "a")]
    stored = [msg("user", "a"), msg("user", "b"), msg("assistant", "reply-b")]
    wanted = base + [msg("assistant", "reply-a")]
    stage = make_stage(stored)

    out = await stage._merge_history_for_write("umo", "cid", base, wanted)

    contents = [m["content"] for m in out]
    assert "b" in contents, f"turn B's message was erased: {contents}"
    assert "reply-b" in contents, f"turn B's reply was erased: {contents}"
    assert "reply-a" in contents, f"turn A's reply was lost: {contents}"
    assert contents == ["a", "b", "reply-b", "reply-a"], contents


@pytest.mark.asyncio
async def test_no_duplication_when_history_unchanged():
    """Rebase must not duplicate messages when nothing changed."""
    base = [msg("user", "a")]
    wanted = base + [msg("assistant", "r")]
    stage = make_stage(list(base))
    out = await stage._merge_history_for_write("umo", "cid", base, wanted)
    assert out == wanted
    assert [m["content"] for m in out].count("a") == 1


@pytest.mark.asyncio
async def test_incompatible_rewrite_preserves_stored_history():
    """A reset/edit that this turn cannot reconcile must not be clobbered."""
    base = [msg("user", "a"), msg("assistant", "b")]
    stored = [msg("user", "unrelated")]  # rewritten out from under the turn
    wanted = base + [msg("assistant", "reply")]
    stage = make_stage(stored)

    out = await stage._merge_history_for_write("umo", "cid", base, wanted)

    contents = [m["content"] for m in out]
    assert "unrelated" in contents, f"stored history was clobbered: {contents}"
    assert "reply" in contents, f"this turn's output was dropped: {contents}"


@pytest.mark.asyncio
async def test_read_failure_falls_back_to_turn_snapshot():
    """If the re-read fails, fall back rather than crash the turn."""
    conv_mgr = SimpleNamespace(
        get_conversation=AsyncMock(side_effect=RuntimeError("db"))
    )
    stage = InternalAgentSubStage.__new__(InternalAgentSubStage)
    stage.conv_manager = conv_mgr

    base = [msg("user", "a")]
    wanted = base + [msg("assistant", "r")]
    out = await stage._merge_history_for_write("umo", "cid", base, wanted)
    assert out == wanted


@pytest.mark.asyncio
async def test_missing_conversation_falls_back():
    """A deleted conversation must not raise."""
    conv_mgr = SimpleNamespace(get_conversation=AsyncMock(return_value=None))
    stage = InternalAgentSubStage.__new__(InternalAgentSubStage)
    stage.conv_manager = conv_mgr

    base = [msg("user", "a")]
    wanted = base + [msg("assistant", "r")]
    out = await stage._merge_history_for_write("umo", "cid", base, wanted)
    assert out == wanted


@pytest.mark.asyncio
async def test_checkpoint_message_is_preserved():
    """A checkpoint segment appended by the caller must survive the rebase."""
    base = [msg("user", "a")]
    stored = base + [msg("user", "b")]
    checkpoint = {"type": "checkpoint", "content": {"id": "ck-1"}}
    wanted = base + [checkpoint]
    stage = make_stage(stored)

    out = await stage._merge_history_for_write("umo", "cid", base, wanted)

    assert checkpoint in out, out
    assert "b" in [m.get("content") for m in out if isinstance(m.get("content"), str)]

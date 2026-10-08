"""Verify _rebase_folded_prefix lets compression survive concurrent turns.

Compression snapshots the turns to fold (`old`), then summarizes with the session
lock RELEASED, then writes back. Turns that arrive during the LLM call make the
old CAS check fail, which previously discarded the computed summary and silently
skipped compression.

``_rebase_folded_prefix`` locates the still-present folded prefix and removes just
that run, so newer turns survive and the summary is not wasted.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

MEMORY_PATH = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "plugins"
    / "astrbot_plugin_memory_ctx"
    / "memory.py"
)


def load_engine_class():
    """Import MemoryEngine via a synthetic package (memory.py uses relative imports).

    The plugin directory has no ``__init__.py`` (it is loaded as a namespace
    package by AstrBot's plugin loader), so build the parent package explicitly.
    """
    import types

    plugin_dir = MEMORY_PATH.parent
    pkg_name = "astrbot_plugin_memory_ctx"
    if pkg_name not in sys.modules:
        package = types.ModuleType(pkg_name)
        package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
        package.__package__ = pkg_name
        sys.modules[pkg_name] = package

    mod_name = f"{pkg_name}.memory"
    if mod_name in sys.modules:
        return sys.modules[mod_name].MemoryEngine
    spec = importlib.util.spec_from_file_location(mod_name, MEMORY_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - environment-dependent
        sys.modules.pop(mod_name, None)
        pytest.skip(f"memory.py cannot be imported: {exc}")
    return module.MemoryEngine


@pytest.fixture(scope="module")
def engine():
    cls = load_engine_class()
    # Only the pure helper is exercised; no context/config/store needed.
    inst = cls.__new__(cls)
    return inst


def msg(role, content):
    return {"role": role, "content": content}


def test_prefix_at_offset_zero_still_works(engine):
    old = [msg("user", "a"), msg("assistant", "b")]
    cur = old + [msg("user", "c")]
    assert engine._rebase_folded_prefix(old, cur) == [msg("user", "c")]


def test_prefix_found_after_leading_turns(engine):
    """New turns landed BEFORE the folded prefix: prefix must be located."""
    old = [msg("user", "a"), msg("assistant", "b")]
    cur = [msg("user", "new"), msg("assistant", "r")] + old + [msg("user", "c")]
    out = engine._rebase_folded_prefix(old, cur)
    assert out is not None
    contents = [m["content"] for m in out]
    assert "new" in contents, f"newer turn was dropped: {contents}"
    assert "c" in contents, f"trailing turn was dropped: {contents}"
    assert "a" not in contents, f"folded prefix was not removed: {contents}"


def test_prefix_found_in_middle(engine):
    old = [msg("user", "a")]
    cur = [msg("user", "x"), msg("user", "a"), msg("user", "y")]
    out = engine._rebase_folded_prefix(old, cur)
    assert [m["content"] for m in out] == ["x", "y"]


def test_rewritten_history_returns_none(engine):
    """An irreconcilable rewrite must yield None so history is left alone."""
    old = [msg("user", "a"), msg("assistant", "b")]
    cur = [msg("user", "completely"), msg("assistant", "different")]
    assert engine._rebase_folded_prefix(old, cur) is None


def test_empty_old_returns_body(engine):
    cur = [msg("user", "a")]
    assert engine._rebase_folded_prefix([], cur) == cur


def test_current_shorter_than_old_returns_none(engine):
    old = [msg("user", "a"), msg("assistant", "b")]
    assert engine._rebase_folded_prefix(old, [msg("user", "a")]) is None


def test_no_duplication_of_surviving_turns(engine):
    """Surviving turns must appear exactly once."""
    old = [msg("user", "a")]
    cur = [msg("user", "a"), msg("user", "b"), msg("user", "c")]
    out = engine._rebase_folded_prefix(old, cur)
    contents = [m["content"] for m in out]
    assert contents == ["b", "c"], contents


def test_partial_match_is_not_accepted(engine):
    """A truncated/corrupted copy of the prefix must not be dropped."""
    old = [msg("user", "a"), msg("assistant", "b")]
    cur = [msg("user", "a"), msg("assistant", "b-mutated")]
    assert engine._rebase_folded_prefix(old, cur) is None

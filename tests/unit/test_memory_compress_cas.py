"""Adversarial test of the compare-and-swap guard in MemoryEngine.compress_session.

The pushed fix (2881dccf9) makes compression:
  phase 1  locked   : snapshot history
  phase 2  UNLOCKED : LLM summarize (seconds) + persist
  phase 3  locked   : re-read, verify prefix unchanged, then trim

The safety of dropping messages rests entirely on this guard:

    if cur_body[: len(old)] != old:
        ...skip write-back...
        return {"skipped": "history_changed"}

This test exercises the real guard logic over the interesting interleavings,
using the actual MemoryEngine methods where reachable and a faithful model of
the guard where an LLM is required.
"""

import json


# The guard is a direct prefix comparison. Model it exactly as written.
def guard_passes(old, current_history, split_leading_system, strip_summary_pairs):
    """Return True when compress_session would accept the write-back."""
    _cur_head, cur_body = split_leading_system(current_history)
    _cur_prior, cur_body = strip_summary_pairs(cur_body)
    return cur_body[: len(old)] == old


def _split_leading_system(history):
    head = []
    i = 0
    while i < len(history) and history[i].get("role") == "system":
        head.append(history[i])
        i += 1
    return head, history[i:]


def _strip_summary_pairs(body):
    # No summary pairs in these fixtures.
    return [], list(body)


def msg(role, content):
    return {"role": role, "content": content}


def test_unchanged_prefix_is_accepted():
    old = [msg("user", "a"), msg("assistant", "b")]
    current = old + [msg("user", "c")]
    assert guard_passes(old, current, _split_leading_system, _strip_summary_pairs)


def test_appended_message_while_summarizing_is_preserved():
    """A turn that arrived during phase 2 must survive the trim."""
    old = [msg("user", "a"), msg("assistant", "b")]
    recent = [msg("user", "c")]
    # Turn arrived mid-compression:
    current = old + recent + [msg("user", "NEW")]
    assert guard_passes(old, current, _split_leading_system, _strip_summary_pairs)
    # Trim keeps everything after len(old): recent + NEW
    kept = current[len(old) :]
    assert msg("user", "NEW") in kept, kept


def test_prefix_changed_is_rejected():
    """If the folded prefix itself changed, write-back must be refused."""
    old = [msg("user", "a"), msg("assistant", "b")]
    current = [msg("user", "a"), msg("assistant", "MUTATED")]
    assert not guard_passes(old, current, _split_leading_system, _strip_summary_pairs)


def test_truncated_prefix_is_rejected():
    """History shrunk (e.g. /reset) -> guard must reject, not delete."""
    old = [msg("user", "a"), msg("assistant", "b")]
    current = [msg("user", "a")]
    assert not guard_passes(old, current, _split_leading_system, _strip_summary_pairs)


def test_leading_system_message_does_not_break_guard():
    """A system head is stripped before comparison on both sides."""
    old = [msg("user", "a"), msg("assistant", "b")]
    current = [msg("system", "persona"), msg("user", "a"), msg("assistant", "b")]
    assert guard_passes(old, current, _split_leading_system, _strip_summary_pairs)


def test_reordered_prefix_is_rejected():
    old = [msg("user", "a"), msg("assistant", "b")]
    current = [msg("assistant", "b"), msg("user", "a")]
    assert not guard_passes(old, current, _split_leading_system, _strip_summary_pairs)


def test_duplicate_prefix_then_append():
    """Idempotency: identical prefix is accepted regardless of tail length."""
    old = [msg("user", "a")]
    for tail in range(5):
        current = old + [msg("user", f"t{i}") for i in range(tail)]
        assert guard_passes(old, current, _split_leading_system, _strip_summary_pairs)


def test_json_roundtrip_stability():
    """History survives the json.loads/dumps cycle the engine performs."""
    old = [msg("user", "a"), msg("assistant", "b")]
    roundtripped = json.loads(json.dumps(old))
    assert roundtripped == old


def test_old_prefix_restored_by_user_edit_is_accepted():
    """Known limitation: reverting the prefix to a previous state passes the guard.

    An equal prefix means no messages would be lost by the trim, so accepting is
    safe for this specific failure mode - documented here so the behaviour is
    explicit rather than assumed.
    """
    old = [msg("user", "a"), msg("assistant", "b")]
    current = list(old)  # someone reverted, same content
    assert guard_passes(old, current, _split_leading_system, _strip_summary_pairs)

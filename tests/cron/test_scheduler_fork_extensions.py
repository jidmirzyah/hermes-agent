"""Tests for cron/scheduler_fork_extensions.py -- fork-owned delivery behavior with no upstream
equivalent (FORKSTAKE, 2026-09-15). No existing test covered this behavior before this refactor;
written to lock in behavior-preservation for the extraction out of cron/scheduler.py."""

from cron.scheduler_fork_extensions import apply_fallback_delivery_tag


class _FakeAgent:
    def __init__(self, fallback_index, provider="anthropic", model="claude-sonnet-5"):
        self._fallback_index = fallback_index
        self.provider = provider
        self.model = model


def test_no_op_when_no_deferred_agents():
    assert apply_fallback_delivery_tag("hello", [], success=True) == "hello"


def test_no_op_when_run_failed_even_with_fallback():
    agent = _FakeAgent(fallback_index=1)
    assert apply_fallback_delivery_tag("hello", [agent], success=False) == "hello"


def test_no_op_when_fallback_index_is_zero_ie_primary_ran():
    agent = _FakeAgent(fallback_index=0)
    assert apply_fallback_delivery_tag("hello", [agent], success=True) == "hello"


def test_tags_when_fallback_actually_ran():
    agent = _FakeAgent(fallback_index=2, provider="openai", model="gpt-5.6")
    result = apply_fallback_delivery_tag("original response", [agent], success=True)
    assert result == (
        "[ran on fallback #2: gpt-5.6 via openai, not the configured primary]\n\n"
        "original response"
    )


def test_uses_only_the_first_deferred_agent():
    primary_shape = _FakeAgent(fallback_index=3, provider="p1", model="m1")
    later_agent = _FakeAgent(fallback_index=1, provider="p2", model="m2")
    result = apply_fallback_delivery_tag("x", [primary_shape, later_agent], success=True)
    assert "p1" in result and "m1" in result
    assert "p2" not in result


def test_missing_attrs_fall_back_to_unknown_without_raising():
    class _BareAgent:
        _fallback_index = 1

    result = apply_fallback_delivery_tag("x", [_BareAgent()], success=True)
    assert "unknown" in result

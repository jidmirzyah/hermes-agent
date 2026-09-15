"""Fork-owned cron-delivery behavior with no upstream equivalent (FORKSTAKE, 2026-09-15).

Deliberately small and separate from the upstream-authored `cron/scheduler_*.py` siblings
(`scheduler_delivery.py`, `scheduler_provider.py`, etc. -- all upstream-authored, per FORKSTAKE's own
investigation) so this fork's own delivery customizations have an obvious home instead of accreting
back into `cron/scheduler.py`'s hot job-execution path, which is what caused this file to be the
single most consistently-conflicting file in the fork's history. See
`Hermes/Execution Logs/Plans/2026-09-15 - FORKSTAKE ...` for the full investigation and design
rationale -- this module extends the fork's own already-proven-safe pattern (an explicit, inert-by-
default keyword parameter on a shared function) rather than a callback registry or mixin.
"""
from __future__ import annotations


def apply_fallback_delivery_tag(deliver_content: str, deferred_agents: list, *, success: bool) -> str:
    """Prefix `deliver_content` with a visible tag when the response actually came from a fallback
    provider, not the job's configured primary -- fallback quality varies, and a silent degrade on a
    judgment-critical job should be visible, not just usable. A no-op (returns `deliver_content`
    unchanged) unless the run both succeeded and actually used a fallback (`_fallback_index > 0`)."""
    if not (success and deferred_agents):
        return deliver_content
    fallback_agent = deferred_agents[0]
    fallback_index = int(getattr(fallback_agent, "_fallback_index", 0) or 0)
    if fallback_index <= 0:
        return deliver_content
    provider = str(getattr(fallback_agent, "provider", "") or "unknown")
    model = str(getattr(fallback_agent, "model", "") or "unknown")
    return (
        f"[ran on fallback #{fallback_index}: {model} via {provider}, "
        f"not the configured primary]\n\n{deliver_content}"
    )

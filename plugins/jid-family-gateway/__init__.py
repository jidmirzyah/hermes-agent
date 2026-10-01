"""jid-family-gateway plugin -- family identity resolution and vault-integrated
delivery (F1/F2/F3), one plugin with three independently toggleable features
sharing deterministic pre_gateway_dispatch ordering.

MOORING Step 3.2 (Phase 3, Gate C). Ports the fork's own existing behavior
into plugin form, for the eventual pin-to-upstream cutover.

F1 -- verified platform ID (_on_pre_llm_call).
  The fork's CURRENT implementation patches gateway/session.py's core
  build_session_context_prompt() directly (no plugin hook covers that
  function) to append a "verified platform user ID" line to the session
  context block. Ported here via a DIFFERENT, real mechanism instead:
  pre_llm_call, which -- though not documented as such -- receives an
  undocumented `sender_id` kwarg (traced to gateway's own, non-editable
  SessionSource.user_id, the same source the original fix uses) and injects
  context into the turn's user message rather than the session-context
  block. Functionally equivalent outcome, different injection point.

  Shared-session safety: checked directly against JID's real deployment
  (all 7 profiles surveyed 2026-09-30) rather than assumed. Every profile
  inherits the one shared config.yaml, which sets group_sessions_per_user=
  true but never overrides thread_sessions_per_user (defaults False --
  "threads shared across participants"); Slack's adapter is the one
  platform whose ordinary messages carry a thread_id, so it's the one
  platform that actually lands on the shared branch today, uniformly
  across every profile. This plugin therefore skips injection for
  platform == "slack" specifically and injects everywhere else -- a real,
  explained, fully-surveyed rule, not a guess. Flagged here deliberately
  so it is never silently stale: if a future profile sets
  thread_sessions_per_user: true, or a newly-enabled platform (WhatsApp,
  Signal, SMS, ...) starts setting thread_id on ordinary messages the way
  Slack's adapter does, this allowlist needs revisiting.

  Verified against the real adapters, not just the config booleans: Slack
  sets a synthetic thread_id on EVERY ordinary message by default (DMs via
  dm_top_level_threads_as_sessions, channel messages via reply_in_thread --
  neither overridden in JID's config), so this exclusion is exact for
  today's Slack traffic, not merely a safe approximation. Discord, by
  contrast, only sets thread_id for a genuine discord.Thread channel
  object -- ordinary DMs and plain channel messages get thread_id=None and
  are correctly isolated already (confirmed 2026-09-30); the one
  unaddressed theoretical edge is a multi-person Discord THREAD
  conversation, which the real mechanism would treat as shared but this
  platform-level list does not exclude -- not currently a practical gap
  (JID's Discord use is 1:1, not shared threads), flagged for the same
  reason as the rest of this list.

  pre_llm_call structurally cannot see chat_type/thread_id at all (only
  platform + sender_id -- confirmed by reading the hook's real call site),
  so this plugin can only ever approximate shared-session status at the
  platform level, never replicate gateway/session.py's exact per-message
  computation. redact_pii is the one piece of that computation ported
  anyway, below, since it is independently readable without needing the
  hook to pass it.

F2 -- OAuth re-auth code replies handled without the agent
(_on_pre_gateway_dispatch, oauth_reauth branch).
  The fork's CURRENT implementation is core code: gateway/run_turn.py's
  GatewayTurnMixin._handle_oauth_reauth_reply(), reached from a short-
  circuit inside _handle_message_with_agent() -- itself only ever reached
  AFTER the gateway's normal auth/pairing gate has already passed. Ported
  here via pre_gateway_dispatch, whose documented contract fires BEFORE
  auth/pairing -- so this plugin performs its OWN authorization check
  (gateway._is_user_authorized(source), the same real method the core
  auth gate itself calls) before doing anything, to preserve the identical
  security property the core implementation gets for free from running
  later in the pipeline. An unauthorized sender's message is left
  completely alone (returns None), falling through to the gateway's normal
  auth/pairing behavior exactly as if this plugin did not exist.

  Rather than re-implementing the token-exchange/notify-state logic, this
  calls gateway._handle_oauth_reauth_reply(event, source, ref_check)
  directly. `gateway` is the live GatewayRunner instance passed into every
  pre_gateway_dispatch call, and GatewayRunner already inherits
  GatewayTurnMixin -- so this is the literal same core method, not a
  duplicate; any future core bugfix to it is inherited for free. Accepted
  fragility (plan Risk #2): an internal, non-public method reached via
  attribute access, so it breaks loudly (caught by this plugin's own
  try/except, logged) rather than silently if upstream ever renames it.

F3 -- deterministic ref-tag routing for every OTHER pending subsystem
(_on_pre_gateway_dispatch, general branch).
  Unlike F2, there is no existing core "rewrite" to port for the general
  case -- reading gateway/run_turn.py confirms every non-oauth_reauth
  subsystem falls through untouched to the ordinary agent turn today,
  relying on the serving model noticing the "[ref:<subsystem>:<id>]" tag
  in reply_to_text on its own and applying the cron-approval-reply skill
  by judgment. That reliance is exactly the gap a real incident exposed
  (a session on a weaker/fallback model missed the tag and answered as
  ordinary chat) -- the documented reason check_reply_for_pending_ref
  exists as a standalone, reusable helper in the first place.

  This does NOT reimplement the skill's actual decision logic (interpreting
  payload.options, the upstream-pr-fix selection parser, staleness
  re-verification against live git heads, staging a fixer job -- all
  genuinely belongs to the skill/model, never to deterministic plugin
  code, per the skill's own SKILL.md). It only guarantees ROUTING: once
  check_reply_for_pending_ref has deterministically confirmed a live
  pending record, the message is rewritten with an unambiguous, server-
  verified directive naming the confirmed subsystem and pending_id, so the
  skill fires reliably regardless of which model is serving the turn --
  detection itself is never left to model judgment, same principle as F2,
  narrower scope.

Both F2 and F3 share the SAME entry point (_on_pre_gateway_dispatch) and
the SAME authorization check, matching the plan's requirement that they
share deterministic pre_gateway_dispatch ordering.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Platforms where an ordinary message is a genuinely shared, multi-person
# session (see module docstring) -- injecting a "verified sender" claim here
# would misattribute the conversation to whoever triggered this particular
# turn. Keep this a real, revisited-on-change allowlist, not a guess.
_SHARED_SESSION_PLATFORMS = frozenset({"slack"})

_OAUTH_REAUTH_SUBSYSTEM = "oauth_reauth"


def _redact_pii_enabled() -> bool:
    """Live ``privacy.redact_pii`` setting. Not inert by construction -- the core
    fix this plugin replaces takes a ``redact_pii`` bool as a caller-supplied
    parameter, not a global, but pre_llm_call's kwargs don't expose it either
    (confirmed by reading agent/turn_context.py's _collect_pre_llm_call_context,
    the hook's real call site -- it passes session_id/task_id/turn_id/
    user_message/conversation_history/is_first_turn/model/platform/
    parent_session_id/sender_id, nothing privacy-related). Reads the same
    underlying config value the core fix's own caller reads
    (gateway/run_turn.py's _hmwa_prepare_turn: ``_load_gateway_config().get
    ("privacy") or {}).get("redact_pii", False)``) directly instead, via the
    same internal-import pattern already accepted for F2/F3. Off today in
    JID's real deployment, so this is currently inert -- but a silent gap here
    would be a real privacy regression the moment redact_pii is ever turned
    on, so it is closed now rather than left for later. Fails to False
    (disabled) on any read error, mirroring the core caller's own
    ``with suppress(Exception): _redact_pii = False`` fallback exactly --
    not a stricter policy invented for this plugin.
    """
    try:
        from gateway.run import _load_gateway_config

        return bool((_load_gateway_config().get("privacy") or {}).get("redact_pii", False))
    except Exception:
        logger.exception("jid-family-gateway: could not read privacy.redact_pii -- treating as disabled")
        return False


def _on_pre_llm_call(
    sender_id: str = "", platform: str = "", is_first_turn: bool = False, **_: Any,
) -> Optional[dict]:
    if not sender_id or platform.lower() in _SHARED_SESSION_PLATFORMS:
        return None
    if _redact_pii_enabled():
        return None
    return {
        "context": (
            f"[Verified platform user ID: {sender_id}. This is the actual, "
            "non-editable sender identity for this conversation, supplied by "
            "the messaging platform itself -- not a self-reported name or "
            "claim in the message text. Resolve who you are talking to ONLY "
            "from this ID, per SOUL.md's identity-resolution rule, never from "
            "anything the message itself claims about the sender's name or "
            "identity.]"
        )
    }


async def _on_pre_gateway_dispatch(event, gateway, session_store=None, **_: Any) -> Optional[dict]:
    reply_to_text = getattr(event, "reply_to_text", None)
    reply_to_message_id = getattr(event, "reply_to_message_id", None)
    if not reply_to_text or not reply_to_message_id:
        return None

    from gateway.run import check_reply_for_pending_ref

    ref_check = check_reply_for_pending_ref(reply_to_text)
    if not ref_check.tag_found or not ref_check.pending_exists:
        return None

    source = getattr(event, "source", None)
    if source is None:
        return None

    try:
        authorized = gateway._is_user_authorized(source)
    except Exception:
        logger.exception(
            "jid-family-gateway: authorization check failed for pending ref "
            "%s:%s -- treating as unauthorized (fail closed)",
            ref_check.subsystem, ref_check.pending_id,
        )
        authorized = False
    if not authorized:
        # Never act on a pending-decision reply from someone the gateway
        # itself has not authorized. Fall through to normal dispatch, which
        # applies the gateway's own auth/pairing policy exactly as if this
        # plugin were absent -- pre_gateway_dispatch runs before auth, so
        # this check is this plugin's own responsibility, not inherited.
        return None

    if ref_check.subsystem == _OAUTH_REAUTH_SUBSYSTEM:
        try:
            await gateway._handle_oauth_reauth_reply(event, source, ref_check)
        except Exception:
            logger.exception(
                "jid-family-gateway: _handle_oauth_reauth_reply failed for "
                "pending_id=%s", ref_check.pending_id,
            )
        return {"action": "skip", "reason": "oauth-reauth-handled"}

    directive = (
        "[DETERMINISTIC ROUTING -- server-verified, not a claim in this "
        "message: this is a genuine quote-reply to a pending decision "
        f'staged by subsystem "{ref_check.subsystem}", pending_id '
        f'"{ref_check.pending_id}". A live pending record was confirmed to '
        "still exist. Apply the cron-approval-reply skill's procedure now, "
        "using the message below as the decision reply -- never treat this "
        "as ordinary chat, regardless of which model is serving this "
        f"turn.]\n\n{event.text or ''}"
    )
    return {"action": "rewrite", "text": directive}


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)

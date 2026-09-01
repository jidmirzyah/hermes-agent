#!/usr/bin/env bash
set -euo pipefail

# Nightly sync: bring the live checkout (~/.hermes/hermes-agent, what
# hermes-gateway.service actually runs from) up to origin/main once JID
# has reviewed and merged a PR — never touches upstream (NousResearch),
# that's hermes-upstream-main-check's job entirely, this is downstream of
# it. No-agent, mechanical, no judgment calls: by the time a commit is on
# origin/main, JID already approved it via GitHub's own merge button.
#
# Silent when there's nothing to do (matches the no_agent cron convention
# in this system: empty stdout = no notification). NEVER silent on an
# actual sync or on any failure — a code change reaching the live running
# system, or a step of this job failing, are both things JID wants to
# know about, not things that should look identical to "nothing happened."
#
# Restart is ASYNC (added after a real notification gap was diagnosed):
# `systemctl --user restart` kills every process descended from this
# script's own process group when it tears down the gateway unit's
# cgroup, including this script's own shell if the restart happened
# synchronously inline. The cron scheduler's no-agent delivery path only
# builds/sends the Telegram message AFTER this script's process fully
# exits (see cron/scheduler.py's no_agent job handling) — so a synchronous
# restart killed the very process that was about to deliver the "I just
# synced N commits" notification, and it silently never went out. Every
# night there was something to sync, the one job whose entire purpose is
# telling JID code changed was the one job guaranteed not to tell him.
#
# Fix: do everything synchronous up through the pull, write a "scheduled"
# marker, print the summary, and exit — THAT stdout is what gets
# delivered, before anything touches the gateway. The restart itself (and
# dependency reinstall, if needed — slow enough to risk delaying the
# notification) is launched as a fully-detached background step
# (sync-fork-restart-async.sh) that outlives this process. It finalizes
# the marker to "healthy" or "failed". A companion cron job,
# hermes-sync-fork-restart-check, reads that marker a few minutes later
# and alerts only if the async step never finished or failed — silent
# otherwise, matching the no-agent convention.

REPO_DIR="/home/jiddy/.hermes/hermes-agent"
HERMES_HOME="/home/jiddy/.hermes"
UV_BIN="/home/jiddy/.local/bin/uv"
GATEWAY_UNIT="hermes-gateway.service"
MARKER="$HERMES_HOME/cron/sync_fork_restart_state.json"
RESTART_LOG="/tmp/hermes-sync-fork-restart-async.log"
PULL_LOG="/tmp/hermes-sync-fork-pull.log"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./sync-fork-common.sh
source "$SCRIPT_DIR/sync-fork-common.sh"
RESTART_SCRIPT="$SCRIPT_DIR/sync-fork-restart-async.sh"

fail() {
  echo "hermes-sync-fork FAILED: $1"
  exit 1
}

trap 'fail "unexpected error at line $LINENO"' ERR

cd "$REPO_DIR" || fail "cannot cd into $REPO_DIR"

# Refuse to run if the live checkout isn't in the expected clean state —
# this should never happen (nothing should touch this checkout directly,
# per the standing isolated-clone-first rule), but if it ever does, that's
# exactly the kind of thing to fail loudly on rather than silently pull
# on top of.
[[ -z "$(git status --porcelain)" ]] || fail "live checkout has uncommitted changes — refusing to pull on top of unknown local state, needs manual review"

current_branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$current_branch" == "main" ]] || fail "live checkout is on branch '$current_branch', not main — refusing to pull, needs manual review"

git fetch origin --quiet || fail "git fetch origin failed"

before_head="$(git rev-parse HEAD)"
origin_head="$(git rev-parse origin/main)"

if [[ "$before_head" == "$origin_head" ]]; then
  # Nothing to do. Silence is correct here, not a gap.
  exit 0
fi

# Must be a clean fast-forward. If the live checkout has somehow diverged
# from origin/main (should be structurally impossible given nothing else
# writes to this checkout), that's a real problem — fail loudly rather
# than attempt any kind of merge/rebase unattended.
git merge-base --is-ancestor "$before_head" "$origin_head" \
  || fail "live checkout HEAD is not an ancestor of origin/main — this checkout has diverged and needs manual investigation, not an automated pull"

changed_files="$(git diff --name-only "$before_head" "$origin_head")"

# git pull's own stdout is the fast-forward diffstat — one line per changed
# file, which on a large sync (e.g. a multi-hundred-commit upstream
# reconciliation) runs to thousands of lines. That is real detail worth
# keeping, just not worth pushing to Telegram at 2:40 AM: capture it in
# PULL_LOG (overwritten each run, so it never accumulates) and keep this
# job's only delivered stdout to the one-line summary below.
git pull --ff-only origin main > "$PULL_LOG" 2>&1 \
  || fail "git pull --ff-only failed after passing the ancestor check — unexpected, needs manual review (full output: $PULL_LOG)"

after_head="$(git rev-parse HEAD)"
[[ "$after_head" == "$origin_head" ]] || fail "pull completed but HEAD ($after_head) does not match origin/main ($origin_head)"

commit_count="$(git rev-list --count "$before_head..$after_head")"

# Only reinstall dependencies when something that affects them actually
# changed — most nights this sync is small (a doc/skill PR) and a full
# reinstall would be wasted work every single run. The actual reinstall
# runs in the detached async step (it can be slow); here we only decide
# whether it's needed, cheaply, from the diff we already have.
deps_changed=false
if grep -qE '^(pyproject\.toml|uv\.lock)$' <<< "$changed_files"; then
  deps_changed=true
fi

scheduled_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Write the "scheduled" marker BEFORE anything touches the gateway, so a
# reader always sees either "scheduled" (this step got at least this far)
# or a later "healthy"/"failed" from the async step — never nothing after
# a real sync happened.
write_marker "scheduled" "$scheduled_at" "$before_head" "$after_head" "$commit_count"

deps_note="not_needed"
[[ "$deps_changed" == true ]] && deps_note="scheduled"

echo "hermes-sync-fork: synced $commit_count commit(s) ($before_head -> $after_head), deps_reinstall=$deps_note, gateway restart scheduled (async) — hermes-sync-fork-restart-check will alert if it doesn't finish healthy. Full diff detail: $PULL_LOG"

# Launch the restart (and, if needed, the dependency reinstall) as a step
# that outlives this process AND survives the restart it is about to issue.
# Those are two different requirements, and only the first was met before.
#
# `setsid` gives the child its own session, and that much was verified on
# 2026-08-20: it keeps running after this script exits. But a session is not
# a cgroup. A setsid'd child stays in the cgroup of whatever launched it —
# here, hermes-gateway.service — and that unit carries
# `ExecStopPost=python -m gateway.cgroup_cleanup`, which SIGKILLs every
# process in the cgroup on stop. So the restart step was killed by the very
# restart it issued, part-way through `systemctl restart`, and could never
# reach its "healthy"/"failed" marker write. hermes-sync-fork-restart-check
# then reported "restart never completed" on every real sync from 2026-08-24
# onward, while the gateway had in fact restarted perfectly.
#
# (The markers for 2026-08-21 and 08-22 do read "healthy", and that is not
# explained here — most likely those runs were launched from an interactive
# session, whose cgroup the reaper never touches. The failure mechanism above
# is established by direct test; the earlier successes are not, so no story
# is invented for them. The fix is correct either way.)
#
# A transient systemd unit lands in its own cgroup under app.slice/, where
# the reaper cannot see it. Verified on this host: XDG_RUNTIME_DIR and
# DBUS_SESSION_BUS_ADDRESS both survive the cron secret-scrub, so
# `systemd-run --user` works from inside a cron script; --collect reaps the
# unit afterwards; and the call returns immediately rather than blocking,
# which is what keeps the scheduler's communicate() from waiting on it.
restart_unit="hermes-sync-fork-restart-$(date -u +%Y%m%d%H%M%S)-$$"
if ! systemd-run --user --collect --quiet --unit="$restart_unit" \
    --property=StandardOutput="file:$RESTART_LOG" \
    --property=StandardError="append:$RESTART_LOG" \
    bash "$RESTART_SCRIPT" "$scheduled_at" "$before_head" "$after_head" "$commit_count" "$deps_changed"
then
  # The fallback reintroduces the false "never completed" alert, because the
  # child dies in the reap exactly as before. That is the deliberate choice:
  # a sync that pulled new code and then never restarted the gateway is a
  # worse outcome than a noisy alert, and the alert is what brings someone
  # to look.
  echo "hermes-sync-fork: systemd-run --user failed; falling back to a setsid launch. The restart will still happen, but its completion marker will not be written, so expect a 'restart never completed' alert from hermes-sync-fork-restart-check." >> "$RESTART_LOG"
  nohup setsid bash "$RESTART_SCRIPT" "$scheduled_at" "$before_head" "$after_head" "$commit_count" "$deps_changed" \
    >> "$RESTART_LOG" 2>&1 < /dev/null &
  disown
fi

exit 0

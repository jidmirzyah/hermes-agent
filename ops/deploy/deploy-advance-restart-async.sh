#!/usr/bin/env bash
set -uo pipefail
# Deliberately no `set -e` / ERR trap here: this process runs fully detached
# from deploy-advance.sh (its parent already exited and delivered its own
# notification by the time this runs, same reasoning as
# scripts/sync-fork-restart-async.sh, which this is derived from). Its own
# failures must be captured into the advance receipt so L3 drift detection
# can alert on them -- an uncaught crash into a detached log nobody reads
# promptly would reintroduce the exact silent gap that pattern exists to
# close (see sync-fork.sh's header for the original 2026-08-24 incident).
#
# Invoked by deploy-advance.sh as:
#   deploy-advance-restart-async.sh <scheduled_at> <before_head> <after_head> <commit_count> <deps_changed>
# fully detached (systemd-run --user, or a setsid fallback) so it outlives
# the parent script's process and survives the gateway restart it issues.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./deploy-advance-common.sh
source "$SCRIPT_DIR/deploy-advance-common.sh"

scheduled_at="$1"
before_head="$2"
after_head="$3"
commit_count="$4"
deps_changed="$5"

REPO_DIR="${DEPLOY_REPO_DIR:-/home/jiddy/.hermes/hermes-agent}"
HERMES_HOME="${DEPLOY_HERMES_HOME:-/home/jiddy/.hermes}"
UV_BIN="${DEPLOY_UV_BIN:-/home/jiddy/.local/bin/uv}"
GATEWAY_UNIT="${DEPLOY_GATEWAY_UNIT:-hermes-gateway.service}"
RECEIPT="$HERMES_HOME/cron/deploy_advance_state.json"
LOG_DIR="$HERMES_HOME/logs"
DEPS_LOG="$LOG_DIR/deploy-advance-deps-reinstall.log"
SYSTEMCTL_LOG="$LOG_DIR/deploy-advance-restart-systemctl.log"

mark_failed() {
  write_advance_receipt "failed" "$scheduled_at" "$before_head" "$after_head" "$commit_count" "$1"
  exit 1
}

mkdir -p "$LOG_DIR" || mark_failed "cannot create log directory $LOG_DIR"

# Give deploy-advance.sh's own exit and the scheduler's stdout capture +
# notification delivery a few seconds' head start before anything here
# touches the gateway (same reasoning as sync-fork-restart-async.sh).
sleep 5

if [[ "$deps_changed" == "true" ]]; then
  cd "$REPO_DIR" 2>/dev/null || mark_failed "cannot cd into $REPO_DIR for dependency reinstall"
  if ! "$UV_BIN" sync --frozen >"$DEPS_LOG" 2>&1; then
    mark_failed "dependency reinstall failed after advancing $commit_count commit(s) (before=$before_head after=$after_head) -- see $DEPS_LOG. Live checkout is on the new commits but the running gateway is still on old code; restart was not attempted, needs manual intervention"
  fi
fi

# Restart so the running gateway actually loads the new code. Going through
# systemctl directly (not `hermes gateway restart`, which refuses to run
# under the gateway's own _HERMES_GATEWAY=1 environment by design) is
# genuinely external to the gateway process, so it doesn't need to touch
# that self-restart-loop guard -- same reasoning as sync-fork.sh.
if ! systemctl --user restart "$GATEWAY_UNIT" >"$SYSTEMCTL_LOG" 2>&1; then
  mark_failed "gateway restart command failed after advancing $commit_count commit(s) -- see $SYSTEMCTL_LOG. Live checkout is updated on disk but the running process may still be on old code."
fi

# Give the new process a moment to actually come up before checking.
sleep 5

status_output="$(systemctl --user status "$GATEWAY_UNIT" 2>&1)" || mark_failed "gateway status command itself failed after restart -- cannot confirm health. Raw output: $status_output"

grep -q "active (running)" <<< "$status_output" \
  || mark_failed "gateway did not report 'active (running)' after restart. Status output: $status_output"

# Scoped to this exact restart's systemd invocation ID, not a plain time
# window -- see sync-fork-restart-async.sh's own comment on why a naive
# time-window scan can misattribute the dying OLD process's error tail to
# the new one during the handoff.
invocation_id="$(systemctl --user show "$GATEWAY_UNIT" --property=InvocationID --value)"
[[ -n "$invocation_id" ]] || mark_failed "could not read the new gateway process's systemd invocation ID -- cannot scope the post-restart log check"

recent_errors="$(journalctl --user -u "$GATEWAY_UNIT" "_SYSTEMD_INVOCATION_ID=$invocation_id" --no-pager 2>/dev/null | grep -iE 'traceback|error' || true)"
if [[ -n "$recent_errors" ]]; then
  mark_failed "gateway restarted and reports healthy, but errors appeared in its own log: $recent_errors"
fi

write_advance_receipt "healthy" "$scheduled_at" "$before_head" "$after_head" "$commit_count"
exit 0

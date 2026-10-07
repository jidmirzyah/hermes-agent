#!/usr/bin/env bash
set -euo pipefail

# deploy-advance.sh -- MOORING Step 1.3 (L1's advance mechanism, one of four
# layers replacing the fork's in-code self-update gate; see
# Hermes/Execution Logs/Plans/2026-09-29 - MOORING Pin to Reviewed Upstream,
# Customizations Into Plugins.md). Derived from scripts/sync-fork.sh, but:
#
#   * Targets origin/deploy, not origin/main -- the L1 deploy pointer JID
#     moves only by merging a PR, per Step 1.1's decision.
#   * Refuses to advance to anything not reachable from the declared approved
#     base (an upstream `stable` tag, per Step 1.1, plus any declared
#     residual patches -- see approved-base.txt) -- origin/deploy moving is
#     not itself proof the target is safe, only that JID approved this
#     specific history.
#   * Proves the target commit in a fresh, throwaway clone (ast.parse, an
#     import smoke test, `uv sync --frozen`, `hermes plugins list`, the
#     acceptance checklist's offline-runnable subset) BEFORE touching the
#     real live checkout at all -- sync-fork.sh has no such step because it
#     only ever moves the fork forward along review JID already gave via
#     GitHub's merge button; this script's whole point is to let that same
#     merge-button approval stand in for an upstream release, which is
#     approved on GENERAL PRINCIPLE, not on having been read commit-by-commit,
#     so it needs its own, mechanical proof step.
#   * Writes its own advance receipt (deploy_advance_state.json), separate
#     from sync-fork's own marker file, so the two mechanisms never read or
#     clobber each other's state -- hermes-sync-fork keeps running unmodified
#     alongside this one; it is not replaced in this phase.
#
# The restart step (deploy-advance-restart-async.sh) follows sync-fork's own
# async-restart pattern for the exact reason documented in sync-fork.sh's own
# header: `systemctl --user restart` tears down this script's whole cgroup,
# including a synchronously-run restart step's own process, so the restart
# (and any dependency reinstall) must be launched detached, via
# `systemd-run --user`, so it survives and can write its own completion
# marker after this process has already exited and (for a no_agent cron
# invocation) delivered its own notification.
#
# NOT built here: the one-time "switch" mode for the first move from fork
# history to upstream history, which is not a fast-forward. That is Step
# 6.2's own job, at actual cutover, and each run needs its own separate
# approval -- deliberately no flag for it exists in this script at all.
#
# All paths below are env-var overridable (defaults match the real live
# checkout) so this exact script can be pointed at a staging clone while
# being proved in Phase 1, and only pointed at the real live checkout at
# Phase 6 cutover, without needing a rewrite.

REPO_DIR="${DEPLOY_REPO_DIR:-/home/jiddy/.hermes/hermes-agent}"
HERMES_HOME="${DEPLOY_HERMES_HOME:-/home/jiddy/.hermes}"
UV_BIN="${DEPLOY_UV_BIN:-/home/jiddy/.local/bin/uv}"
GATEWAY_UNIT="${DEPLOY_GATEWAY_UNIT:-hermes-gateway.service}"
RECEIPT="$HERMES_HOME/cron/deploy_advance_state.json"
LOG_DIR="$HERMES_HOME/logs"
RESTART_LOG="$LOG_DIR/deploy-advance-restart-async.log"
PULL_LOG="$LOG_DIR/deploy-advance-pull.log"
PREFLIGHT_LOG="$LOG_DIR/deploy-advance-preflight.log"
PREFLIGHT_SCRATCH_BASE="${DEPLOY_PREFLIGHT_SCRATCH:-$HERMES_HOME/scratch}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./deploy-advance-common.sh
source "$SCRIPT_DIR/deploy-advance-common.sh"
RESTART_SCRIPT="$SCRIPT_DIR/deploy-advance-restart-async.sh"
APPROVED_BASE_FILE="${DEPLOY_APPROVED_BASE_FILE:-$SCRIPT_DIR/approved-base.txt}"

# The acceptance checklist's offline-runnable automated subset (Step 0.2 /
# the MOORING Acceptance Checklist doc's F1/F2/F3-auto, G3-auto, G1/G4-auto
# rows). Keep this list in sync with that doc. Overridable (space-separated)
# for testing against a lightweight fixture package instead of the real repo.
if [[ -n "${DEPLOY_OFFLINE_CHECKLIST_TESTS:-}" ]]; then
  read -ra OFFLINE_CHECKLIST_TESTS <<< "$DEPLOY_OFFLINE_CHECKLIST_TESTS"
else
  OFFLINE_CHECKLIST_TESTS=(
    "tests/gateway/test_session.py"
    "tests/gateway/test_oauth_reauth_no_agent.py"
    "tests/gateway/test_check_reply_for_pending_ref.py"
    "tests/gateway/test_unauthorized_dm_behavior.py"
    "tests/gateway/test_platform_authz_scope.py"
    "tests/tools/test_update_approval.py"
    "tests/tools/test_write_approval.py"
    "tests/tools/test_write_approval_fail_closed.py"
  )
fi

fail() {
  echo "deploy-advance FAILED: $1"
  exit 1
}

trap 'fail "unexpected error at line $LINENO"' ERR

mkdir -p "$LOG_DIR" || fail "cannot create log directory $LOG_DIR"

cd "$REPO_DIR" || fail "cannot cd into $REPO_DIR"

# Same "nothing should touch this checkout directly" invariant as
# sync-fork.sh: fail loudly rather than advance on top of unknown state.
[[ -z "$(git status --porcelain)" ]] || fail "live checkout has uncommitted changes -- refusing to advance on top of unknown local state, needs manual review"

current_branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$current_branch" == "deploy" ]] || fail "live checkout is on branch '$current_branch', not deploy -- refusing to advance, needs manual review (the one-time history-switch mode for the first fork->upstream move is Step 6.2's own job, not this script's)"

git fetch origin --quiet || fail "git fetch origin failed"

before_head="$(git rev-parse HEAD)"
target_head="$(git rev-parse origin/deploy 2>/dev/null)" || fail "origin/deploy does not exist -- nothing to advance to"

if [[ "$before_head" == "$target_head" ]]; then
  exit 0  # nothing to do; silence matches the no_agent job convention
fi

# Must be a clean fast-forward: the live checkout only ever moves forward
# along a line JID already approved by merging into origin/deploy.
git merge-base --is-ancestor "$before_head" "$target_head" \
  || fail "live checkout HEAD is not an ancestor of origin/deploy -- this checkout has diverged and needs manual investigation, not an automated advance"

# ---- Reachability: target must be the declared approved base, plus only
# declared residual-patch commits on top (approved-base.txt). ----
[[ -f "$APPROVED_BASE_FILE" ]] || fail "no approved-base file at $APPROVED_BASE_FILE -- refusing to advance with no declared reachability target"

mapfile -t base_lines < <(grep -vE '^[[:space:]]*(#|$)' "$APPROVED_BASE_FILE")
[[ "${#base_lines[@]}" -ge 1 ]] || fail "approved-base file $APPROVED_BASE_FILE has no declared base -- refusing to advance"

approved_base="${base_lines[0]}"
[[ "$approved_base" != "REPLACE_ME_WITH_APPROVED_BASE_TAG_OR_SHA" ]] \
  || fail "approved-base file $APPROVED_BASE_FILE still has its placeholder -- set the real approved upstream tag/SHA before this script can advance anything"

declared_patches=("${base_lines[@]:1}")

git fetch upstream --quiet --tags || fail "git fetch upstream failed"
approved_base_sha="$(git rev-parse "$approved_base" 2>/dev/null)" \
  || fail "approved base '$approved_base' (from $APPROVED_BASE_FILE) does not resolve to a real commit -- fetch upstream tags or check the SHA"

git merge-base --is-ancestor "$approved_base_sha" "$target_head" \
  || fail "origin/deploy ($target_head) is not a descendant of the approved base '$approved_base' ($approved_base_sha) -- refusing to advance to unreviewed history"

extra_commits="$(git rev-list "$approved_base_sha..$target_head")"
if [[ -n "$extra_commits" ]]; then
  while IFS= read -r commit; do
    matched=false
    for declared in "${declared_patches[@]:-}"; do
      [[ -n "$declared" && "$commit" == "$declared" ]] && { matched=true; break; }
    done
    [[ "$matched" == true ]] \
      || fail "origin/deploy carries an undeclared commit ($commit) on top of approved base '$approved_base' -- add it to $APPROVED_BASE_FILE if it's a genuine residual patch (Phase 4/6), else investigate how it got there"
  done <<< "$extra_commits"
fi

# ---- Pre-flight: prove the target commit in a fresh, throwaway clone
# before touching anything real. Cloning from $REPO_DIR (not over the
# network) since its object database already has target_head's objects
# from the git fetch above. ----
mkdir -p "$PREFLIGHT_SCRATCH_BASE" || fail "cannot create preflight scratch dir $PREFLIGHT_SCRATCH_BASE"
preflight_dir="$(mktemp -d "$PREFLIGHT_SCRATCH_BASE/deploy-advance-preflight.XXXXXX")" \
  || fail "cannot create preflight clone directory"
trap 'rm -rf "$preflight_dir"' EXIT

if (
    set -euo pipefail
    echo "=== deploy-advance preflight: $(date -u +%Y-%m-%dT%H:%M:%SZ) target=$target_head ==="

    git clone --quiet "$REPO_DIR" "$preflight_dir"
    git -C "$preflight_dir" checkout --quiet "$target_head"

    echo "--- ast.parse over every tracked .py file ---"
    git -C "$preflight_dir" ls-files '*.py' | python3 -c '
import ast, sys
failed = False
for path in sys.stdin.read().splitlines():
    try:
        with open(path, "rb") as fh:
            ast.parse(fh.read(), filename=path)
    except SyntaxError as e:
        print(f"SYNTAX ERROR: {path}: {e}")
        failed = True
sys.exit(1 if failed else 0)
'

    echo "--- uv sync --frozen ---"
    ( cd "$preflight_dir" && "$UV_BIN" sync --frozen )

    preflight_python="$preflight_dir/.venv/bin/python3"
    [[ -x "$preflight_python" ]] || { echo "no venv python produced by uv sync at $preflight_python"; exit 1; }

    echo "--- import smoke test ---"
    ( cd "$preflight_dir" && "$preflight_python" -c "import hermes_cli.main" )

    echo "--- hermes plugins list (zero load errors) ---"
    plugins_output="$(cd "$preflight_dir" && HERMES_HOME="$preflight_dir/.preflight-home" "$preflight_python" -m hermes_cli.main plugins list 2>&1)"
    echo "$plugins_output"
    if grep -qiE 'error|failed to load' <<< "$plugins_output"; then
      echo "hermes plugins list reported a load error"
      exit 1
    fi

    echo "--- offline acceptance-checklist subset ---"
    ( cd "$preflight_dir" && HERMES_HOME="$preflight_dir/.preflight-home" "$preflight_python" -m pytest "${OFFLINE_CHECKLIST_TESTS[@]}" )

    echo "=== preflight passed ==="
  ) > "$PREFLIGHT_LOG" 2>&1
then
  preflight_status=0
else
  preflight_status=$?
fi

rm -rf "$preflight_dir"
trap 'fail "unexpected error at line $LINENO"' ERR

[[ "$preflight_status" -eq 0 ]] \
  || fail "pre-flight failed for target $target_head (exit $preflight_status) -- live checkout untouched. Full log: $PREFLIGHT_LOG"

# ---- Pre-flight passed: now touch the real live checkout. ----
changed_files="$(git diff --name-only "$before_head" "$target_head")"

git pull --ff-only origin deploy > "$PULL_LOG" 2>&1 \
  || fail "git pull --ff-only failed after passing every prior check -- unexpected, needs manual review (full output: $PULL_LOG)"

after_head="$(git rev-parse HEAD)"
[[ "$after_head" == "$target_head" ]] || fail "pull completed but HEAD ($after_head) does not match origin/deploy ($target_head)"

commit_count="$(git rev-list --count "$before_head..$after_head")"

deps_changed=false
if grep -qE '^(pyproject\.toml|uv\.lock)$' <<< "$changed_files"; then
  deps_changed=true
fi

scheduled_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_advance_receipt "scheduled" "$scheduled_at" "$before_head" "$after_head" "$commit_count"

deps_note="not_needed"
[[ "$deps_changed" == true ]] && deps_note="scheduled"

echo "deploy-advance: advanced $commit_count commit(s) ($before_head -> $after_head), deps_reinstall=$deps_note, gateway restart scheduled (async) -- L3 drift detection reads $RECEIPT. Preflight log: $PREFLIGHT_LOG. Full diff detail: $PULL_LOG"

# Detached restart, same systemd-run pattern as sync-fork.sh (see that
# script's own header for why: `systemctl --user restart` tears down this
# script's whole cgroup, so a synchronously-run restart step would kill
# itself mid-flight; a transient systemd-run --user unit lands outside that
# cgroup and survives).
#
# --setenv is mandatory here, not cosmetic: systemd-run's new unit is spawned
# by the systemd --user manager itself, not forked from this shell, so it
# does NOT inherit this process's environment the way a plain subprocess
# (the setsid fallback below) would. Without these, the restart script
# silently falls back to ITS OWN hardcoded production defaults --
# ~/.hermes/hermes-agent and hermes-gateway.service -- regardless of
# whatever DEPLOY_* overrides this script was given. Found the hard way:
# an early test run of this exact script restarted the real live gateway
# and re-synced its real dependencies, twice, because of exactly this gap
# (2026-09-29 Gate A rehearsal).
restart_unit="deploy-advance-restart-$(date -u +%Y%m%d%H%M%S)-$$"
if ! systemd-run --user --collect --quiet --unit="$restart_unit" \
    --setenv=DEPLOY_REPO_DIR="$REPO_DIR" --setenv=DEPLOY_HERMES_HOME="$HERMES_HOME" \
    --setenv=DEPLOY_UV_BIN="$UV_BIN" --setenv=DEPLOY_GATEWAY_UNIT="$GATEWAY_UNIT" \
    --property=StandardOutput="file:$RESTART_LOG" \
    --property=StandardError="append:$RESTART_LOG" \
    bash "$RESTART_SCRIPT" "$scheduled_at" "$before_head" "$after_head" "$commit_count" "$deps_changed"
then
  echo "deploy-advance: systemd-run --user failed; falling back to a setsid launch. The restart will still happen, but its completion marker will not be written reliably." >> "$RESTART_LOG"
  nohup setsid bash "$RESTART_SCRIPT" "$scheduled_at" "$before_head" "$after_head" "$commit_count" "$deps_changed" \
    >> "$RESTART_LOG" 2>&1 < /dev/null &
  disown
fi

exit 0

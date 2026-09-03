#!/usr/bin/env bash
set -euo pipefail

# Read-only, no_agent nightly watch: reports where upstream/main sits
# relative to the live checkout. No LLM call, no analysis, no merge.
# Allowed network action: fetch upstream main only.
# No output = no notification when the checkout is not behind upstream.
#
# This is deliberately NOT a guarantee/pointer-area summary — see
# Plans/2026-09-02 - CRONBURN PENDGATE Upstream Review Cost, Step 1:
# a summary earns nothing when the closing reminder already tells the
# reader what to do, and the same pattern has been tried and dropped
# elsewhere in this system.

GIT="git -C /home/jiddy/.hermes/hermes-agent"
HEARTBEAT="/home/jiddy/.hermes/scripts/.upstream-head-watch-last-success"

fetch_err="$(mktemp)"
log_err="$(mktemp)"
trap 'rm -f "$fetch_err" "$log_err"' EXIT

if ! $GIT fetch upstream main --quiet 2>"$fetch_err"; then
  err="$(cat "$fetch_err")"
  printf 'Hermes upstream head watch FAILED: git fetch upstream main failed: %s\n' "$err"
  exit 1
fi

# head_behind is the number that actually matters: is the checked-out HEAD
# behind upstream. origin_behind / ahead_of_upstream are reported alongside
# it for context but never gate the check on their own.
head_behind="$($GIT rev-list HEAD..upstream/main --count)"
origin_behind="$($GIT rev-list origin/main..upstream/main --count)"
ahead_of_upstream="$($GIT rev-list upstream/main..HEAD --count)"

touch "$HEARTBEAT"

if [[ "$head_behind" == "0" ]]; then
  exit 0
fi

# `set -e` does not propagate a failure inside a process-substitution
# pipeline (`mapfile -t x < <(cmd)` ignores cmd's exit status), so capture
# git log's own exit status explicitly before trusting its output.
if ! commit_log="$($GIT log HEAD..upstream/main --oneline 2>"$log_err")"; then
  err="$(cat "$log_err")"
  printf 'Hermes upstream head watch FAILED: git log HEAD..upstream/main failed: %s\n' "$err"
  exit 1
fi

mapfile -t commit_lines <<< "$commit_log"
max_lines=15
total_lines="${#commit_lines[@]}"
show_lines=$(( total_lines < max_lines ? total_lines : max_lines ))

upstream_head_full="$($GIT rev-parse upstream/main)"
upstream_head_short="$($GIT rev-parse --short upstream/main)"
local_head_full="$($GIT rev-parse HEAD)"
local_head_short="$($GIT rev-parse --short HEAD)"

printf 'Hermes upstream head watch: local checkout (HEAD) is %s commit(s) behind upstream/main.\n' "$head_behind"
printf '(origin/main..upstream/main: %s, upstream/main..HEAD: %s)\n\n' "$origin_behind" "$ahead_of_upstream"
printf 'upstream/main: %s (%s)\n' "$upstream_head_short" "$upstream_head_full"
printf 'local HEAD:    %s (%s)\n\n' "$local_head_short" "$local_head_full"
printf 'Commits:\n'
for ((i=0; i<show_lines; i++)); do
  printf '%s\n' "${commit_lines[i]}"
done

if (( total_lines > max_lines )); then
  printf '+%s more\n' "$(( total_lines - max_lines ))"
fi

printf '\nAsk Jarvis to run the upstream review to evaluate and act on these — this watch job only checks, it does not merge, review, or apply anything.\n'

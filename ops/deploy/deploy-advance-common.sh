#!/usr/bin/env bash
# Shared helpers for deploy-advance.sh and deploy-advance-restart-async.sh.
# Sourced, never executed directly. Adapted from scripts/sync-fork-common.sh's
# write_marker (same atomic-write pattern) but for the deploy-advance receipt --
# kept as a SEPARATE marker/receipt file from sync-fork's own
# sync_fork_restart_state.json, so the two mechanisms never read or clobber
# each other's state while hermes-sync-fork keeps running unmodified alongside
# this one (MOORING Step 1.3 -- "the existing hermes-sync-fork job is not
# replaced in this phase").

# Atomically write the advance receipt as JSON: temp file in the same
# directory + fsync + os.replace (rename on the same filesystem is atomic, so
# L3 drift detection can never observe a half-written file).
#
# Args: state scheduled_at before_head after_head commit_count [reason]
write_advance_receipt() {
  local state="$1" scheduled_at="$2" before_head="$3" after_head="$4" commit_count="$5" reason="${6:-}"
  python3 - "$RECEIPT" "$state" "$scheduled_at" "$before_head" "$after_head" "$commit_count" "$reason" <<'PYEOF'
import json
import os
import sys
import tempfile

receipt, state, scheduled_at, before_head, after_head, commit_count, reason = sys.argv[1:8]

data = {
    "state": state,
    "scheduled_at": scheduled_at,
    "before_head": before_head,
    "after_head": after_head,
    "commit_count": int(commit_count),
}
if reason:
    data["reason"] = reason

directory = os.path.dirname(receipt) or "."
os.makedirs(directory, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=directory, prefix=".deploy_advance_state.json.")
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, receipt)
finally:
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
PYEOF
}

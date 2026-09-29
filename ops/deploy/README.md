# ops/deploy/

L1's advance mechanism (MOORING Step 1.3). Deliberately separated from the
application's own package tree -- these scripts control what code the live
checkout runs, so they are reviewed and tracked here rather than shipped as
part of the installed `hermes-agent` package or mixed in with the rest of
`scripts/`.

- `deploy-advance.sh` -- fetches `origin/deploy`, refuses anything that
  isn't a clean fast-forward reachable from the declared approved base
  (`approved-base.txt`), proves the target commit in a throwaway clone, then
  advances the live checkout and schedules a restart.
- `deploy-advance-restart-async.sh` -- the detached restart step
  `deploy-advance.sh` launches via `systemd-run --user`, derived from
  `scripts/sync-fork-restart-async.sh`'s own proven pattern.
- `deploy-advance-common.sh` -- shared atomic receipt-writer, sourced by both.
- `approved-base.txt` -- the declared reachability target: an upstream
  `stable` tag or pinned SHA, plus any declared residual-patch commit SHAs
  from a Phase 4/6 disposition. Ships unconfigured (a placeholder) until the
  actual candidate pin is chosen.

Every path is env-var overridable (`DEPLOY_REPO_DIR`, `DEPLOY_HERMES_HOME`,
etc.) so the exact same script can be pointed at a staging clone while being
proved (Phase 1) and only pointed at the real live checkout at cutover
(Phase 6), without a rewrite. Not wired into cron in this phase -- inert
until scheduled or run by hand.

The one-time "switch" mode for the first move from fork history to upstream
history (not a fast-forward) is Step 6.2's own separate job, each run needing
its own approval -- there is deliberately no flag for it here.

See `tests/ops/deploy/test_deploy_advance.py` for the test suite (constructs
fixture git repos and runs the real script as a subprocess).

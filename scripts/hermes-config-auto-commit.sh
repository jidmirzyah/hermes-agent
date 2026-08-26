#!/bin/bash
# Mechanical auto-commit + push for the skills/cron git repo. No LLM
# reasoning - pure git plumbing. Deliberately stages only skills/ and
# cron/jobs.json by explicit path (not `git add -A`) - this repo's root
# is ~/.hermes, which also holds auth.json, google_token.json,
# config.yaml, credentials/, etc., so relying on .gitignore alone for a
# blanket add is a needless risk when an explicit path list is just as
# easy and cannot stage anything outside the two intended trees.
# Silent when there is nothing new; a push failure is left to fail the
# script (set -e) so it surfaces as a real cron error rather than
# silently defeating the off-machine backup.
set -euo pipefail

REPO="/home/jiddy/.hermes"
cd "$REPO"

git add skills cron/jobs.json .gitignore

if ! git diff --cached --quiet; then
    git commit -q -m "auto: $(date -Iseconds)"
fi

git push -q origin main

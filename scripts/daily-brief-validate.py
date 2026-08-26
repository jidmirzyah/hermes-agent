#!/usr/bin/env python3
"""Validate daily-brief save/delivery agreement, then advance its checkpoint."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from foundation_cron_common import load_json, local_now, normalized_text, parse_datetime

DAILY_JOB_NAME = "hermes-daily-brief"
RESPONSE_MARKER = "\n## Response\n\n"
SILENT_MARKER = "[SILENT]"

# Shape of a brief, used to decide whether a delivered response is safe to
# write into the vault. Every one of these is a failure that actually happened
# rather than a guess about what might:
#   2026-08-13 the response was an unrelated ad-hoc verification report while a
#             real brief already sat in the vault  -> opening/section checks,
#             and never overwrite a non-empty file
#   2026-08-17 a complete brief followed by a fabricated "cannot overwrite
#             existing file" refusal naming a path that does not exist
#   2026-08-18 the entire brief twice, split by "Final response:", then
#             `[[DONE]]`                            -> truncate at the closing line
#   2026-08-11 curly apostrophes throughout; every run since uses straight ones,
#             so matching only U+0027 would silently refuse to repair that shape
BRIEF_OPENING = "Good morning"
BRIEF_SECTION_MARKERS = ("\U0001F4C5", "\U0001F4E7", "\U0001F5C2", "\U0001F4E5", "\U0001F468", "\U0001F527")
MIN_BRIEF_SECTIONS = 4
BRIEF_CLOSING_RE = re.compile("^That['\u2019]s everything since .*$", re.MULTILINE)


def _find_job(hermes_home: Path) -> dict:
    jobs = load_json(hermes_home / "cron/jobs.json").get("jobs", [])
    matches = [job for job in jobs if job.get("name") == DAILY_JOB_NAME]
    if len(matches) != 1:
        raise RuntimeError(f"expected one {DAILY_JOB_NAME} job, found {len(matches)}")
    return matches[0]


def _latest_output(hermes_home: Path, job_id: str) -> Path | None:
    output_dir = hermes_home / "cron/output" / job_id
    if not output_dir.is_dir():
        return None
    files = [path for path in output_dir.glob("*.md") if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def _extract_final_response(audit_document: str) -> str:
    if RESPONSE_MARKER not in audit_document:
        raise ValueError("cron audit output has no final ## Response section")
    return audit_document.rsplit(RESPONSE_MARKER, 1)[1]


def extract_brief(audit_document: str) -> str:
    """The brief body from a cron audit document, or "" if it isn't one.

    The delivered response is authoritative for what the saved brief should
    contain -- proven by comparing every saved brief against its own audit
    output, which agrees on 9 of 9 days where both exist. It is *not*
    automatically safe to write: see the dated failure list above the shape
    constants. This returns text only when the response both looks like a brief
    and can be bounded, and truncates at the closing line the prompt contract
    defines as the end of one.
    """
    if RESPONSE_MARKER not in audit_document:
        return ""
    body = normalized_text(audit_document.rsplit(RESPONSE_MARKER, 1)[1])
    if not body or body == SILENT_MARKER or body.startswith(SILENT_MARKER):
        return ""
    if not body.startswith(BRIEF_OPENING):
        return ""
    if sum(marker in body for marker in BRIEF_SECTION_MARKERS) < MIN_BRIEF_SECTIONS:
        return ""
    closing = BRIEF_CLOSING_RE.search(body)
    if not closing:
        return ""
    return body[: closing.end()].strip()


def _set_checkpoint(database: Path, job_id: str, value: str, now: datetime) -> None:
    with sqlite3.connect(database, timeout=5) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS cron_notepad (
                 job_id TEXT NOT NULL,
                 key TEXT NOT NULL,
                 value TEXT NOT NULL,
                 updated_at TEXT NOT NULL,
                 PRIMARY KEY (job_id, key)
               )"""
        )
        connection.execute(
            """INSERT INTO cron_notepad (job_id, key, value, updated_at)
               VALUES (?, 'last_brief_at', ?, ?)
               ON CONFLICT(job_id, key)
               DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (job_id, value, now.isoformat()),
        )


SELF_HEAL_KEY = "self_heal_triggered_date"


def _self_heal_already_triggered_today(database: Path, job_id: str, today: str) -> bool:
    with sqlite3.connect(database, timeout=5) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS cron_notepad (
                 job_id TEXT NOT NULL,
                 key TEXT NOT NULL,
                 value TEXT NOT NULL,
                 updated_at TEXT NOT NULL,
                 PRIMARY KEY (job_id, key)
               )"""
        )
        row = connection.execute(
            "SELECT value FROM cron_notepad WHERE job_id = ? AND key = ?",
            (job_id, SELF_HEAL_KEY),
        ).fetchone()
        return row is not None and row[0] == today


def _record_self_heal_triggered(database: Path, job_id: str, today: str, now: datetime) -> None:
    with sqlite3.connect(database, timeout=5) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        # Create rather than assume. Today this only ever runs after
        # _self_heal_already_triggered_today has created the table, so the
        # dependency is invisible until someone records without checking first
        # -- which then fails with "no such table" at exactly the moment a
        # repair is being recorded. The two sibling writers already do this.
        connection.execute(
            """CREATE TABLE IF NOT EXISTS cron_notepad (
                 job_id TEXT NOT NULL,
                 key TEXT NOT NULL,
                 value TEXT NOT NULL,
                 updated_at TEXT NOT NULL,
                 PRIMARY KEY (job_id, key)
               )"""
        )
        connection.execute(
            """INSERT INTO cron_notepad (job_id, key, value, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(job_id, key)
               DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (job_id, SELF_HEAL_KEY, today, now.isoformat()),
        )


def _retrigger(hermes_home: Path, job_id: str, now: datetime, why: str) -> str:
    """Re-run the job, for the cases a rebuild cannot cover.

    A rebuild needs a brief to have been composed. When the provider fails
    outright (2026-08-12 and 2026-08-15 both 429'd) there is no delivered text
    at all, and re-running is the only repair. Bounded to one attempt per
    calendar day via the same notepad.db that holds the checkpoint, so a
    persistent fault escalates as a repeated daily alert rather than looping.
    """
    database = hermes_home / "cron/notepad.db"
    today = now.date().isoformat()
    if _self_heal_already_triggered_today(database, job_id, today):
        return f"(retry skipped -- {why}, and a retry was already attempted today)"
    from cron.jobs import trigger_job

    if not trigger_job(job_id):
        return f"(retry failed -- {why}, and trigger_job() found no matching job)"
    _record_self_heal_triggered(database, job_id, today, now)
    return f"(retry: {why}; re-triggered hermes-daily-brief)"


def attempt_repair(*, hermes_home: Path, vault_root: Path, now: datetime) -> str:
    """Rebuild the saved brief from the response cron already delivered.

    Preferred over re-running the job because it is deterministic: the model
    skipped the ``write_file`` call on 4 of the 14 runs that completed, so a
    retry is a coin flip on the same odds, while the text it delivered is
    already on disk and provably equal to what a successful write produces.

    Never raises -- a repair failure must not mask the alert that triggered it.
    """
    try:
        job = _find_job(hermes_home)
        job_id = job["id"]
    except Exception as exc:
        return f"(repair skipped: {type(exc).__name__}: {exc})"

    try:
        brief_path = vault_root / "Hermes/Briefs" / f"{now.date().isoformat()}.md"

        # Never overwrite content that is already there. On 2026-08-13 the
        # delivered response was an unrelated verification report while the
        # vault held a real 3,064-character brief; a rebuild that trusted the
        # response would have destroyed it. A mismatch is for a human.
        if brief_path.is_file() and normalized_text(
            brief_path.read_text(encoding="utf-8", errors="replace")
        ):
            return (
                "(repair skipped: a non-empty brief is already saved -- a "
                "mismatch against the delivered response needs manual review)"
            )

        output_path = _latest_output(hermes_home, job_id)
        if output_path is None:
            return _retrigger(hermes_home, job_id, now, "no cron output to rebuild from")

        # Yesterday's output must never be written as today's brief. Same
        # window rule validate() applies to the delivered/saved comparison.
        last_run_raw = job.get("last_run_at")
        if last_run_raw:
            try:
                last_run = parse_datetime(last_run_raw)
                produced = datetime.fromtimestamp(
                    output_path.stat().st_mtime, tz=last_run.tzinfo
                )
                if produced < last_run.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ):
                    return _retrigger(
                        hermes_home,
                        job_id,
                        now,
                        "the latest cron output predates today's run window",
                    )
            except ValueError:
                return _retrigger(
                    hermes_home, job_id, now, "last_run_at is unparseable"
                )

        brief = extract_brief(output_path.read_text(encoding="utf-8", errors="replace"))
        if not brief:
            return _retrigger(
                hermes_home,
                job_id,
                now,
                "the delivered response is not a usable brief",
            )

        brief_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = brief_path.with_suffix(brief_path.suffix + ".tmp")
        tmp.write_text(brief, encoding="utf-8")
        tmp.replace(brief_path)

        written = brief_path.read_text(encoding="utf-8", errors="replace")
        if normalized_text(written) != normalized_text(brief):
            return "(repair failed: the rebuilt brief did not survive read-back)"
        return (
            f"(repaired: rebuilt {brief_path.name} from the response cron "
            f"delivered, {len(brief)} chars -- the agent skipped its write_file "
            "call)"
        )
    except Exception as exc:
        return f"(repair failed: {type(exc).__name__}: {exc})"


def validate(
    *, hermes_home: Path, vault_root: Path, now: datetime, update_checkpoint: bool = True
) -> list[str]:
    problems: list[str] = []
    job = _find_job(hermes_home)
    job_id = job["id"]
    last_run_raw = job.get("last_run_at")
    last_run: datetime | None = None
    if not last_run_raw:
        problems.append("daily brief has no recorded last run")
    else:
        try:
            last_run = parse_datetime(last_run_raw)
            if last_run.date() != now.date():
                problems.append(f"latest daily-brief run is from {last_run.date()}, not {now.date()}")
        except ValueError:
            problems.append("daily brief has an invalid last_run_at value")

    if job.get("last_status") != "ok":
        problems.append(f"daily brief status is {job.get('last_status')!r}, not 'ok'")
    deliver = str(job.get("deliver") or "")
    if not deliver.startswith("telegram:"):
        problems.append(f"daily brief delivery target is {deliver!r}, not explicit Telegram")
    if job.get("last_delivery_error"):
        problems.append(f"daily brief delivery error: {job['last_delivery_error']}")

    brief_path = vault_root / "Hermes/Briefs" / f"{now.date().isoformat()}.md"
    output_path = _latest_output(hermes_home, job_id)
    if not brief_path.is_file():
        problems.append(f"saved brief is missing: {brief_path}")
    if output_path is None:
        problems.append("cron final-response output is missing")

    if brief_path.is_file() and output_path is not None:
        saved = normalized_text(brief_path.read_text(encoding="utf-8", errors="replace"))
        try:
            delivered = normalized_text(
                _extract_final_response(output_path.read_text(encoding="utf-8", errors="replace"))
            )
        except ValueError as exc:
            problems.append(str(exc))
            delivered = ""
        if not saved:
            problems.append("saved brief is empty")
        elif delivered and saved != delivered:
            problems.append(
                "saved brief does not match cron's final delivered response "
                f"({len(saved)} vs {len(delivered)} characters)"
            )
        if last_run and datetime.fromtimestamp(output_path.stat().st_mtime, tz=last_run.tzinfo) < last_run.replace(
            hour=0, minute=0, second=0, microsecond=0
        ):
            problems.append("latest cron output file predates today's run window")

    if not problems and update_checkpoint and last_run:
        _set_checkpoint(hermes_home / "cron/notepad.db", job_id, last_run.isoformat(), now)
    return problems


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--vault-root", type=Path, default=Path.home() / "Obsidian Core")
    parser.add_argument("--now", help="ISO timestamp override for deterministic tests")
    parser.add_argument("--no-checkpoint-update", action="store_true")
    parser.add_argument(
        "--no-self-heal", action="store_true", help="test-only: skip the repair step"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = parse_datetime(args.now) if args.now else local_now()
    try:
        problems = validate(
            hermes_home=args.hermes_home,
            vault_root=args.vault_root,
            now=now,
            update_checkpoint=not args.no_checkpoint_update,
        )
    except Exception as exc:
        problems = [f"validator crashed: {type(exc).__name__}: {exc}"]
    if problems:
        print("🩺 Operations Alert — Daily brief validation failed")
        for problem in problems:
            print(f"- {problem}")
        if not args.no_self_heal:
            outcome = attempt_repair(
                hermes_home=args.hermes_home, vault_root=args.vault_root, now=now
            )
            print(outcome)
            # Re-validate rather than assume the repair was sufficient, and let
            # the normal checkpoint rule do the advancing: without this the
            # checkpoint stays behind and the next brief reports two days of
            # changes in one. The alert above still stands -- a repaired run is
            # reported, never silently absorbed.
            try:
                remaining = validate(
                    hermes_home=args.hermes_home,
                    vault_root=args.vault_root,
                    now=now,
                    update_checkpoint=not args.no_checkpoint_update,
                )
            except Exception as exc:
                print(f"(re-validation failed: {type(exc).__name__}: {exc})")
            else:
                if remaining:
                    print("(still failing after repair: " + "; ".join(remaining) + ")")
                else:
                    print("(validation now passes; checkpoint advanced)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

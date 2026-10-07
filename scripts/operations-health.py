#!/usr/bin/env python3
"""Failure-only health check for Hermes cron and encrypted backups."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from foundation_cron_common import age_hours, load_json, local_now, newest_matching, parse_datetime


def _freshness_alert(label: str, path: Path, maximum_hours: float, now: datetime) -> str | None:
    if not path.exists():
        return f"{label} heartbeat is missing: {path}"
    age = age_hours(path, now)
    if age > maximum_hours:
        return f"{label} heartbeat is stale ({age:.1f}h; limit {maximum_hours:g}h)"
    return None


SCRIPT_SUFFIXES = (".py", ".sh")


def script_drift_alerts(hermes_home: Path) -> list[str]:
    """Compare the scripts cron actually executes against their tracked copies.

    ``<HERMES_HOME>/scripts/`` is what cron runs -- cron/scheduler.py resolves
    every job's script path against it -- and that directory is untracked. The
    tracked copies live in the checkout at ``hermes-agent/scripts/``, and
    nothing syncs the two: sync-fork.sh only fast-forwards the checkout.

    Both directions of drift have already happened. Live ran ahead of the repo
    for weeks carrying fixes that existed nowhere else, including a validator
    written after a skill sat unselectable for twelve days; and within an hour
    of that being corrected, the repo ran ahead of live with merged fixes that
    had not been deployed.

    The failure was never that the copies differed -- it was that nothing
    noticed. So this reports and deliberately does not sync. An automatic
    repo -> live copy is precisely the mechanism that would have destroyed the
    live-only work.

    Timing makes this reliable rather than noisy: hermes-sync-fork
    fast-forwards the checkout at 02:40 and this job runs at 03:40, so the
    checkout is already current and any remaining difference is real drift
    rather than a pull that has not happened yet.

    Repo-only scripts are deliberately NOT reported: most of the checkout's
    scripts are development and CI tooling with no business on the VM.
    """
    live_dir = hermes_home / "scripts"
    repo_dir = hermes_home / "hermes-agent" / "scripts"
    if not repo_dir.is_dir():
        return [f"Script drift cannot be checked: {repo_dir} is missing"]
    if not live_dir.is_dir():
        return [f"Script drift cannot be checked: {live_dir} is missing"]

    def names(d: Path) -> set[str]:
        return {f.name for f in d.iterdir() if f.is_file() and f.suffix in SCRIPT_SUFFIXES}

    live, repo = names(live_dir), names(repo_dir)
    differing = []
    alerts: list[str] = []
    for name in sorted(live & repo):
        try:
            if (live_dir / name).read_bytes() != (repo_dir / name).read_bytes():
                differing.append(name)
        except OSError as exc:
            alerts.append(f"Script {name} could not be compared: {exc}")
    if differing:
        # Named as one alert rather than one per file, and with both causes
        # spelled out. Running this against the real system mid-afternoon
        # produced eight "differs" lines whose actual cause was a checkout
        # eight commits behind -- accurate, but it read like eight separate
        # problems. A lagging checkout also raises its own hermes-sync-fork
        # alert, so saying so here points at the same root cause instead of
        # competing with it.
        alerts.append(
            f"{len(differing)} script(s) differ between {live_dir} and the "
            f"tracked copies in {repo_dir}: {', '.join(differing)}. Either a "
            "merged change was never deployed, or a live edit was never "
            "committed -- check which side is newer before copying anything. "
            "If hermes-sync-fork has not run since the last merge, the "
            "checkout is simply behind and this resolves itself."
        )
    for name in sorted(live - repo):
        alerts.append(
            f"Script {name} exists only in {live_dir} -- it is untracked and "
            "unreviewed, and exists nowhere else"
        )
    return alerts


def skill_registry_alerts(hermes_home: Path) -> list[str]:
    """Compare the skill usage registry against the skills actually on disk.

    ``skills/.usage.json`` is telemetry, and ``bump_use`` deliberately records
    any name it is handed -- its own docstring calls usage tracking "pure
    observability ... orthogonal to whether a skill is ever curated", which is
    why bundles and hub-installed skills get counted too. The cost of that
    permissiveness is that a name nothing can resolve still accumulates a
    record, and nothing notices.

    It happened. ``vault-governance`` held 51 recorded uses with no SKILL.md
    anywhere, and its record was ``curator_managed: True`` -- so the one entry
    the curator would eventually have acted on was the one that did not exist.
    In the other direction, 18 skills installed in a single batch on 2026-08-10
    had no record at all, which left them invisible to the curator and made any
    "unused skills" count wrong in both directions at once.

    Reports both directions and changes nothing. Repair is ``forget()`` for a
    dead record and ``seed_record_if_missing()`` for a missing one, but which
    of those is right depends on why the drift appeared, so it stays a
    judgement call rather than an automatic sweep.
    """
    skills_dir = hermes_home / "skills"
    usage_file = skills_dir / ".usage.json"
    if not skills_dir.is_dir():
        return [f"Skill registry cannot be checked: {skills_dir} is missing"]
    if not usage_file.is_file():
        return [f"Skill registry cannot be checked: {usage_file} is missing"]
    try:
        usage = load_json(usage_file)
    except (OSError, ValueError) as exc:
        return [f"Skill registry cannot be read: {exc}"]
    if not isinstance(usage, dict):
        return [f"Skill registry is not an object: {usage_file}"]

    on_disk = {
        path.parent.name
        for path in skills_dir.rglob("SKILL.md")
        if path.is_file()
    }
    tracked = set(usage)
    alerts: list[str] = []
    orphaned = sorted(tracked - on_disk)
    untracked = sorted(on_disk - tracked)
    if orphaned:
        alerts.append(
            f"{len(orphaned)} skill usage record(s) have no SKILL.md on disk: "
            f"{', '.join(orphaned)}. A record with no skill still accrues "
            "usage and can be curator-managed -- clear it with "
            "skill_usage.forget() once you know why it is there."
        )
    if untracked:
        alerts.append(
            f"{len(untracked)} skill(s) on disk have no usage record: "
            f"{', '.join(untracked)}. They are invisible to the curator and to "
            "any usage-based decision -- seed_record_if_missing() gives each an "
            "anchor without enrolling it for archival."
        )
    return alerts


DEPLOY_DRIFT_URGENT_DAYS = 7.0


def deploy_drift_alerts(
    hermes_home: Path, now: datetime, *, urgent_days: float = DEPLOY_DRIFT_URGENT_DAYS,
) -> tuple[list[str], dict[str, object]]:
    """L3 drift detection and pin-staleness report (MOORING Step 1.4).

    Replaces the old behind-``main`` commit count, which is meaningless once
    the live checkout tracks a pinned ``deploy`` branch instead of continuously
    reconciled upstream history (see the MOORING plan's Phase 1). Reports:

      * live ``HEAD`` against ``origin/deploy`` (an unadvanced approved pin)
      * days since ``deploy-advance.sh``'s last recorded advance
      * the newest upstream ``stable``-pattern tag available, and whether
        ``origin/deploy`` already includes it

    ``URGENT`` when the pin is more than ``urgent_days`` days stale by either
    measure, or the newest available tag's own message matches an
    obvious security keyword (best-effort only: a plain ``git tag -n99``
    read of the annotation text, not a GitHub Releases API call -- adding
    that dependency to a background health check trades reliability for a
    weak signal that a human reviewing the tag before merging the L1 PR
    would catch anyway).

    Deliberately does not fetch: every other check in this file is a pure
    local read, and ``hermes-sync-fork`` (nightly) and
    ``hermes-upstream-main-check`` already fetch ``origin`` and ``upstream``
    respectively as part of their own jobs -- reusing their freshness keeps
    this check's own failure surface at zero added network calls, at the
    cost of at most ~1 day of staleness in the comparison itself, which is
    immaterial against a 7-day urgent threshold.
    """
    repo_dir = hermes_home / "hermes-agent"
    alerts: list[str] = []
    facts: dict[str, object] = {}

    if not repo_dir.is_dir():
        return [f"Deploy drift cannot be checked: {repo_dir} is missing"], facts

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True, text=True, timeout=30)

    head = git("rev-parse", "HEAD")
    if head.returncode != 0:
        return [f"Deploy drift check could not read live HEAD: {head.stderr.strip()}"], facts
    live_head = head.stdout.strip()
    facts["live_head"] = live_head

    deploy_ref = git("rev-parse", "origin/deploy")
    if deploy_ref.returncode != 0:
        # Expected until Step 1.1's deploy branch actually exists on origin --
        # not itself an alert.
        facts["origin_deploy_head"] = None
    else:
        deploy_head = deploy_ref.stdout.strip()
        facts["origin_deploy_head"] = deploy_head
        if live_head != deploy_head:
            behind = git("rev-list", "--count", f"{live_head}..{deploy_head}")
            commits_behind = int(behind.stdout.strip()) if behind.returncode == 0 and behind.stdout.strip().isdigit() else None
            facts["commits_behind_deploy"] = commits_behind
            detail = f"{commits_behind} commit(s)" if commits_behind is not None else "an unknown number of commits"
            alerts.append(f"Live checkout is {detail} behind origin/deploy ({live_head[:12]} -> {deploy_head[:12]}) -- deploy-advance.sh has not applied an approved advance yet")

    # Days since deploy-advance.sh's last recorded advance.
    receipt_path = hermes_home / "cron" / "deploy_advance_state.json"
    if receipt_path.is_file():
        try:
            receipt = load_json(receipt_path)
            scheduled_at = parse_datetime(receipt["scheduled_at"])
            days_since = (now - scheduled_at).total_seconds() / 86400
            facts["days_since_last_advance"] = round(days_since, 2)
            facts["last_advance_state"] = receipt.get("state")
            if receipt.get("state") == "failed":
                alerts.append(f"Last deploy-advance run failed: {receipt.get('reason', 'no reason recorded')}")
            elif days_since > urgent_days:
                alerts.append(f"URGENT: {days_since:.1f} days since the last deploy-advance (limit {urgent_days:g})")
        except (OSError, ValueError, KeyError) as exc:
            alerts.append(f"Deploy-advance receipt could not be read: {exc}")
    else:
        facts["days_since_last_advance"] = None
        # Not an alert: deploy-advance.sh is not wired into cron until real
        # cutover (MOORING Phase 6), so "never advanced" is the expected
        # state through Phases 1-5, not a fault.

    # Newest upstream stable-pattern tag, and whether origin/deploy has it.
    all_tags = git("tag", "--sort=-v:refname", "--list", "v[0-9]*")
    tag_list = [t for t in all_tags.stdout.splitlines() if t.strip()]
    if not tag_list:
        facts["newest_upstream_tag"] = None
    else:
        newest_tag = tag_list[0]
        facts["newest_upstream_tag"] = newest_tag
        newest_sha = git("rev-parse", newest_tag).stdout.strip()
        if facts.get("origin_deploy_head"):
            ahead = git("merge-base", "--is-ancestor", newest_sha, facts["origin_deploy_head"])
            if ahead.returncode != 0:
                behind_tag = git("rev-list", "--count", f"{facts['origin_deploy_head']}..{newest_sha}")
                commits = behind_tag.stdout.strip() if behind_tag.returncode == 0 else "an unknown number of"
                alerts.append(f"origin/deploy is behind the newest upstream tag {newest_tag} ({commits} commit(s))")

        tag_message = git("tag", "-n99", "--list", newest_tag).stdout.lower()
        security_keywords = ("cve", "security", "vulnerability", "rce ", "exploit")
        if any(kw in tag_message for kw in security_keywords):
            alerts.append(
                f"URGENT: newest upstream tag {newest_tag} looks security-relevant "
                "(matched a keyword in its tag message) -- review before the next "
                "scheduled advance, regardless of the days-behind threshold"
            )

    return alerts, facts


def inspect_health(
    *,
    hermes_home: Path,
    primary_backup_dir: Path,
    secondary_backup_dir: Path,
    now: datetime,
    backup_max_hours: float = 48,
    runtime_checks: bool = True,
) -> tuple[list[str], dict[str, object]]:
    alerts: list[str] = []
    facts: dict[str, object] = {}

    primary = newest_matching(primary_backup_dir, "hermes-backup-*.tar.age")
    secondary = newest_matching(secondary_backup_dir, "hermes-backup-*.tar.age")
    for label, archive in (("Primary encrypted backup", primary), ("Secondary encrypted backup", secondary)):
        if archive is None:
            alerts.append(f"{label} is missing")
            continue
        age = age_hours(archive, now)
        facts[f"{label.lower().replace(' ', '_')}_age_hours"] = round(age, 2)
        facts[f"{label.lower().replace(' ', '_')}_name"] = archive.name
        if age > backup_max_hours:
            alerts.append(f"{label} is stale ({age:.1f}h; limit {backup_max_hours:g}h)")

    if primary and secondary:
        if primary.name != secondary.name:
            alerts.append(
                "Secondary backup is behind primary "
                f"(primary {primary.name}; secondary {secondary.name})"
            )
        elif primary.stat().st_size != secondary.stat().st_size:
            alerts.append(f"Primary/secondary backup sizes differ for {primary.name}")

    if runtime_checks:
        heartbeat_checks = (
            ("Gateway", hermes_home / "state/gateway.heartbeat", 0.25),
            ("Cron ticker", hermes_home / "cron/ticker_last_success", 0.25),
            ("Upstream check", hermes_home / "scripts/.upstream-check-last-success", 48),
        )
        for label, path, maximum in heartbeat_checks:
            alert = _freshness_alert(label, path, maximum, now)
            if alert:
                alerts.append(alert)

        alerts.extend(script_drift_alerts(hermes_home))
        alerts.extend(skill_registry_alerts(hermes_home))

        deploy_alerts, deploy_facts = deploy_drift_alerts(hermes_home, now)
        alerts.extend(deploy_alerts)
        facts.update(deploy_facts)

        jobs_path = hermes_home / "cron/jobs.json"
        try:
            jobs = load_json(jobs_path).get("jobs", [])
        except (OSError, ValueError) as exc:
            alerts.append(f"Cron registry cannot be read: {exc}")
            jobs = []

        for job in jobs:
            if not job.get("enabled", True) or job.get("state") == "paused":
                continue
            name = job.get("name") or job.get("id") or "unknown job"
            last_status = job.get("last_status")
            if last_status not in (None, "ok"):
                alerts.append(f"Cron {name} last status is {last_status}: {job.get('last_error') or 'no detail'}")
            if job.get("last_delivery_error"):
                alerts.append(f"Cron {name} delivery failed: {job['last_delivery_error']}")

            next_run = job.get("next_run_at")
            if next_run:
                try:
                    overdue_hours = (now - parse_datetime(next_run)).total_seconds() / 3600
                    if overdue_hours > 0.25:
                        alerts.append(f"Cron {name} is overdue by {overdue_hours:.1f}h")
                except ValueError:
                    alerts.append(f"Cron {name} has an invalid next_run_at value")

            # A job that has never run is only a problem once it has actually
            # missed a fire. The overdue check above catches that the moment
            # next_run_at passes, and it applies to never-run jobs too. Judging
            # staleness by age-since-creation instead flagged every monthly job
            # created mid-month -- "never run after 11.6 days" for a job whose
            # first fire had not arrived yet. Known gap: if the scheduler ever
            # advanced next_run_at without executing, neither check fires;
            # detecting that needs interval awareness this script does not have.
            if not job.get("last_run_at") and not job.get("next_run_at"):
                alerts.append(f"Cron {name} has never run and has no next_run_at scheduled")

    return alerts, facts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--primary-backup-dir", type=Path, default=Path.home() / "backups/hermes-agent")
    parser.add_argument("--secondary-backup-dir", type=Path, default=Path.home() / "hermes-backups-sync")
    parser.add_argument("--now", help="ISO timestamp override for deterministic tests")
    parser.add_argument("--max-backup-age-hours", type=float, default=48)
    parser.add_argument("--summary", action="store_true", help="Print a compact healthy summary instead of silence")
    parser.add_argument("--skip-runtime-checks", action="store_true", help="Only inspect backup fixtures")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = parse_datetime(args.now) if args.now else local_now()
    try:
        alerts, facts = inspect_health(
            hermes_home=args.hermes_home,
            primary_backup_dir=args.primary_backup_dir,
            secondary_backup_dir=args.secondary_backup_dir,
            now=now,
            backup_max_hours=args.max_backup_age_hours,
            runtime_checks=not args.skip_runtime_checks,
        )
    except Exception as exc:
        print(f"🩺 Operations Alert\n- health check crashed: {type(exc).__name__}: {exc}")
        return 1

    if alerts:
        print("🩺 Operations Alert")
        for alert in alerts:
            print(f"- {alert}")
    elif args.summary:
        primary_age = facts.get("primary_encrypted_backup_age_hours")
        secondary_age = facts.get("secondary_encrypted_backup_age_hours")
        print(f"healthy; encrypted backup age {primary_age:.1f}h primary / {secondary_age:.1f}h secondary")
    return 0


if __name__ == "__main__":
    sys.exit(main())

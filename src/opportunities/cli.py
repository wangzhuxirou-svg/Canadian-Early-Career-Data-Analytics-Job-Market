"""Command-line entry points for local scheduled checks and status reports."""

import argparse
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from src.opportunities.runner import (
    AccountCollectorUnavailable,
    LOCAL_TIMEZONE,
    coverage_report,
    dry_run,
    load_enabled_organizers,
    next_schedule_slots,
    claim_current_slot,
)
from src.opportunities.state import OpportunityState
from src.opportunities.scan import (
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_COLLECTED,
    DEFAULT_MAX_POSTS,
    DEFAULT_MAX_SCROLLS,
    run_live_scan,
)


def _selected_accounts(organizer_ids: list[str] | None, limit: int | None):
    accounts = load_enabled_organizers()
    if organizer_ids:
        by_id = {account["organizer_id"]: account for account in accounts}
        unknown = sorted(set(organizer_ids) - set(by_id))
        if unknown:
            raise ValueError(f"Unknown or disabled organizer IDs: {', '.join(unknown)}.")
        accounts = [by_id[organizer_id] for organizer_id in organizer_ids]
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be positive.")
        accounts = accounts[:limit]
    return accounts


def _run(arguments) -> int:
    accounts = _selected_accounts(arguments.accounts, arguments.limit)
    if arguments.scheduled and (arguments.accounts or arguments.limit):
        raise ValueError("--scheduled checks all enabled accounts; use a manual run for a subset.")
    if arguments.offline and not arguments.dry_run:
        raise ValueError("--offline currently requires --dry-run.")
    if arguments.dry_run:
        result = dry_run(accounts, since=arguments.since)
    else:
        if arguments.since:
            raise ValueError("Live --since is not supported; use --lookback-days.")
        state = OpportunityState(arguments.state_db)
        slot = claim_current_slot(state, slots=arguments.times) if arguments.scheduled else None
        if arguments.scheduled and slot is None:
            print(json.dumps({"status": "skipped", "reason": "No unclaimed slot is due today."}))
            return 0
        try:
            result = run_live_scan(
                accounts=accounts,
                state=state,
                lookback_days=arguments.lookback_days,
                max_posts=arguments.max_posts_per_account,
                max_scrolls=arguments.max_scrolls,
                max_collected=arguments.max_collected_per_account,
                wait_seconds=arguments.wait_seconds,
            )
        except BaseException as error:
            if slot:
                state.finish_schedule_slot(slot[0], "failed", error=str(error))
            raise
        if slot:
            state.finish_schedule_slot(slot[0], result["status"], result.get("run_id"), result.get("error"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"complete": 0, "dry_run": 0, "partial": 1, "failed": 2}.get(result["status"], 2)


def _schedule(arguments) -> int:
    slots = next_schedule_slots(
        now=datetime.now(ZoneInfo(LOCAL_TIMEZONE)),
        slots=arguments.times,
        count=arguments.count,
    )
    print(f"Target timezone: {LOCAL_TIMEZONE}")
    print("Configured times: " + ", ".join(arguments.times))
    print("These are poll targets, not a guarantee of successful checks.")
    for slot in slots:
        print(slot.isoformat())
    return 0


def _report(arguments) -> int:
    state = OpportunityState(arguments.state_db)
    report = coverage_report(state)
    if arguments.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(
        "Club data status — {} — {}".format(
            report["generated_at"], report["timezone"]
        )
    )
    print(
        "Successful account checks in last 24h: {}/{}".format(
            report["successful_account_checks_last_24h"],
            report["daily_account_target"],
        )
    )
    if report["lock"]:
        print(
            "Run lock held by {} since {}".format(
                report["lock"]["run_id"], report["lock"]["acquired_at"]
            )
        )
    for account in report["accounts"]:
        age = (
            "never"
            if account["age_hours_since_success"] is None
            else f"{account['age_hours_since_success']:.1f}h"
        )
        stale = "STALE" if account["stale"] else "fresh"
        print(
            "{} ({}) — {} — last success {} — {}".format(
                account["organizer_id"],
                account["username"],
                account["check_status"],
                age,
                stale,
            )
        )
    if arguments.recent_runs:
        print("\nRecent runs:")
        for run in report["runs"][:arguments.recent_runs]:
            print(
                "{} {} {} {}".format(
                    run["run_id"], run["mode"], run["status"], run["started_at"]
                )
            )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Local club opportunity scheduler tools.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Plan or execute account checks.")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--offline", action="store_true")
    run_parser.add_argument("--accounts", nargs="+")
    run_parser.add_argument("--limit", type=int)
    run_parser.add_argument("--since", help="Optional ISO date/time boundary.")
    run_parser.add_argument("--scheduled", action="store_true", help="Run at most once for the latest due Toronto slot.")
    run_parser.add_argument("--times", nargs="+", default=["08:00", "12:00", "16:00", "20:00"])
    run_parser.add_argument(
        "--state-db",
        help="Local SQLite state path; defaults to data/opportunities/state.sqlite3.",
    )
    run_parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    run_parser.add_argument(
        "--max-posts-per-account", type=int, default=DEFAULT_MAX_POSTS
    )
    run_parser.add_argument("--max-scrolls", type=int, default=DEFAULT_MAX_SCROLLS)
    run_parser.add_argument(
        "--max-collected-per-account", type=int, default=DEFAULT_MAX_COLLECTED
    )
    run_parser.add_argument("--wait-seconds", type=int, default=300)

    schedule_parser = subparsers.add_parser(
        "schedule", help="Show upcoming local poll targets."
    )
    schedule_parser.add_argument(
        "--times", nargs="+", default=["08:00", "12:00", "16:00", "20:00"]
    )
    schedule_parser.add_argument("--count", type=int, default=8)

    report_parser = subparsers.add_parser("report", help="Report saved check coverage.")
    report_parser.add_argument("--state-db")
    report_parser.add_argument("--json", action="store_true")
    report_parser.add_argument("--recent-runs", type=int, default=5)

    unlock_parser = subparsers.add_parser(
        "recover-lock",
        help="Manually release an interrupted run lock by its exact run ID.",
    )
    unlock_parser.add_argument("--run-id", required=True)
    unlock_parser.add_argument("--state-db")

    arguments = parser.parse_args()
    try:
        if arguments.command == "run":
            exit_code = _run(arguments)
        elif arguments.command == "schedule":
            exit_code = _schedule(arguments)
        elif arguments.command == "report":
            exit_code = _report(arguments)
        else:
            state = OpportunityState(arguments.state_db)
            state.recover_abandoned_lock(arguments.run_id)
            print(f"Recovered interrupted run lock for {arguments.run_id}.")
            exit_code = 0
    except AccountCollectorUnavailable as error:
        parser.exit(2, f"{error}\n")
    except (OSError, ValueError) as error:
        parser.error(str(error))
        return
    sys.exit(exit_code)


if __name__ == "__main__":
    main()

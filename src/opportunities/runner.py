"""Local scheduler calculations and account-check orchestration."""

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.opportunities.state import OpportunityState, RunLockedError


ROOT = Path(__file__).resolve().parents[2]
ORGANIZERS_PATH = ROOT / "resources" / "organizers.json"
DEFAULT_SLOTS = ("08:00", "12:00", "16:00", "20:00")
LOCAL_TIMEZONE = "America/Toronto"
STALE_AFTER = timedelta(hours=24)


class AccountCollectorUnavailable(RuntimeError):
    """Raised when a live account scanner has not been configured."""


def load_enabled_organizers(path: Path | None = None) -> list[dict]:
    records = json.loads((path or ORGANIZERS_PATH).read_text(encoding="utf-8"))
    return sorted(
        (record for record in records if record["enabled"]),
        key=lambda record: (record["priority"], record["organizer_id"]),
    )


def parse_slots(values: list[str] | None = None) -> tuple[tuple[int, int], ...]:
    parsed = []
    for value in values or list(DEFAULT_SLOTS):
        try:
            hour_text, minute_text = value.split(":", maxsplit=1)
            hour, minute = int(hour_text), int(minute_text)
        except (ValueError, AttributeError) as error:
            raise ValueError(f"Invalid schedule slot {value!r}; use HH:MM.") from error
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid schedule slot {value!r}; use a valid local time.")
        if (hour, minute) in parsed:
            raise ValueError(f"Duplicate schedule slot: {value}")
        parsed.append((hour, minute))
    return tuple(sorted(parsed))


def next_schedule_slots(
    now: datetime | None = None,
    slots: list[str] | None = None,
    count: int = 4,
    timezone_name: str = LOCAL_TIMEZONE,
) -> list[datetime]:
    if count < 1:
        raise ValueError("count must be positive.")
    zone = ZoneInfo(timezone_name)
    if now is None:
        now = datetime.now(zone)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    else:
        now = now.astimezone(zone)

    daily_slots = parse_slots(slots)
    candidates = []
    local_day = now.date()
    days_checked = 0
    while len(candidates) < count:
        for hour, minute in daily_slots:
            candidate = datetime(
                local_day.year,
                local_day.month,
                local_day.day,
                hour,
                minute,
                tzinfo=zone,
            )
            if candidate > now:
                candidates.append(candidate)
                if len(candidates) == count:
                    break
        local_day += timedelta(days=1)
        days_checked += 1
        if days_checked > 366 and len(candidates) < count:
            raise ValueError("Could not calculate schedule slots.")
    return candidates


def _slot_id(scheduled_at: datetime) -> str:
    local = scheduled_at.astimezone(ZoneInfo(LOCAL_TIMEZONE))
    return f"{local:%Y-%m-%dT%H:%M}_{local.tzname()}"


def dry_run(
    accounts: list[dict] | None = None,
    limit: int | None = None,
    since: str | None = None,
) -> dict:
    selected = accounts if accounts is not None else load_enabled_organizers()
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive.")
        selected = selected[:limit]
    return {
        "status": "dry_run",
        "network_requests": 0,
        "account_count": len(selected),
        "accounts": [
            {"organizer_id": account["organizer_id"], "username": account["username"]}
            for account in selected
        ],
        "since": since,
        "message": "Planning only; no browser, network request, or state mutation occurred.",
    }


def run_account_checks(
    checker=None,
    accounts: list[dict] | None = None,
    limit: int | None = None,
    state: OpportunityState | None = None,
    max_attempts: int = 2,
    retry_delay_seconds: float = 1.0,
) -> dict:
    if checker is None:
        raise AccountCollectorUnavailable(
            "No live account scanner was supplied. No network request was made."
        )
    selected = accounts if accounts is not None else load_enabled_organizers()
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive.")
        selected = selected[:limit]
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive.")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds cannot be negative.")
    state = state or OpportunityState()
    run_id = uuid.uuid4().hex
    results = []
    overall_status = "complete"
    run_error = None

    state.create_run("scheduled", run_id)
    set_run_id = getattr(checker, "set_run_id", None)
    if callable(set_run_id):
        set_run_id(run_id)
    try:
        with state.run_lock(run_id):
            for account in selected:
                result = None
                last_error = None
                attempts = 0
                for attempt in range(1, max_attempts + 1):
                    attempts = attempt
                    check_id = state.record_account_attempt(
                        run_id, account["organizer_id"]
                    )
                    try:
                        result = checker(account)
                        if not isinstance(result, dict) or result.get("status") not in {
                            "success",
                            "partial",
                            "failed",
                            "blocked",
                            "requires_auth",
                            "skipped",
                        }:
                            raise RuntimeError(
                                "Account checker returned an invalid result status."
                            )
                    except (OSError, TimeoutError) as error:
                        last_error = error
                        state.finish_account_attempt(
                            check_id,
                            status="failed",
                            coverage_status="partial",
                            error=str(error),
                        )
                        if attempt < max_attempts:
                            time.sleep(retry_delay_seconds * (2 ** (attempt - 1)))
                            continue
                        break
                    except RuntimeError as error:
                        last_error = error
                        state.finish_account_attempt(
                            check_id,
                            status="failed",
                            coverage_status="partial",
                            error=str(error),
                        )
                        break
                    else:
                        break
                if result is None:
                    error_text = str(last_error) if last_error else "Account check returned no result."
                    results.append({
                        "organizer_id": account["organizer_id"],
                        "status": "failed",
                        "attempts": attempts,
                        "error": error_text,
                    })
                    overall_status = "partial"
                    continue
                status = result["status"]
                state.finish_account_attempt(
                    check_id,
                    status=status,
                    coverage_status=result.get("coverage_status", "unknown"),
                    discovered_count=result.get("discovered_count"),
                    collected_count=result.get("collected_count"),
                    error=result.get("error"),
                )
                results.append({
                    "organizer_id": account["organizer_id"],
                    "attempts": attempts,
                    **result,
                })
                if status not in {"success", "skipped"}:
                    overall_status = "partial"
                if result.get("stop_run"):
                    break
            finalize = getattr(checker, "finalize", None)
            if callable(finalize):
                finalize()
    except RunLockedError as error:
        overall_status = "failed"
        run_error = str(error)
    except (OSError, RuntimeError, TimeoutError) as error:
        overall_status = "failed"
        run_error = str(error)
    finally:
        state.finish_run(run_id, overall_status, run_error)
    return {
        "run_id": run_id,
        "status": overall_status,
        "error": run_error,
        "results": results,
    }


def claim_current_slot(
    state: OpportunityState,
    now: datetime | None = None,
    slots: list[str] | None = None,
) -> tuple[str, datetime] | None:
    zone = ZoneInfo(LOCAL_TIMEZONE)
    now = now or datetime.now(zone)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    local_now = now.astimezone(zone)
    due_slots = [
        datetime(
            local_now.year,
            local_now.month,
            local_now.day,
            hour,
            minute,
            tzinfo=zone,
        )
        for hour, minute in parse_slots(slots)
        if datetime(
            local_now.year,
            local_now.month,
            local_now.day,
            hour,
            minute,
            tzinfo=zone,
        ) <= local_now
    ]
    if not due_slots:
        return None
    due = max(due_slots)
    identifier = _slot_id(due)
    scheduled_at = due.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    if not state.claim_schedule_slot(identifier, scheduled_at):
        return None
    return identifier, due


def coverage_report(
    state: OpportunityState,
    organizers: list[dict] | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    now = now.astimezone(timezone.utc)
    base = state.report()
    latest = {item["organizer_id"]: item for item in base["accounts"]}
    successful = base.pop("successful_checks", [])
    last_successes = {}
    for check in successful:
        last_successes.setdefault(check["organizer_id"], check["succeeded_at"])
    account_rows = []
    for organizer in organizers if organizers is not None else load_enabled_organizers():
        check = latest.get(organizer["organizer_id"])
        last_success = last_successes.get(organizer["organizer_id"])
        age_hours = None
        if last_success:
            timestamp = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
            age_hours = max(0.0, (now - timestamp).total_seconds() / 3600)
        account_rows.append({
            "organizer_id": organizer["organizer_id"],
            "username": organizer["username"],
            "last_attempt_at": check["attempted_at"] if check else None,
            "last_success_at": last_success,
            "coverage_status": check["coverage_status"] if check else "not_checked",
            "check_status": check["status"] if check else "not_checked",
            "age_hours_since_success": round(age_hours, 2) if age_hours is not None else None,
            "stale": age_hours is None or age_hours > STALE_AFTER.total_seconds() / 3600,
        })
    base["accounts"] = account_rows
    base["timezone"] = LOCAL_TIMEZONE
    enabled_ids = {account["organizer_id"] for account in account_rows}
    base["successful_account_checks_last_24h"] = sum(
        1 for check in successful
        if check["organizer_id"] in enabled_ids
        and now - STALE_AFTER <= datetime.fromisoformat(check["succeeded_at"].replace("Z", "+00:00")) <= now
    )
    base["accounts_with_success_last_24h"] = sum(not account["stale"] for account in account_rows)
    base["daily_account_target"] = len(account_rows) * len(DEFAULT_SLOTS)
    local_today = now.astimezone(ZoneInfo(LOCAL_TIMEZONE)).date()
    base["daily_successful_checks"] = []
    for days_ago in range(6, -1, -1):
        day = local_today - timedelta(days=days_ago)
        counts = {identifier: 0 for identifier in enabled_ids}
        for check in successful:
            if check["organizer_id"] in counts and datetime.fromisoformat(
                check["succeeded_at"].replace("Z", "+00:00")
            ).astimezone(ZoneInfo(LOCAL_TIMEZONE)).date() == day:
                counts[check["organizer_id"]] += 1
        base["daily_successful_checks"].append({
            "date": day.isoformat(), "successful_checks": sum(counts.values()),
            "target_checks": base["daily_account_target"],
            "accounts_below_four_checks": sorted(identifier for identifier, count in counts.items() if count < len(DEFAULT_SLOTS)),
            "current_day": day == local_today,
        })
    return base

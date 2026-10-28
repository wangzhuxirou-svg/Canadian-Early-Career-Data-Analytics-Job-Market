from datetime import datetime

import pytest

from src.opportunities.runner import (
    AccountCollectorUnavailable,
    claim_current_slot,
    coverage_report,
    dry_run,
    next_schedule_slots,
    run_account_checks,
)
from src.opportunities.state import OpportunityState, RunLockedError


def _raw_post(
    content_hash="a" * 64,
    collection_status="complete",
    fetched_at="2026-10-28T04:00:00Z",
):
    return {
        "source_post_id": "post_1",
        "organizer_id": "club_1",
        "content_hash": content_hash,
        "first_seen_at": "2026-10-27T04:00:00Z",
        "last_seen_at": fetched_at,
        "last_fetched_at": fetched_at,
        "collection_status": collection_status,
    }


def test_state_retains_first_seen_and_deduplicates_unchanged_content(tmp_path):
    state = OpportunityState(tmp_path / "state.sqlite3")
    first_run = state.create_run("test", "run-1")
    first = state.record_snapshot(
        first_run, _raw_post(), tmp_path / "snapshots" / "first.json"
    )
    state.finish_run(first_run, "complete")
    second_run = state.create_run("test", "run-2")
    second = state.record_snapshot(
        second_run,
        _raw_post(fetched_at="2026-10-28T16:00:00Z"),
        tmp_path / "snapshots" / "second.json",
    )
    state.finish_run(second_run, "complete")

    report = state.report()
    assert first["content_revision"] == second["content_revision"] == 1
    assert first["content_changed"] is True
    assert second["content_changed"] is False
    assert report["posts"][0]["first_seen_at"] == "2026-10-27T04:00:00Z"
    assert len(report["posts"]) == 1
    with state._connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM post_history").fetchone()[0] == 2


def test_content_change_advances_revision_but_partial_does_not_replace_current(
    tmp_path,
):
    state = OpportunityState(tmp_path / "state.sqlite3")
    run_one = state.create_run("test", "run-1")
    state.record_snapshot(run_one, _raw_post(), tmp_path / "complete.json")
    state.finish_run(run_one, "complete")
    run_two = state.create_run("test", "run-2")
    partial = state.record_snapshot(
        run_two,
        _raw_post("b" * 64, "partial", "2026-10-28T08:00:00Z"),
        tmp_path / "partial.json",
    )
    state.finish_run(run_two, "partial")

    report = state.report()

    assert partial["content_revision"] == 2
    assert partial["content_changed"] is True
    assert partial["became_current"] is False
    assert report["posts"][0]["content_hash"] == "a" * 64
    assert report["posts"][0]["snapshot_path"].endswith("complete.json")


def test_run_lock_rejects_concurrent_run_and_releases_when_finished(tmp_path):
    state = OpportunityState(tmp_path / "state.sqlite3")
    run_one = state.create_run("test", "run-1")
    run_two = state.create_run("test", "run-2")

    with state.run_lock(run_one):
        assert state.get_lock()["run_id"] == run_one
        with pytest.raises(RunLockedError):
            with state.run_lock(run_two):
                pass

    assert state.get_lock() is None
    state.finish_run(run_one, "complete")
    state.finish_run(run_two, "failed")


def test_interrupted_lock_requires_matching_manual_recovery(tmp_path):
    state = OpportunityState(tmp_path / "state.sqlite3")
    run_id = state.create_run("test", "abandoned-run")
    with state._connection() as connection:
        connection.execute(
            "INSERT INTO run_lock (lock_name, run_id, acquired_at) VALUES (?, ?, ?)",
            ("opportunities", run_id, "2026-10-28T04:00:00Z"),
        )

    with pytest.raises(ValueError, match="does not own"):
        state.recover_abandoned_lock("different-run")
    assert state.get_lock()["run_id"] == run_id

    state.recover_abandoned_lock(run_id)

    assert state.get_lock() is None
    assert state.report()["runs"][0]["status"] == "failed"


def test_account_attempts_record_success_and_failure_without_erasing_history(
    tmp_path,
):
    state = OpportunityState(tmp_path / "state.sqlite3")
    run_id = state.create_run("test", "run-1")
    success = state.record_account_attempt(run_id, "club_1")
    state.finish_account_attempt(
        success, "success", "complete", discovered_count=4, collected_count=3
    )
    failed = state.record_account_attempt(run_id, "club_2")
    state.finish_account_attempt(
        failed,
        "requires_auth",
        "partial",
        error="Manual login required.",
    )
    state.finish_run(run_id, "partial")

    report = state.report()
    by_id = {row["organizer_id"]: row for row in report["accounts"]}
    assert by_id["club_1"]["succeeded_at"] is not None
    assert by_id["club_1"]["discovered_count"] == 4
    assert by_id["club_2"]["succeeded_at"] is None
    assert by_id["club_2"]["status"] == "requires_auth"
    assert state.failed_account_count() == 1


def test_partial_account_attempt_does_not_count_as_fresh_success(tmp_path):
    state = OpportunityState(tmp_path / "state.sqlite3")
    run_id = state.create_run("test", "run-1")
    check_id = state.record_account_attempt(run_id, "club_1")
    state.finish_account_attempt(check_id, "partial", "partial")
    state.finish_run(run_id, "partial")

    report = coverage_report(
        state,
        organizers=[{"organizer_id": "club_1", "username": "club.one"}],
    )

    assert report["accounts"][0]["last_success_at"] is None
    assert report["accounts"][0]["stale"] is True
    assert report["successful_account_checks_last_24h"] == 0


def test_toronto_schedule_respects_dst_and_does_not_duplicate_slots():
    before_spring_forward = datetime.fromisoformat("2026-03-08T01:30:00-05:00")
    slots = next_schedule_slots(before_spring_forward, count=2)

    assert slots[0].isoformat() == "2026-03-08T08:00:00-04:00"
    assert slots[1].isoformat() == "2026-03-08T12:00:00-04:00"


def test_schedule_slot_claim_is_idempotent_and_current_time_based(tmp_path):
    state = OpportunityState(tmp_path / "state.sqlite3")
    now = datetime.fromisoformat("2026-10-28T12:05:00-04:00")

    first = claim_current_slot(state, now)
    second = claim_current_slot(state, now)

    assert first is not None
    assert first[1].isoformat() == "2026-10-28T12:00:00-04:00"
    assert second is None


def test_offline_dry_run_never_mutates_state_or_marks_accounts_success(tmp_path):
    state = OpportunityState(tmp_path / "state.sqlite3")
    result = dry_run(
        [{"organizer_id": "club_1", "username": "club.one"}],
        limit=1,
        since="2026-10-14",
    )

    assert result["network_requests"] == 0
    assert result["account_count"] == 1
    assert result["since"] == "2026-10-14"
    assert state.report()["runs"] == []
    assert state.report()["accounts"] == []


def test_live_runner_fails_explicitly_without_a_scanner():
    with pytest.raises(AccountCollectorUnavailable, match="No network request was made"):
        run_account_checks()


def test_runner_records_per_account_failures_and_continues(tmp_path):
    state = OpportunityState(tmp_path / "state.sqlite3")
    accounts = [
        {"organizer_id": "club_1", "username": "club.one"},
        {"organizer_id": "club_2", "username": "club.two"},
    ]

    def checker(account):
        if account["organizer_id"] == "club_1":
            raise RuntimeError("Temporary page load failure.")
        return {
            "status": "success",
            "coverage_status": "complete",
            "discovered_count": 1,
            "collected_count": 1,
        }

    result = run_account_checks(checker, accounts=accounts, state=state)

    assert result["status"] == "partial"
    assert [row["status"] for row in result["results"]] == ["failed", "success"]
    report = state.report()
    assert len(report["accounts"]) == 2
    assert report["lock"] is None
    assert report["runs"][0]["status"] == "partial"


def test_runner_retries_one_transient_timeout_and_records_each_attempt(tmp_path):
    state = OpportunityState(tmp_path / "state.sqlite3")
    attempts = []

    def checker(account):
        attempts.append(account["organizer_id"])
        if len(attempts) == 1:
            raise TimeoutError("Temporary timeout.")
        return {
            "status": "success",
            "coverage_status": "complete",
            "discovered_count": 1,
            "collected_count": 1,
        }

    result = run_account_checks(
        checker,
        accounts=[{"organizer_id": "club_1", "username": "club.one"}],
        state=state,
        retry_delay_seconds=0,
    )

    assert result["status"] == "complete"
    assert result["results"][0]["attempts"] == 2
    with state._connection() as connection:
        rows = connection.execute(
            "SELECT status FROM account_checks ORDER BY check_id"
        ).fetchall()
    assert [row["status"] for row in rows] == ["failed", "success"]


def test_runner_stops_all_accounts_after_access_restriction(tmp_path):
    state = OpportunityState(tmp_path / "state.sqlite3")
    accounts = [
        {"organizer_id": "club_1", "username": "club.one"},
        {"organizer_id": "club_2", "username": "club.two"},
    ]
    checked = []

    def checker(account):
        checked.append(account["organizer_id"])
        return {
            "status": "blocked",
            "coverage_status": "blocked",
            "discovered_count": 0,
            "collected_count": 0,
            "stop_run": True,
            "error": "Access restriction detected.",
        }

    result = run_account_checks(checker, accounts=accounts, state=state)

    assert checked == ["club_1"]
    assert result["status"] == "partial"
    assert result["results"][0]["status"] == "blocked"
    assert len(state.report()["accounts"]) == 1

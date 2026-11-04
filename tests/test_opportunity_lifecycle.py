"""Offline regression tests for revision, OCR and scheduler handoff failures."""

import copy
import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.opportunities import cli, collect, ocr, scan
from src.opportunities.drafts import source_is_current, store_draft, track_source
from src.opportunities.extract import build_draft
from src.opportunities.runner import coverage_report
from src.opportunities.state import OpportunityState


def raw_post():
    return {
        "source": "instagram", "source_post_id": "lifecycle", "organizer_id": "ds3_utsc",
        "source_url": "https://www.instagram.com/p/lifecycle/",
        "posted_at": "2026-10-28T12:00:00Z", "caption_raw": "Register for our Python workshop on November 20, 2026",
        "caption_status": "available", "media": [], "media_completeness": "complete",
        "first_seen_at": "2026-10-28T12:00:00Z", "last_seen_at": "2026-10-28T12:00:00Z",
        "last_fetched_at": "2026-10-28T12:00:00Z", "content_hash": "a" * 64,
        "collection_status": "complete", "errors": [],
    }


def save_raw(directory, post):
    (directory / "lifecycle.json").write_text(json.dumps(post), encoding="utf-8")


def test_changed_source_invalidates_approval_before_extraction_and_archives(tmp_path):
    post = raw_post()
    save_raw(tmp_path, post)
    draft = build_draft(post)
    store_draft(tmp_path, post, draft, track_source(tmp_path, post, 1))
    original = json.loads((tmp_path / "drafts/lifecycle.json").read_text())
    assert source_is_current(tmp_path, original["opportunity"])
    ledger = tmp_path / "review_overrides" / "saved.json"
    ledger.parent.mkdir()
    ledger.write_text('{"review_status":"approved"}')

    post["content_hash"] = "b" * 64
    save_raw(tmp_path, post)
    version = track_source(tmp_path, post, 2)
    assert not source_is_current(tmp_path, original["opportunity"])
    store_draft(tmp_path, post, draft, version)
    updated = json.loads((tmp_path / "drafts/lifecycle.json").read_text())
    assert updated["opportunity"]["content_revision"] == 2
    assert updated["opportunity"]["change_pending"] is True
    assert updated["opportunity"]["review_status"] == "needs_review"
    assert json.loads((tmp_path / "draft_history/lifecycle/1.json").read_text()) == original
    assert json.loads(ledger.read_text())["review_status"] == "approved"


def test_unchanged_source_preserves_draft_and_changed_ocr_requeues(tmp_path):
    post = raw_post()
    save_raw(tmp_path, post)
    draft = build_draft(post)
    store_draft(tmp_path, post, draft, track_source(tmp_path, post))
    original_bytes = (tmp_path / "drafts/lifecycle.json").read_bytes()
    assert store_draft(tmp_path, post, build_draft(post), track_source(tmp_path, post)) == "existing_draft_preserved"
    assert (tmp_path / "drafts/lifecycle.json").read_bytes() == original_bytes
    changed = copy.deepcopy(draft)
    changed["opportunity"]["location"] = "IC 200"
    store_draft(tmp_path, post, changed, track_source(tmp_path, post))
    assert json.loads((tmp_path / "drafts/lifecycle.json").read_text())["opportunity"]["content_revision"] == 2


@pytest.mark.parametrize("reason", ["ocr_failed", "filtered_expired", "no_opportunity_evidence", "incomplete_snapshot"])
def test_failed_or_filtered_source_cannot_remain_publishable(tmp_path, reason):
    post = raw_post()
    save_raw(tmp_path, post)
    draft = build_draft(post)
    store_draft(tmp_path, post, draft, track_source(tmp_path, post))
    version = track_source(tmp_path, post)
    store_draft(tmp_path, post, None, version, reason)
    assert not source_is_current(tmp_path, draft["opportunity"])


def test_legacy_draft_requires_new_revision(tmp_path):
    post = raw_post()
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts/lifecycle.json").write_text(json.dumps(build_draft(post)))
    assert track_source(tmp_path, post, 1)["content_revision"] == 2


def test_coverage_retains_success_after_later_partial_and_counts_checks(tmp_path):
    state = OpportunityState(tmp_path / "state.sqlite3")
    run = state.create_run()
    for status, coverage in [("success", "complete"), ("success", "complete"), ("partial", "partial")]:
        check = state.record_account_attempt(run, "club")
        state.finish_account_attempt(check, status, coverage)
    report = coverage_report(state, [{"organizer_id": "club", "username": "club"}])
    assert report["accounts"][0]["check_status"] == "partial"
    assert report["accounts"][0]["last_success_at"] is not None
    assert report["accounts"][0]["stale"] is False
    assert report["successful_account_checks_last_24h"] == 2
    assert report["accounts_with_success_last_24h"] == 1
    assert report["daily_account_target"] == 4


@pytest.mark.parametrize("status, expected", [("complete", 0), ("partial", 1), ("failed", 2)])
def test_run_exit_codes(monkeypatch, tmp_path, status, expected):
    monkeypatch.setattr(cli, "_selected_accounts", lambda *args: [])
    monkeypatch.setattr(cli, "run_live_scan", lambda **kwargs: {"status": status})
    args = Namespace(accounts=None, limit=None, scheduled=False, offline=False,
                     dry_run=False, since=None, state_db=tmp_path / "state.sqlite3",
                     lookback_days=30, max_posts_per_account=24, max_scrolls=6,
                     max_collected_per_account=5, wait_seconds=0)
    assert cli._run(args) == expected


def test_scanner_reads_per_snapshot_ocr_failures_without_download(monkeypatch, tmp_path):
    python = tmp_path / "python.exe"
    python.touch()
    for name in ("craft_mlt_25k.pth", "english_g2.pth"):
        (tmp_path / name).touch()
    monkeypatch.setattr(scan, "OCR_PYTHON", python)
    monkeypatch.setattr(scan, "OCR_MODEL_DIR", tmp_path)
    def fake_run(command, **kwargs):
        assert "--offline" in command and "--json" in command
        return SimpleNamespace(returncode=1, stdout=json.dumps({"snapshots": {
            "a.json": {"status": "low_confidence"}, "b.json": {"status": "failed"}}}), stderr="")
    monkeypatch.setattr(scan.subprocess, "run", fake_run)
    result = scan.InstagramAccountScanner._run_local_ocr([Path("a.json"), Path("b.json")])
    assert result["a.json"]["status"] == "low_confidence"
    assert result["b.json"]["status"] == "failed"


def test_ocr_cli_reports_partial_results_and_nonzero(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ocr", "--snapshots", "a.json", "b.json", "--offline", "--json"])
    monkeypatch.setattr(ocr, "_load_reader", lambda offline: (object(), "test") if offline else pytest.fail("Must be offline"))
    monkeypatch.setattr(ocr, "process_snapshot", lambda path, *args: [{"status": "success" if str(path) == "a.json" else "corrupt"}])
    assert ocr.main() == 1
    reports = json.loads(capsys.readouterr().out)["snapshots"]
    assert reports["a.json"]["status"] == "complete"
    assert reports["b.json"]["status"] == "failed"


@pytest.mark.parametrize("incomplete, ocr_status", [(True, "complete"), (False, "low_confidence"), (False, "failed"), (False, "empty")])
def test_full_scanner_never_reports_partial_input_as_complete(monkeypatch, tmp_path, incomplete, ocr_status):
    post = raw_post()
    post["media"] = [{"type": "image", "download_status": "downloaded", "sha256": "b" * 64}]
    if incomplete:
        post["collection_status"] = "partial"
    monkeypatch.setattr(scan, "DATA_DIR", tmp_path)
    monkeypatch.setattr(scan, "discover_profile_posts", lambda *args, **kwargs: ([post["source_url"]], True))
    monkeypatch.setattr(scan, "_detect_access_block", lambda page: None)
    monkeypatch.setattr(scan, "_embedded_post_data", lambda *args: {})
    monkeypatch.setattr(scan, "_caption_and_post_data", lambda *args: (post["caption_raw"], {}))
    monkeypatch.setattr(scan, "_posted_at", lambda *args: datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(scan, "_is_image_post", lambda *args: True)
    monkeypatch.setattr(scan, "collect_post_from_page", lambda *args: post)
    monkeypatch.setattr(scan, "_post_evidence_text", lambda *args: post["caption_raw"])
    monkeypatch.setattr(scan, "_draft_is_expired", lambda *args, **kwargs: False)
    def save(post, **kwargs):
        save_raw(tmp_path, post)
        return tmp_path / "lifecycle.json"
    monkeypatch.setattr(scan, "save_post_snapshot", save)
    monkeypatch.setattr(scan, "build_draft", lambda post: build_draft({**post, "media": []}))
    scanner = scan.InstagramAccountScanner()
    scanner.page = SimpleNamespace(url="https://www.instagram.com/ds3/", goto=lambda *args, **kwargs: None,
                                   wait_for_timeout=lambda *args: None,
                                   locator=lambda selector: SimpleNamespace(count=lambda: 0, inner_text=lambda **kwargs: "Profile"))
    monkeypatch.setattr(scanner, "_run_local_ocr", lambda paths: {str(paths[0]): {"status": ocr_status}})
    result = scanner({"organizer_id": "ds3_utsc", "username": "ds3", "profile_url": "https://www.instagram.com/ds3/"})
    assert result["status"] == "partial"
    assert result["coverage_status"] == "partial"
    assert result["collected_posts"][0]["ocr_status"] == ocr_status
    if incomplete or ocr_status == "failed":
        # A usable caption now survives media failure, but is not auto-cleared.
        assert (tmp_path / "drafts/lifecycle.json").exists()
        assert result["collected_posts"][0]["draft_status"] == "needs_review"


def test_scheduled_cli_claims_slot_once_without_second_scan(monkeypatch, tmp_path):
    from src.opportunities.runner import claim_current_slot
    monkeypatch.setattr(cli, "_selected_accounts", lambda *args: [])
    monkeypatch.setattr(cli, "claim_current_slot", lambda state, **kwargs: claim_current_slot(
        state, datetime.fromisoformat("2026-10-28T12:01:00-04:00")))
    scans = []
    monkeypatch.setattr(cli, "run_live_scan", lambda **kwargs: scans.append(1) or {"status": "complete"})
    args = Namespace(accounts=None, limit=None, scheduled=True, times=None, offline=False,
                     dry_run=False, since=None, state_db=tmp_path / "state.sqlite3",
                     lookback_days=30, max_posts_per_account=24, max_scrolls=6,
                     max_collected_per_account=5, wait_seconds=1)
    assert cli._run(args) == 0
    assert cli._run(args) == 0
    assert scans == [1]


def test_retry_snapshots_are_immutable_and_partial_preserves_current(monkeypatch, tmp_path):
    monkeypatch.setattr(collect, "DATA_DIR", tmp_path)
    first = raw_post()
    saved = collect.save_post_snapshot(first, snapshot_dir=tmp_path / "snapshots/run")
    original = saved.read_bytes()
    partial = {**first, "content_hash": "b" * 64, "collection_status": "partial"}
    retry = collect.save_post_snapshot(partial, snapshot_dir=tmp_path / "snapshots/run")
    assert retry != saved
    assert saved.read_bytes() == original
    assert json.loads(retry.read_text())["collection_status"] == "partial"
    assert json.loads((tmp_path / "lifecycle.json").read_text())["content_hash"] == first["content_hash"]

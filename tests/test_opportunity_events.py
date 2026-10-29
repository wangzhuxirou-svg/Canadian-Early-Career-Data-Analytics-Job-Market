"""Regression coverage for real failure patterns, using offline synthetic posts."""

import csv
import json
from datetime import datetime, timezone

import pytest

from src.opportunities import review
from src.opportunities.drafts import atomic_json, source_is_current
from src.opportunities.extract import build_draft
from src.opportunities.scan import opportunity_relevance
from src.opportunities.temporal import clock_range, deadline_clock
from src.opportunities.triage import process_local_post, reprocess

NOW = datetime(2026, 10, 28, 6, tzinfo=timezone.utc)


def raw(post_id, caption, posted="2026-10-23T16:00:00Z", organizer="ds3_utsc"):
    return {"source": "instagram", "source_post_id": post_id, "organizer_id": organizer,
            "source_url": f"https://www.instagram.com/p/{post_id}/", "posted_at": posted,
            "caption_raw": caption, "caption_status": "available", "media": [], "media_completeness": "complete",
            "first_seen_at": "2026-10-28T05:00:00Z", "last_seen_at": "2026-10-28T05:00:00Z",
            "last_fetched_at": "2026-10-28T05:00:00Z", "content_hash": "a" * 64,
            "collection_status": "complete", "errors": []}


def save(directory, *posts):
    for post in posts:
        atomic_json(directory / f"{post['source_post_id']}.json", post)


def test_thanks_for_applied_is_excluded_even_when_old_poster_says_apply_now(tmp_path):
    post = raw("thanks", "A HUGE THANK YOU to everyone who applied to be an associate with us this year! We'll be getting in touch soon.")
    post["media"] = [{"position": 1, "type": "image", "source_url": "https://cdn.example/a.jpg", "local_path": "a.jpg",
                      "sha256": "b" * 64, "width": 100, "height": 100, "download_status": "downloaded"}]
    atomic_json(tmp_path / "ocr/a.json", {"image_hash": "b" * 64, "text": "ASSOCIATE HIRING! APPLY NOW", "status": "success"})
    save(tmp_path, post)
    decision = process_local_post(tmp_path, post, NOW)
    assert decision["decision"] == "excluded"
    assert decision["reasons"] == ["retrospective_or_closed_call"]
    assert opportunity_relevance(post["caption_raw"], True) == "exclude"


@pytest.mark.parametrize("caption", [
    "Save the date: November 25 for our Data Analytics Case Competition. More details coming soon!",
    "Our annual Data Analytics Case Competition is back. Stay tuned for more!",
])
def test_teasers_with_future_dates_are_excluded(tmp_path, caption):
    post = raw("teaser", caption)
    save(tmp_path, post)
    decision = process_local_post(tmp_path, post, NOW)
    assert decision["decision"] == "excluded"
    assert decision["reasons"] == ["announcement_only"]


def test_teaser_words_do_not_exclude_open_registration(tmp_path):
    post = raw("open", "Python Workshop\nDate: November 25, 2026\nSave the date! Register now through our bio. More details coming soon!")
    save(tmp_path, post)
    assert process_local_post(tmp_path, post, NOW)["decision"] == "auto_candidate"


def test_ambassador_is_retained_for_review_without_inventing_deadline(tmp_path):
    post = raw("ambassador", "Join the Herd! Become a Campus Brand Ambassador!\nLearn more and apply now through the link in bio.")
    save(tmp_path, post)
    result = process_local_post(tmp_path, post, NOW)
    assert result["decision"] == "needs_review"
    assert result["reasons"] == ["missing_deadline_date"]


@pytest.mark.parametrize("text,start,end", [
    ("9 AM – 5 PM", "14:00:00Z", "22:00:00Z"),
    ("09:00-17:00", "14:00:00Z", "22:00:00Z"),
    ("3–5 PM", "20:00:00Z", "22:00:00Z"),
    ("5:15 - 8:00 p.m.", "22:15:00Z", "01:00:00Z"),
])
def test_clock_ranges_preserve_toronto_local_times(text, start, end):
    actual = clock_range(text, "2026-11-15")
    assert actual[0].endswith(start) and actual[1].endswith(end)


def test_deadline_clock_keeps_pm_after_at_and_dst_offsets():
    assert deadline_clock("Register by October 21 at 11:59 PM", "2026-10-21") == "2026-10-22T03:59:00Z"
    assert deadline_clock("Deadline: November 21 at 11:59 PM", "2026-11-21") == "2026-11-22T04:59:00Z"
    assert deadline_clock("Closes October 26 at noon", "2026-10-26") == "2026-10-26T16:00:00Z"


def test_date_without_clock_stays_date_only_and_ambiguous_clocks_are_not_guessed():
    draft = build_draft(raw("dateonly", "Python Workshop\nDate: November 15, 2026\nRegister now."))
    assert draft["opportunity"]["start_at"] is None
    assert draft["opportunity"]["start_precision"] == "date"
    assert clock_range("October 16-23", "2026-10-16") is None
    with pytest.raises(ValueError):
        clock_range("01:15-01:45", "2026-11-01")
    with pytest.raises(ValueError):
        clock_range("9 AM–5 PM PST", "2026-11-15")


def test_caption_date_wins_over_carousel_pricing_windows(tmp_path):
    post = raw("pricing", "UTSFC 2026\nJoin us on November 15 at Hart House. Registration link in bio.")
    post["media"] = [{"position": 1, "type": "image", "source_url": None, "local_path": "poster.jpg", "sha256": "b" * 64,
                      "width": 100, "height": 100, "download_status": "downloaded"}]
    atomic_json(tmp_path / "poster.json", {"image_hash": "b" * 64, "status": "success", "text": "Early bird\nOctober 9\nOctober 16\nRegular tickets\nOctober 17\nOctober 30\nREGISTRATION\nCLOSES\nOCTOBER 30, 2026"})
    draft = build_draft(post, tmp_path)
    assert draft["opportunity"]["start_date"] == "2026-11-15"
    assert draft["opportunity"]["deadline_date"] == "2026-10-30"
    assert not draft["conflicts"]


def event_posts():
    return [raw("original", "UTSFC 2026 is open for registration!\nJoin us on November 15, 2026 at Hart House. Link in bio.", "2026-10-09T18:00:00Z"),
            raw("reminder", "The runway is ready for UTSFC 2026!\nJoin us for our case competition.\n📅 November 15, 2026 | 9 AM – 5 PM\n📍 Hart House\nRegistration closes NEXT FRIDAY!", "2026-10-23T16:08:50Z"),
            raw("lastcall", "UTSFC 2026!\nOnly 3 days left to sign up! Register now through our bio.", "2026-10-27T23:00:00Z")]


def test_same_event_is_one_card_with_all_sources_and_time_range(tmp_path):
    save(tmp_path, *event_posts())
    result = reprocess(tmp_path, NOW)
    assert result["counts"] == {"auto_candidate": 1, "merged_duplicate": 2}
    group = json.loads((tmp_path / "event_groups/original.json").read_text())
    opportunity = group["draft"]["opportunity"]
    assert opportunity["source_post_ids"] == ["original", "reminder", "lastcall"]
    assert opportunity["start_at"] == "2026-11-15T14:00:00Z"
    assert opportunity["end_at"] == "2026-11-15T22:00:00Z"
    assert opportunity["deadline_date"] == "2026-10-30"
    review._validator("opportunity-draft").validate(group["draft"])
    page = (tmp_path / "triage-report.html").read_text(encoding="utf-8")
    assert page.count("<article>") == 1
    assert "09:00–17:00" in page
    assert group["field_sources"]["start_at"] == "reminder"


def test_group_rerun_is_idempotent_and_member_changes_invalidate_old_group(tmp_path):
    posts = event_posts()
    save(tmp_path, *posts)
    reprocess(tmp_path, NOW)
    group = json.loads((tmp_path / "event_groups/original.json").read_text())
    reprocess(tmp_path, NOW)
    again = json.loads((tmp_path / "event_groups/original.json").read_text())
    assert again["content_revision"] == group["content_revision"]
    posts[1]["content_hash"] = "b" * 64
    posts[1]["caption_raw"] = posts[1]["caption_raw"].replace("9 AM", "10 AM")
    save(tmp_path, posts[1])
    assert not source_is_current(tmp_path, group["draft"]["opportunity"])
    reprocess(tmp_path, NOW)
    changed = json.loads((tmp_path / "event_groups/original.json").read_text())
    assert changed["content_revision"] > group["content_revision"]
    assert (tmp_path / "event_group_history/original" / f"{group['content_revision']}.json").exists()


@pytest.mark.parametrize("second,organizer", [
    ("Python Workshop\nDate: November 21, 2026\nRegister now.", "ds3_utsc"),
    ("Python Workshop\nDate: November 20, 2026\nRegister now.", "create_uoft"),
    ("SQL Workshop\nDate: November 20, 2026\nRegister now.", "ds3_utsc"),
])
def test_distinct_dates_organizers_and_workshops_do_not_merge(tmp_path, second, organizer):
    a = raw("a", "Python Workshop\nDate: November 20, 2026\nRegister now.")
    b = raw("b", second, organizer=organizer)
    save(tmp_path, a, b)
    report = reprocess(tmp_path, NOW)
    assert "merged_duplicate" not in report["counts"]


def test_manual_queue_shows_one_group_and_stale_group_csv_is_rejected(tmp_path, monkeypatch):
    save(tmp_path, *event_posts())
    reprocess(tmp_path, NOW)
    monkeypatch.setattr(review, "DATA_DIR", tmp_path)
    monkeypatch.setattr(review, "DRAFTS_DIR", tmp_path / "drafts")
    monkeypatch.setattr(review, "OVERRIDES_DIR", tmp_path / "review_overrides")
    csv_path, _ = review.export_review(tmp_path / "review", include_auto=True)
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["draft_id"] == "event_original"
    row = rows[0]
    row.update(review_status="approved", reviewer="tester", reviewed_at="2026-10-28T06:00:00Z", reason="Checked sources")
    changed = event_posts()[1]
    changed["content_hash"] = "b" * 64
    changed["caption_raw"] += "\nLocation: New venue"
    save(tmp_path, changed)
    reprocess(tmp_path, NOW)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    with pytest.raises(review.ReviewImportError):
        review.import_review(csv_path)


def test_optional_missing_facts_are_warnings_but_real_date_conflicts_need_review(tmp_path):
    post = raw("partial", "Python Workshop\nJoin us for a practical workshop. Register now.")
    save(tmp_path, post)
    result = process_local_post(tmp_path, post, NOW)
    assert result["decision"] == "auto_candidate"
    assert "missing_start_date" in result["warnings"]
    post["caption_raw"] += "\nDate: November 20, 2026\nDate: November 21, 2026"
    post["content_hash"] = "b" * 64
    save(tmp_path, post)
    assert process_local_post(tmp_path, post, NOW)["decision"] == "needs_review"


def test_later_agreeing_posts_can_update_deadline_without_losing_original_sources(tmp_path):
    posts = event_posts()
    posts[0]["caption_raw"] += "\nRegistration closes October 26, 2026."
    save(tmp_path, *posts)
    report = reprocess(tmp_path, NOW)
    group = json.loads((tmp_path / "event_groups/original.json").read_text())
    assert group["draft"]["opportunity"]["deadline_date"] == "2026-10-30"
    assert group["updates"]
    assert report["counts"] == {"auto_candidate": 1, "merged_duplicate": 2}
    assert "October 26" in json.loads((tmp_path / "original.json").read_text())["caption_raw"]


def test_older_discovered_post_does_not_change_existing_canonical_id(tmp_path):
    save(tmp_path, *event_posts())
    reprocess(tmp_path, NOW)
    older = raw("older", "UTSFC 2026 is open for registration!\nJoin us on November 15, 2026. Register now.", "2026-10-01T18:00:00Z")
    save(tmp_path, older)
    reprocess(tmp_path, NOW)
    group = json.loads((tmp_path / "event_groups/original.json").read_text())
    assert group["active"] and group["canonical_post_id"] == "original"
    assert "older" in group["draft"]["opportunity"]["source_post_ids"]


@pytest.mark.parametrize("status,expected", [("approved", "human_approved"), ("rejected", "excluded")])
def test_review_decisions_survive_rebuild_and_appear_in_correct_output(tmp_path, status, expected):
    post = raw("reviewed", "Python Workshop\nDate: November 20, 2026\nRegister now.")
    save(tmp_path, post)
    reprocess(tmp_path, NOW)
    draft = json.loads((tmp_path / "drafts/reviewed.json").read_text())
    opportunity = draft["opportunity"]
    atomic_json(tmp_path / "review_overrides" / f"{opportunity['opportunity_id']}.json", {
        "opportunity_id": opportunity["opportunity_id"], "content_revision": opportunity["content_revision"],
        "review_status": status, "field_overrides": {"title": "Human checked workshop"},
        "updated_at": "2026-10-28T06:00:00Z", "updated_by": "tester", "history": []})
    report = reprocess(tmp_path, NOW)
    assert report["counts"] == {expected: 1}
    candidates = json.loads((tmp_path / "publishable-candidates.json").read_text())["candidates"]
    if status == "approved":
        assert candidates[0]["opportunity"]["title"] == "Human checked workshop"
        assert candidates[0]["opportunity"]["review_status"] == "approved"
    else:
        assert candidates == []


def test_clear_caption_survives_current_ocr_failure_as_warning(tmp_path):
    post = raw("ocrfault", "Python Workshop\nDate: November 20, 2026\nRegister now.")
    save(tmp_path, post)
    process_local_post(tmp_path, post, NOW)
    path = tmp_path / "draft_versions/ocrfault.json"
    version = json.loads(path.read_text())
    version["processing_issues"] = ["ocr_failed_this_run"]
    atomic_json(path, version)
    result = process_local_post(tmp_path, post, NOW)
    assert result["decision"] == "auto_candidate"
    assert "ocr_failed_this_run" in result["warnings"]


@pytest.mark.parametrize("caption", ["YEARLY GENERAL MEETING", "Executive meeting for current members", "General meeting open to all members", "Members-only meeting. Register now."])
def test_internal_meetings_are_excluded_from_collection_and_triage(tmp_path, caption):
    post = raw("internal", caption)
    save(tmp_path, post)
    assert opportunity_relevance(caption, True) == "exclude"
    decision = process_local_post(tmp_path, post, NOW)
    assert decision["decision"] == "excluded"
    assert decision["reasons"] == ["internal_club_meeting"]


def test_explicitly_public_general_meeting_is_retained(tmp_path):
    post = raw("public", "Yearly General Meeting\nOpen to all students!\nDate: November 20, 2026\nRegister now via link in bio.")
    save(tmp_path, post)
    assert process_local_post(tmp_path, post, NOW)["decision"] == "auto_candidate"


def test_recovered_by_date_row_produces_exact_expired_deadline(tmp_path):
    post = raw("deadline", "LAST CHANCE TO APPLY!\nSubmit your application to join us as an Associate.", "2026-10-07T15:00:00Z")
    post["media"] = [{"position": 1, "type": "image", "source_url": None, "local_path": "poster.jpg", "sha256": "b" * 64,
                      "width": 100, "height": 100, "download_status": "downloaded"}]
    atomic_json(tmp_path / "ocr/new.json", {"image_hash": "b" * 64, "status": "success", "text": "BY OCT 7 AT 11:59PM",
                                           "processed_at": "2026-10-28T06:00:00Z"})
    save(tmp_path, post)
    result = process_local_post(tmp_path, post, NOW)
    assert result["decision"] == "excluded" and result["reasons"] == ["expired_deadline"]
    opportunity = json.loads((tmp_path / "drafts/deadline.json").read_text())["opportunity"]
    assert opportunity["start_date"] is None
    assert opportunity["deadline_date"] == "2026-10-07"
    assert opportunity["deadline_at"] == "2026-10-08T03:59:00Z"


@pytest.mark.parametrize("call_posted,event_posted", [
    ("2026-10-01T16:00:00Z", "2026-10-10T16:00:00Z"),
    ("2026-10-10T16:00:00Z", "2026-10-01T16:00:00Z"),
])
def test_expired_event_removes_undated_speaker_call_in_either_order(tmp_path, monkeypatch, call_posted, event_posted):
    save(tmp_path,
         raw("call", "Women in Tech Summit wants YOU to give a five minute talk! Link in bio to sign up or register!", call_posted),
         raw("event", "Women in Tech Summit\nDate: October 17, 2026\nRegister now via link in bio.", event_posted))
    report = reprocess(tmp_path, NOW)
    assert report["counts"] == {"excluded": 1, "merged_duplicate": 1}
    leader = next(row for row in report["records"] if row["decision"] == "excluded")
    assert leader["reasons"] == ["expired_start"]
    group_path = tmp_path / "event_groups" / f"{leader['source_post_id']}.json"
    group = json.loads(group_path.read_text())
    assert group["draft"]["opportunity"]["kind"] == "event"
    assert group["draft"]["opportunity"]["start_date"] == "2026-10-17"
    review._validator("opportunity-draft").validate(group["draft"])
    assert json.loads((tmp_path / "publishable-candidates.json").read_text())["candidates"] == []
    monkeypatch.setattr(review, "DATA_DIR", tmp_path)
    monkeypatch.setattr(review, "DRAFTS_DIR", tmp_path / "drafts")
    csv_path, _ = review.export_review(tmp_path / "review")
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.DictReader(stream)) == []
    reprocess(tmp_path, NOW)
    assert json.loads(group_path.read_text())["content_revision"] == group["content_revision"]


@pytest.mark.parametrize("caption,posted", [
    ("Women in Tech Summit ambassador applications open! Apply now.", "2026-10-01T16:00:00Z"),
    ("Women in Tech Summit call for speakers. Apply now.", "2025-10-01T16:00:00Z"),
])
def test_other_recruitment_and_previous_year_calls_stay_separate(tmp_path, caption, posted):
    save(tmp_path, raw("call", caption, posted),
         raw("event", "Women in Tech Summit\nDate: October 17, 2026\nRegister now.", "2026-10-10T16:00:00Z"))
    assert "merged_duplicate" not in reprocess(tmp_path, NOW)["counts"]

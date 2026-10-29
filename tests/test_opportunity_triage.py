import json
from datetime import datetime, timedelta, timezone

import pytest

from src.opportunities import review
from src.opportunities.drafts import atomic_json
from src.opportunities.extract import build_draft
from src.opportunities.scan import opportunity_relevance
from src.opportunities.triage import VIDEO_WARNING, assess, process_local_post, reprocess

NOW = datetime(2026, 10, 28, 16, tzinfo=timezone.utc)
CAPTION = "Python Workshop\nDate: November 20, 2026\nLocation: IC 200\nRegister now via link in bio."


def post(caption=CAPTION):
    return {"source": "instagram", "source_post_id": "test_post", "organizer_id": "ds3_utsc",
            "source_url": "https://www.instagram.com/p/test_post/", "posted_at": "2026-10-28T12:00:00Z",
            "caption_raw": caption, "caption_status": "available", "media": [], "media_completeness": "complete",
            "first_seen_at": "2026-10-28T12:00:00Z", "last_seen_at": "2026-10-28T12:00:00Z",
            "last_fetched_at": "2026-10-28T12:00:00Z", "content_hash": "a" * 64,
            "collection_status": "complete", "errors": []}


def process(tmp_path, raw):
    atomic_json(tmp_path / "test_post.json", raw)
    decision = process_local_post(tmp_path, raw, now=NOW)
    draft = json.loads((tmp_path / "drafts/test_post.json").read_text(encoding="utf-8"))
    return decision, draft


def test_clear_official_caption_becomes_candidate_without_human_approval(tmp_path):
    decision, draft = process(tmp_path, post())
    assert decision["decision"] == "auto_candidate"
    assert draft["opportunity"]["review_status"] == "needs_review"
    assert not (tmp_path / "review_overrides").exists()
    report = reprocess(tmp_path, now=NOW)
    assert report["counts"] == {"auto_candidate": 1}
    candidate = json.loads((tmp_path / "publishable-candidates.json").read_text())["candidates"][0]
    assert candidate["opportunity"]["review_status"] == "needs_review"
    assert candidate["eligibility"]["decision"] == "auto_candidate"
    review._validator("opportunity").validate(candidate["opportunity"])
    assert (tmp_path / "drafts/test_post.json").read_text().count('"content_revision": 1') == 1


def test_video_caption_can_auto_qualify_without_falsifying_collection_status(tmp_path):
    raw = post()
    raw.update(collection_status="partial", errors=[VIDEO_WARNING], media=[{
        "position": 1, "type": "video", "download_status": "metadata_only", "source_url": None,
        "local_path": None, "sha256": None, "width": None, "height": None}])
    decision, _ = process(tmp_path, raw)
    assert decision["decision"] == "auto_candidate"
    assert json.loads((tmp_path / "test_post.json").read_text())["collection_status"] == "partial"


@pytest.mark.parametrize("caption, expected", [
    ("LAST CHANCE TO APPLY!\nSubmit your application to join EA as an Associate!", "needs_review"),
    ("Python Workshop\nDate: November 20, 2026\nDate: November 21, 2026\nLocation: IC 200\nRegister via link in bio", "needs_review"),
    ("Python Workshop\nDate: October 20, 2026\nLocation: IC 200\nRegister via link in bio", "excluded"),
    ("Welcome back to our conference sponsor!", "excluded"),
    (CAPTION + " Registration closes next Friday!", "auto_candidate"),
    (CAPTION.replace("November 20, 2026", "November 20"), "auto_candidate"),
    (CAPTION.replace("Location: IC 200\n", ""), "auto_candidate"),
    (CAPTION.replace("via link in bio", ""), "auto_candidate"),
])
def test_reasoned_routing(tmp_path, caption, expected):
    assert process(tmp_path, post(caption))[0]["decision"] == expected


def test_apply_phrases_are_collected_and_deadline_is_not_event_start(tmp_path):
    assert opportunity_relevance("LAST CHANCE TO APPLY! Submit your application to join EA as an Associate.", False) == "collect"
    draft = build_draft(post("We are hiring associates!\nApplications close November 20, 2026\nApply now via link in bio."))
    assert draft["opportunity"]["kind"] == "ongoing_opportunity"
    assert draft["opportunity"]["start_date"] is None
    assert draft["opportunity"]["deadline_date"] == "2026-11-20"
    assert "start_date" not in draft["missing_fields"]


def test_old_historical_date_does_not_expire_future_event(tmp_path):
    raw = post("Python Workshop\nDate: November 20, 2026\nLocation: IC 200\nRegister via link in bio\nOur club was founded October 1, 2025.")
    decision, draft = process(tmp_path, raw)
    assert decision["decision"] == "auto_candidate"
    assert draft["opportunity"]["start_date"] == "2026-11-20"


def test_manual_hold_or_rejection_cannot_be_bypassed(tmp_path):
    decision, draft = process(tmp_path, post())
    ledger_path = tmp_path / "review_overrides" / f"{draft['opportunity']['opportunity_id']}.json"
    atomic_json(ledger_path, {"review_status": "rejected", "content_revision": 1, "field_overrides": {}})
    assert assess(draft, post(), tmp_path, NOW)["decision"] == "needs_review"
    changed = {**post(), "content_hash": "b" * 64}
    assert process(tmp_path, changed)[0]["decision"] == "needs_review"


def test_missing_or_failed_image_does_not_block_caption_draft_but_requires_review(tmp_path):
    raw = post()
    raw.update(collection_status="partial", errors=["Image download failed"], media=[{"type": "image", "download_status": "failed", "sha256": "b" * 64}])
    decision, draft = process(tmp_path, raw)
    assert decision["decision"] == "needs_review"
    assert "unresolved_collection_gap" in decision["warnings"]
    assert draft["opportunity"]["start_date"] == "2026-11-20"


def test_candidate_is_rechecked_after_expiry_or_source_change(tmp_path, monkeypatch):
    _, draft = process(tmp_path, post())
    monkeypatch.setattr(review, "DATA_DIR", tmp_path)
    monkeypatch.setattr(review, "OVERRIDES_DIR", tmp_path / "review_overrides")
    assert assess(draft, post(), tmp_path, NOW + timedelta(days=30))["decision"] == "excluded"
    atomic_json(tmp_path / "test_post.json", {**post(), "content_hash": "b" * 64})
    assert review.effective_publishable_opportunity(draft) is None


def test_default_manual_queue_omits_automatic_candidates_but_supports_spot_checks(tmp_path, monkeypatch):
    _, draft = process(tmp_path, post())
    monkeypatch.setattr(review, "DATA_DIR", tmp_path)
    monkeypatch.setattr(review, "DRAFTS_DIR", tmp_path / "drafts")
    monkeypatch.setattr(review, "OVERRIDES_DIR", tmp_path / "review_overrides")
    # Freeze policy time so this test is independent of wall-clock expiry.
    import src.opportunities.triage as triage
    original = triage.assess
    monkeypatch.setattr(triage, "assess", lambda draft, raw, directory, now=None: original(draft, raw, directory, NOW))
    assert review._load_reviewable_drafts() == []
    assert len(review._load_reviewable_drafts(include_auto=True)) == 1
    assert review.effective_publishable_opportunity(draft)["eligibility"]["decision"] == "auto_candidate"


def test_excluded_reruns_do_not_increment_versions_or_create_new_archives(tmp_path):
    raw = post("Welcome back to our conference sponsor!")
    _, original = process(tmp_path, raw)
    original_version = original["opportunity"]["content_revision"]
    for _ in range(2):
        report = reprocess(tmp_path, now=NOW)
        assert report["counts"] == {"excluded": 1}
    saved = json.loads((tmp_path / "drafts/test_post.json").read_text())
    assert saved["opportunity"]["content_revision"] == original_version
    assert not (tmp_path / "draft_history").exists()


def test_video_caption_resolves_relative_deadline_from_publication(tmp_path):
    raw = post(CAPTION + " Registration closes NEXT FRIDAY!")
    raw.update(collection_status="partial", errors=[VIDEO_WARNING], media=[{
        "position": 1, "type": "video", "download_status": "metadata_only", "source_url": None,
        "local_path": None, "sha256": None, "width": None, "height": None}])
    decision, draft = process(tmp_path, raw)
    assert decision["decision"] == "auto_candidate"
    assert draft["exclusion_reason"] is None
    assert "unresolved_collection_gap" not in decision["reasons"]
    assert draft["opportunity"]["deadline_date"] == "2026-10-30"


def test_location_emoji_and_caption_date_are_supported_without_recruitment_false_tag(tmp_path):
    raw = post(CAPTION.replace("Location: IC 200", "📍 IC 200") + "\nAI in business and recruitment: panel discussion.")
    decision, draft = process(tmp_path, raw)
    assert decision["decision"] == "auto_candidate"
    assert draft["opportunity"]["location"] == "IC 200"
    assert "recruitment" not in draft["opportunity"]["activity_type_ids"]


def test_freshness_and_evidence_mismatch_prevent_auto_routing(tmp_path):
    raw = post()
    _, draft = process(tmp_path, raw)
    stale = assess(draft, raw, tmp_path, NOW + timedelta(days=4))
    assert stale["decision"] == "needs_review"
    assert "source_snapshot_needs_refresh" in stale["reasons"]
    draft["opportunity"]["title"] = "A title not in the evidence"
    decision = assess(draft, raw, tmp_path, NOW)
    assert decision["decision"] == "needs_review"
    assert "title_needs_caption_confirmation" in decision["reasons"]


def test_report_escapes_untrusted_caption_content(tmp_path):
    raw = post(CAPTION + '\n<script>alert("x")</script>')
    process(tmp_path, raw)
    reprocess(tmp_path, now=NOW)
    page = (tmp_path / "triage-report.html").read_text(encoding="utf-8")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_partial_snapshot_with_readable_poster_still_enters_manual_review(tmp_path):
    from src.opportunities.drafts import source_is_current
    raw = post(None)
    raw.update(caption_status="unavailable", collection_status="partial", errors=[VIDEO_WARNING], media=[{
        "position": 1, "type": "image", "source_url": "https://cdn.example/image.jpg", "local_path": "poster.jpg",
        "sha256": "b" * 64, "width": 100, "height": 100, "download_status": "downloaded"}])
    atomic_json(tmp_path / "ocr/poster.json", {"image_hash": "b" * 64, "status": "success", "processed_at": "2026-10-28T12:00:00Z", "text": CAPTION})
    decision, draft = process(tmp_path, raw)
    assert decision["decision"] == "needs_review"
    assert "caption_unavailable" in decision["reasons"]
    assert "no_accessible_evidence" not in decision["reasons"]
    assert source_is_current(tmp_path, draft["opportunity"])


def test_readable_internal_general_meeting_heading_is_excluded(tmp_path):
    raw = post(None)
    raw.update(caption_status="unavailable", collection_status="partial", errors=[VIDEO_WARNING], media=[{
        "position": 1, "type": "image", "source_url": "https://cdn.example/image.jpg", "local_path": "poster.jpg",
        "sha256": "b" * 64, "width": 100, "height": 100, "download_status": "downloaded"}])
    atomic_json(tmp_path / "ocr/poster.json", {"image_hash": "b" * 64, "status": "success", "processed_at": "2026-10-28T12:00:00Z", "text": "CREATE\nYEARLY\nGENERAL\nMEETING\n2026-27"})
    decision, _ = process(tmp_path, raw)
    assert decision["decision"] == "excluded"
    assert decision["reasons"] == ["internal_club_meeting"]

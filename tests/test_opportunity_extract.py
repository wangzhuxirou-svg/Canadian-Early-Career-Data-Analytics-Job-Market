import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from src.opportunities.extract import build_draft


ROOT = Path(__file__).resolve().parents[1]


def _raw_post(caption, caption_status="available"):
    return {
        "source": "instagram",
        "source_post_id": "sample_post",
        "organizer_id": "ds3_utsc",
        "source_url": "https://www.instagram.com/p/sample_post/",
        "posted_at": "2026-10-22T13:40:50Z",
        "caption_raw": caption,
        "caption_status": caption_status,
        "media": [],
        "media_completeness": "complete",
        "first_seen_at": "2026-10-22T13:40:50Z",
        "last_seen_at": "2026-10-22T13:40:50Z",
        "last_fetched_at": "2026-10-22T13:40:50Z",
        "content_hash": "a" * 64,
        "collection_status": "complete",
        "errors": [],
    }


def _validate_draft(draft):
    schema_dir = ROOT / "contracts" / "schemas"
    schemas = {
        path.name.removesuffix(".schema.json"): json.loads(
            path.read_text(encoding="utf-8")
        )
        for path in schema_dir.glob("*.schema.json")
    }
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas.values()
    )
    Draft202012Validator(
        schemas["opportunity-draft"],
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(draft)


def test_extracts_only_explicit_text_and_records_compact_source_evidence(tmp_path):
    raw_post = _raw_post(
        "DS3 ML Bootcamp Workshop\n"
        "Date: Monday, October 26, 2026\n"
        "Time: 4 PM - 6 PM\n"
        "Location: IC-230\n"
        "For all registered participants."
    )

    draft = build_draft(raw_post, tmp_path)

    _validate_draft(draft)
    opportunity = draft["opportunity"]
    assert opportunity["title"] == "DS3 ML Bootcamp Workshop"
    assert opportunity["start_date"] == "2026-10-26"
    assert opportunity["start_at"] == "2026-10-26T20:00:00Z"
    assert opportunity["end_at"] == "2026-10-26T22:00:00Z"
    assert opportunity["start_precision"] == "datetime"
    assert opportunity["location"] == "IC-230"
    assert opportunity["activity_type_ids"] == ["workshop"]
    assert opportunity["topic_ids"] == ["data_ai"]
    assert opportunity["review_status"] == "needs_review"
    assert opportunity["beginner_level"] == "unknown"
    assert all(item["source_part"] == "caption" for item in draft["evidence"])
    assert opportunity["start_date_text"] == "Date: Monday, October 26, 2026 | Time: 4 PM - 6 PM"
    assert "registration_url" not in draft["missing_fields"]


def test_uses_only_successful_ocr_and_keeps_image_evidence_separate(tmp_path):
    raw_post = _raw_post(None, caption_status="unavailable")
    raw_post["media"] = [{
        "position": 2,
        "type": "image",
        "source_url": "https://cdn.example/poster.jpg",
        "local_path": "poster.jpg",
        "sha256": "b" * 64,
        "width": 1080,
        "height": 1080,
        "download_status": "downloaded",
    }, {
        "position": 3,
        "type": "image",
        "source_url": "https://cdn.example/other-poster.jpg",
        "local_path": "other-poster.jpg",
        "sha256": "c" * 64,
        "width": 1080,
        "height": 1080,
        "download_status": "downloaded",
    }]
    (tmp_path / "success.json").write_text(json.dumps({
        "image_hash": "c" * 64,
        "status": "success",
        "processed_at": "2026-10-22T14:00:00Z",
        "text": "Data Analytics Workshop\nLocation: IC-230",
    }), encoding="utf-8")
    (tmp_path / "low.json").write_text(json.dumps({
        "image_hash": "b" * 64,
        "status": "low_confidence",
        "processed_at": "2026-10-22T14:01:00Z",
        "text": "Register at https://wrong.example",
    }), encoding="utf-8")

    draft = build_draft(raw_post, tmp_path)

    _validate_draft(draft)
    assert draft["opportunity"]["title"] == "Data Analytics Workshop"
    assert draft["opportunity"]["registration_url"] is None
    assert {item["source_part"] for item in draft["evidence"]} == {"image_ocr"}
    assert {item["media_position"] for item in draft["evidence"]} == {3}


def test_does_not_create_opportunity_from_unavailable_caption():
    assert build_draft(_raw_post(None, caption_status="unavailable")) is None


def test_excludes_retrospective_posts_with_contract_valid_reason():
    draft = build_draft(_raw_post("Thank you for joining our workshop! Event recap"))

    _validate_draft(draft)
    assert draft["exclusion_reason"] == "retrospective_or_closed_call"
    assert draft["opportunity"]["review_status"] == "draft"


def test_conflicting_dates_are_left_unknown_for_review():
    draft = build_draft(_raw_post(
        "Workshop\nDate: October 26, 2026\nDate: November 2, 2026"
    ))

    assert draft["opportunity"]["start_date"] is None
    assert draft["opportunity"]["start_precision"] == "unknown"
    assert any("Multiple distinct event dates" in item for item in draft["conflicts"])


def test_link_in_bio_does_not_create_a_registration_url_or_missing_link_flag():
    draft = build_draft(_raw_post(
        "Register for our data workshop. Link in bio."
    ))

    assert draft["opportunity"]["registration_url"] is None
    assert "registration_url" not in draft["missing_fields"]


def test_missing_registration_url_is_reported_only_after_explicit_call_to_action():
    draft = build_draft(_raw_post("Register now for our data workshop."))

    assert "registration_url" in draft["missing_fields"]


def test_unclassified_topic_is_left_empty_instead_of_filled_with_other():
    draft = build_draft(_raw_post("Join us for a workshop on campus."))

    assert draft["opportunity"]["topic_ids"] == []


def test_ocr_internal_general_meeting_is_excluded(tmp_path):
    raw_post = _raw_post(None, caption_status="unavailable")
    raw_post["media"] = [{
        "position": 1,
        "type": "image",
        "source_url": "https://cdn.example/poster.jpg",
        "local_path": "poster.jpg",
        "sha256": "d" * 64,
        "width": 1080,
        "height": 1080,
        "download_status": "downloaded",
    }]
    (tmp_path / "meeting.json").write_text(json.dumps({
        "image_hash": "d" * 64,
        "status": "success",
        "processed_at": "2026-10-28T04:00:00Z",
        "text": "CREATE\nYEARLY\nGENERAL\nMEETING\n2026-27",
    }), encoding="utf-8")

    draft = build_draft(raw_post, tmp_path)

    _validate_draft(draft)
    opportunity = draft["opportunity"]
    assert opportunity["title"] == "YEARLY GENERAL MEETING"
    assert opportunity["kind"] == "event"
    assert opportunity["activity_type_ids"] == []
    assert opportunity["start_date"] is None
    assert opportunity["review_status"] == "draft"
    assert draft["exclusion_reason"] == "internal_club_meeting"

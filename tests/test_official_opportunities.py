import json

import pytest

from src.official_opportunities import load_official_opportunities


def _write_import(tmp_path, **changes):
    opportunity = {
        "opportunity_id": "synthetic-official-event",
        "source_url": "https://example.org/calendar/event",
        "review_status": "approved",
        "change_pending": False,
        "verified_at": "2026-11-25T18:00:00Z",
        "content_revision": 2,
    }
    row = {
        "opportunity": opportunity,
        "observed_at": "2026-11-24T12:00:00Z",
        "review_ref": "synthetic-test-reference",
    }
    for key, value in changes.items():
        if key in row:
            row[key] = value
        else:
            opportunity[key] = value
    path = tmp_path / "official.json"
    path.write_text(json.dumps({"organizers": [], "opportunities": [row]}), encoding="utf-8")
    return path


def test_extracts_official_provenance_without_private_review_reference(tmp_path):
    organizers, opportunities, observations = load_official_opportunities(_write_import(tmp_path))
    assert organizers == []
    assert opportunities[0]["source_post_ids"] == ["official:synthetic-official-event"]
    assert observations[0]["source"] == "official"
    assert observations[0]["eligibility"]["decision"] == "human_approved"
    assert observations[0]["eligibility"]["content_revision"] == 2
    assert observations[0]["source_post_ids"][0]["last_fetched_at"] == "2026-11-24T12:00:00Z"
    assert "synthetic-test-reference" not in json.dumps([opportunities, observations])


@pytest.mark.parametrize("changes", [
    {"review_status": "needs_review"},
    {"change_pending": True},
    {"review_ref": ""},
    {"observed_at": "2026-11-27T12:00:00Z"},
    {"source_url": "https://www.instagram.com/p/example/"},
    {"source_url": "https://token@example.org/event"},
    {"source_post_ids": ["unrelated-id"]},
])
def test_rejects_unreviewed_or_inconsistent_official_input(tmp_path, changes):
    with pytest.raises(ValueError):
        load_official_opportunities(_write_import(tmp_path, **changes))

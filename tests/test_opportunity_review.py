import csv
import json
from pathlib import Path

import pytest

from src.opportunities import review
from src.opportunities.extract import build_draft


ROOT = Path(__file__).resolve().parents[1]


def _raw_post(caption):
    return {
        "source": "instagram",
        "source_post_id": "review_post",
        "organizer_id": "ds3_utsc",
        "source_url": "https://www.instagram.com/p/review_post/",
        "posted_at": "2026-10-22T13:40:50Z",
        "caption_raw": caption,
        "caption_status": "available",
        "media": [],
        "media_completeness": "complete",
        "first_seen_at": "2026-10-22T13:40:50Z",
        "last_seen_at": "2026-10-22T13:40:50Z",
        "last_fetched_at": "2026-10-22T13:40:50Z",
        "content_hash": "a" * 64,
        "collection_status": "complete",
        "errors": [],
    }


def _setup_review_workspace(monkeypatch, tmp_path, caption="Data workshop"):
    data_dir = tmp_path / "data" / "opportunities"
    drafts_dir = data_dir / "drafts"
    review_dir = data_dir / "review"
    overrides_dir = data_dir / "review_overrides"
    drafts_dir.mkdir(parents=True)
    raw_post = _raw_post(caption)
    (data_dir / "review_post.json").write_text(
        json.dumps(raw_post), encoding="utf-8"
    )
    draft = build_draft(raw_post, tmp_path / "ocr")
    (drafts_dir / "review_post.json").write_text(
        json.dumps(draft), encoding="utf-8"
    )
    monkeypatch.setattr(review, "DATA_DIR", data_dir)
    monkeypatch.setattr(review, "DRAFTS_DIR", drafts_dir)
    monkeypatch.setattr(review, "REVIEW_DIR", review_dir)
    monkeypatch.setattr(review, "OVERRIDES_DIR", overrides_dir)
    monkeypatch.setattr(review, "ORGANIZERS_PATH", ROOT / "resources" / "organizers.json")
    return draft, drafts_dir, data_dir


def _read_rows(path):
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _write_rows(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _review_row(monkeypatch, tmp_path):
    _setup_review_workspace(monkeypatch, tmp_path)
    csv_path, html_path = review.export_review()
    row = _read_rows(csv_path)[0]
    return csv_path, html_path, row


def test_import_validates_previous_overrides_in_final_merged_record(monkeypatch, tmp_path):
    csv_path, _, row = _review_row(monkeypatch, tmp_path)
    row.update(review_status="needs_review", reviewer="tester", reviewed_at="2026-10-28T12:00:00Z", reason="Set date")
    row["override__start_date"] = "2026-11-20"
    row["override__start_precision"] = "date"
    row["override__date_precision"] = "date"
    _write_rows(csv_path, [row])
    review.import_review(csv_path)
    # This patch alone looks valid against the original undated draft, but
    # clearing precision conflicts with the previously saved date.
    row["override__start_date"] = ""
    row["override__start_precision"] = "unknown"
    row["override__date_precision"] = "unknown"
    _write_rows(csv_path, [row])
    with pytest.raises(review.ReviewImportError):
        review.import_review(csv_path)
    ledger = json.loads(next(review.OVERRIDES_DIR.glob("*.json")).read_text())
    assert ledger["field_overrides"]["start_precision"] == "date"
    assert len(ledger["history"]) == 1


def test_approved_effective_handoff_rejects_changed_source(monkeypatch, tmp_path):
    from src.opportunities.drafts import track_source, store_draft
    draft, drafts_dir, data_dir = _setup_review_workspace(monkeypatch, tmp_path)
    raw_path = data_dir / "review_post.json"
    raw = json.loads(raw_path.read_text())
    store_draft(data_dir, raw, draft, track_source(data_dir, raw))
    current = json.loads((drafts_dir / "review_post.json").read_text())
    csv_path, _ = review.export_review()
    row = _read_rows(csv_path)[0]
    row.update(review_status="approved", reviewer="tester", reviewed_at="2026-10-28T12:00:00Z", reason="Checked source")
    row["override__title"] = "Reviewed workshop"
    _write_rows(csv_path, [row])
    review.import_review(csv_path)
    effective = review.effective_approved_opportunity(current)
    assert effective["title"] == "Reviewed workshop"
    assert effective["change_pending"] is False
    assert effective["verified_at"] == row["reviewed_at"]
    raw["content_hash"] = "b" * 64
    raw_path.write_text(json.dumps(raw))
    assert review.effective_approved_opportunity(current) is None
    assert review._load_reviewable_drafts() == []
    store_draft(data_dir, raw, draft, track_source(data_dir, raw))
    assert len(review._load_reviewable_drafts()) == 1
    changed = json.loads((drafts_dir / "review_post.json").read_text())
    assert review.effective_approved_opportunity(changed) is None


def test_export_writes_excel_friendly_csv_and_escaped_local_preview(
    monkeypatch, tmp_path
):
    _, _, data_dir = _setup_review_workspace(
        monkeypatch,
        tmp_path,
        "Data workshop\n<script>alert(1)</script>",
    )
    monkeypatch.setattr(review, "ROOT", tmp_path)
    media_dir = data_dir / "media"
    media_dir.mkdir()
    media_path = media_dir / "slide.jpg"
    media_path.write_bytes(b"synthetic image placeholder")
    raw_path = data_dir / "review_post.json"
    raw_post = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_post["media"] = [{
        "position": 1,
        "type": "image",
        "source_url": "https://cdn.example/slide.jpg",
        "local_path": str(media_path.relative_to(tmp_path)).replace("\\", "/"),
        "sha256": "b" * 64,
        "width": 10,
        "height": 10,
        "download_status": "downloaded",
    }]
    raw_path.write_text(json.dumps(raw_post), encoding="utf-8")
    ocr_dir = data_dir / "ocr"
    ocr_dir.mkdir()
    (ocr_dir / "slide.json").write_text(json.dumps({
        "image_hash": "b" * 64,
        "processed_at": "2026-10-28T04:30:00Z",
        "status": "success",
        "text": "<script>OCR text</script>",
    }), encoding="utf-8")
    csv_path, html_path = review.export_review()
    row = _read_rows(csv_path)[0]

    html_text = html_path.read_text(encoding="utf-8")
    assert row["content_revision"] == "1"
    assert row["review_status"] == "needs_review"
    assert "override__title" in row
    assert "&lt;script&gt;" in html_text
    assert "<script>alert(1)</script>" not in html_text
    assert "<script>OCR text</script>" not in html_text
    assert "&lt;script&gt;OCR text&lt;/script&gt;" in html_text
    assert "OCR status: success" in html_text
    assert "Caption not collected." not in html_text


def test_csv_export_escapes_formula_cells(tmp_path, monkeypatch):
    _setup_review_workspace(
        monkeypatch,
        tmp_path,
        '=HYPERLINK("https://example.invalid") data workshop',
    )
    csv_path, _ = review.export_review()
    text = csv_path.read_text(encoding="utf-8-sig")

    assert "'=HYPERLINK" in text


def test_import_valid_patch_applies_overrides_and_preserves_audit_history(
    monkeypatch, tmp_path
):
    csv_path, _, row = _review_row(monkeypatch, tmp_path)
    row["review_status"] = "approved"
    row["reviewer"] = "reviewer-1"
    row["reviewed_at"] = "2026-10-28T04:30:00Z"
    row["reason"] = "Confirmed from the original post."
    row["override__title"] = "Confirmed data workshop"
    row["override__skill_ids"] = '["python"]'
    _write_rows(csv_path, [row])

    result = review.import_review(csv_path)

    ledger_path = review.OVERRIDES_DIR / f"{row['opportunity_id']}.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(result["applied"]) == 1
    assert ledger["review_status"] == "approved"
    assert ledger["field_overrides"] == {
        "title": "Confirmed data workshop",
        "skill_ids": ["python"],
    }
    assert ledger["history"][0]["reviewer"] == "reviewer-1"
    assert ledger["history"][0]["reason"] == "Confirmed from the original post."


def test_import_rejects_stale_revision_and_reports_the_error(
    monkeypatch, tmp_path
):
    csv_path, _, row = _review_row(monkeypatch, tmp_path)
    row["review_status"] = "rejected"
    row["reviewer"] = "reviewer-1"
    row["reviewed_at"] = "2026-10-28T04:30:00Z"
    row["reason"] = "Stale test."
    _write_rows(csv_path, [row])

    draft_path = review.DRAFTS_DIR / "review_post.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["opportunity"]["content_revision"] += 1
    draft_path.write_text(json.dumps(draft), encoding="utf-8")

    with pytest.raises(review.ReviewImportError) as error:
        review.import_review(csv_path)

    report = json.loads(error.value.report_path.read_text(encoding="utf-8"))
    assert "Stale review revision" in report["errors"][0]["errors"][0]
    assert not review.OVERRIDES_DIR.exists()


def test_import_rejects_unknown_skills_and_unknown_override_columns(
    monkeypatch, tmp_path
):
    csv_path, _, row = _review_row(monkeypatch, tmp_path)
    row["review_status"] = "approved"
    row["reviewer"] = "reviewer-1"
    row["reviewed_at"] = "2026-10-28T04:30:00Z"
    row["reason"] = "Invalid skill test."
    row["override__skill_ids"] = '["made_up_skill"]'
    _write_rows(csv_path, [row])

    with pytest.raises(review.ReviewImportError):
        review.import_review(csv_path)

    report = json.loads(
        (csv_path.parent / "review-import-report.json").read_text(encoding="utf-8")
    )
    assert "Unknown skill IDs" in report["errors"][0]["errors"][0]
    assert not review.OVERRIDES_DIR.exists()

    headers = list(row) + ["override__not_allowed"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerow({**row, "override__not_allowed": "unexpected"})
    with pytest.raises(review.ReviewImportError):
        review.import_review(csv_path)
    report = json.loads(
        (csv_path.parent / "review-import-report.json").read_text(encoding="utf-8")
    )
    assert "Unknown override columns" in report["errors"][0]["errors"][0]


def test_import_requires_review_metadata(
    monkeypatch, tmp_path
):
    csv_path, _, row = _review_row(monkeypatch, tmp_path)
    row["review_status"] = "needs_review"
    row["reviewer"] = ""
    row["reviewed_at"] = ""
    row["reason"] = ""
    _write_rows(csv_path, [row])

    with pytest.raises(review.ReviewImportError):
        review.import_review(csv_path)

    report = json.loads(
        (csv_path.parent / "review-import-report.json").read_text(encoding="utf-8")
    )
    errors = " ".join(report["errors"][0]["errors"])
    assert "non-empty" in errors
    assert "does not match 'Z$'" in errors


def test_approved_draft_is_hidden_until_its_content_revision_changes(
    monkeypatch, tmp_path
):
    csv_path, _, row = _review_row(monkeypatch, tmp_path)
    row["review_status"] = "approved"
    row["reviewer"] = "reviewer-1"
    row["reviewed_at"] = "2026-10-28T04:30:00Z"
    row["reason"] = "Confirmed."
    _write_rows(csv_path, [row])
    review.import_review(csv_path)

    next_csv, _ = review.export_review()
    assert _read_rows(next_csv) == []

    draft_path = review.DRAFTS_DIR / "review_post.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["opportunity"]["content_revision"] += 1
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    next_csv, _ = review.export_review()
    assert len(_read_rows(next_csv)) == 1


def test_new_revision_preserves_history_but_does_not_carry_old_overrides(
    monkeypatch, tmp_path
):
    csv_path, _, row = _review_row(monkeypatch, tmp_path)
    row.update({
        "review_status": "approved",
        "reviewer": "reviewer-1",
        "reviewed_at": "2026-10-28T04:30:00Z",
        "reason": "First revision confirmed.",
        "override__title": "First title",
        "override__skill_ids": '["python"]',
    })
    _write_rows(csv_path, [row])
    review.import_review(csv_path)

    draft_path = review.DRAFTS_DIR / "review_post.json"
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    draft["opportunity"]["content_revision"] = 2
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    next_csv, _ = review.export_review()
    next_row = _read_rows(next_csv)[0]
    next_row.update({
        "review_status": "needs_review",
        "reviewer": "reviewer-2",
        "reviewed_at": "2026-10-28T05:30:00Z",
        "reason": "Reviewing changed source.",
        "override__title": "Second title",
    })
    _write_rows(next_csv, [next_row])
    review.import_review(next_csv)

    ledger = json.loads(
        (review.OVERRIDES_DIR / f"{row['opportunity_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert ledger["field_overrides"] == {"title": "Second title"}
    assert len(ledger["history"]) == 2


def test_import_validates_full_opportunity_date_precision_constraints(
    monkeypatch, tmp_path
):
    csv_path, _, row = _review_row(monkeypatch, tmp_path)
    row.update({
        "review_status": "approved",
        "reviewer": "reviewer-1",
        "reviewed_at": "2026-10-28T04:30:00Z",
        "reason": "Date test.",
        "override__start_date": "2026-11-02",
    })
    _write_rows(csv_path, [row])

    with pytest.raises(review.ReviewImportError):
        review.import_review(csv_path)

    report = json.loads(
        (csv_path.parent / "review-import-report.json").read_text(encoding="utf-8")
    )
    assert "Updated opportunity contract invalid" in report["errors"][0]["errors"][0]
    assert not review.OVERRIDES_DIR.exists()

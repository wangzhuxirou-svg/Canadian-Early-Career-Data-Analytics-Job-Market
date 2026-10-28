import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.export_bundle import (
    BundleValidationError,
    build_package,
    publish_package,
    validate_bundle,
    validate_package,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts" / "fixtures"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_base_package(path: Path, bundle: dict) -> Path:
    path.mkdir(parents=True)
    bundle_path = path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    (path / "SHA256SUMS.txt").write_text(f"{digest}  bundle.json\n", encoding="ascii")
    return path


def write_reference_files(path: Path, bundle: dict) -> Path:
    path.mkdir(exist_ok=True)
    for filename, key in (
        ("courses.json", "courses"),
        ("learning_resources.json", "resources"),
    ):
        (path / filename).write_text(
            json.dumps(bundle[key]), encoding="utf-8"
        )
    return path


def test_d7_build_reuses_market_timestamps_and_only_includes_current_approved(
    tmp_path, monkeypatch
):
    fixture_bundle = read_json(FIXTURES / "valid_bundle.json")
    base = copy.deepcopy(fixture_bundle)
    base.pop("opportunities")
    base["opportunities"] = []
    base["organizers"] = []
    market_package = write_base_package(tmp_path / "market", base)

    organizer = read_json(FIXTURES / "valid_bundle.json")["organizers"][0]
    organizer["last_verified_at"] = "2026-10-27T12:00:00Z"
    resource_dir = tmp_path / "resources"
    resource_dir.mkdir()
    write_reference_files(resource_dir, fixture_bundle)
    (resource_dir / "organizers.json").write_text(
        json.dumps([organizer]), encoding="utf-8"
    )

    data_dir = tmp_path / "opportunities"
    (data_dir / "drafts").mkdir(parents=True)
    approved = read_json(FIXTURES / "valid_bundle.json")["opportunities"][0]
    for status, post_id in (("approved", "demo_post"), ("needs_review", "unapproved_post")):
        opportunity = copy.deepcopy(approved)
        opportunity["opportunity_id"] = f"opportunity_{post_id}"
        opportunity["source_post_id"] = post_id
        opportunity["source_post_ids"] = [post_id]
        (data_dir / "drafts" / f"{post_id}.json").write_text(
            json.dumps({"opportunity": {**opportunity, "review_status": status}}),
            encoding="utf-8",
        )
    for post_id in ("demo_post", "unapproved_post"):
        (data_dir / f"{post_id}.json").write_text(
            json.dumps(
                {
                    "collection_status": "complete",
                    "first_seen_at": "2026-10-26T16:00:00Z",
                    "last_seen_at": "2026-10-27T16:00:00Z",
                    "last_fetched_at": "2026-10-27T16:00:00Z",
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "src.opportunities.review.effective_publishable_opportunity",
        lambda draft, _data_dir: (
            {"opportunity": draft["opportunity"], "eligibility": {
                "decision": "human_approved", "content_revision": draft["opportunity"]["content_revision"],
                "evaluated_at": "2026-10-28T06:00:00Z"}}
            if draft["opportunity"]["review_status"] == "approved"
            else None
        ),
    )

    package_dir = build_package(
        market_package,
        tmp_path / "candidate",
        dataset_version="1.2.3",
        data_dir=data_dir,
        resources_dir=resource_dir,
    )
    result = validate_package(package_dir)

    assert result["manifest"]["collected_from"] == base["manifest"]["collected_from"]
    assert result["manifest"]["collected_to"] == base["manifest"]["collected_to"]
    assert [item["opportunity_id"] for item in result["opportunities"]] == [
        "opportunity_demo_post"
    ]
    provenance = read_json(package_dir / "dataset_provenance.json")
    assert provenance["datasets"]["opportunities"]["collected_from"] == "2026-10-26T16:00:00Z"
    assert provenance["datasets"]["opportunities"]["collected_to"] == "2026-10-27T16:00:00Z"
    assert provenance["datasets"]["courses"]["records"][0]["verified_at"]


def test_invalid_and_damaged_packages_cannot_be_built_or_published(tmp_path):
    invalid = copy.deepcopy(read_json(FIXTURES / "valid_bundle.json"))
    invalid["market_jobs"][0]["role_id"] = "invented_role"
    invalid_package = write_base_package(tmp_path / "invalid", invalid)
    output = tmp_path / "must-not-exist"

    with pytest.raises(BundleValidationError):
        build_package(
            invalid_package,
            output,
            dataset_version="1.2.3",
            data_dir=tmp_path / "empty-data",
        )
    assert not output.exists()
    with pytest.raises(BundleValidationError):
        publish_package(
            invalid_package,
            tmp_path / "bad-published",
            tmp_path / "not-needed-because-package-is-invalid.json",
        )
    assert not (tmp_path / "bad-published").exists()

    damaged = copy.deepcopy(read_json(FIXTURES / "valid_bundle.json"))
    damaged["opportunities"][0]["review_status"] = "needs_review"
    with pytest.raises(BundleValidationError, match="contract validation failed"):
        validate_bundle(damaged)


def test_legacy_manifest_upgrade_is_explicit_and_preserves_market_dates(tmp_path):
    base = copy.deepcopy(read_json(FIXTURES / "valid_bundle.json"))
    base["manifest"]["schema_version"] = "1.0.0"
    market_package = write_base_package(tmp_path / "legacy-market", base)
    (market_package / "manifest.json").write_text(
        json.dumps(base["manifest"]), encoding="utf-8"
    )
    bundle_path = market_package / "bundle.json"
    (market_package / "SHA256SUMS.txt").write_text(
        "\n".join(
            f"{hashlib.sha256((market_package / filename).read_bytes()).hexdigest()}  {filename}"
            for filename in ("bundle.json", "manifest.json")
        )
        + "\n",
        encoding="ascii",
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "organizers.json").write_text("[]", encoding="utf-8")
    write_reference_files(resources, base)

    with pytest.raises(BundleValidationError, match="schema version is stale"):
        build_package(
            market_package,
            tmp_path / "without-upgrade",
            dataset_version="1.2.3",
            data_dir=tmp_path / "empty-data",
            resources_dir=resources,
        )
    result = build_package(
        market_package,
        tmp_path / "upgraded",
        dataset_version="1.2.3",
        data_dir=tmp_path / "empty-data",
        resources_dir=resources,
        upgrade_legacy_manifest=True,
    )
    upgraded = validate_package(result)
    assert upgraded["manifest"]["schema_version"] == "1.1.0"
    assert upgraded["manifest"]["collected_from"] == base["manifest"]["collected_from"]
    assert upgraded["manifest"]["collected_to"] == base["manifest"]["collected_to"]


def test_publication_requires_complete_market_data_and_human_attestation(tmp_path):
    base = copy.deepcopy(read_json(FIXTURES / "valid_bundle.json"))
    base["opportunities"] = []
    base["organizers"] = []
    market_package = write_base_package(tmp_path / "market", base)
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "organizers.json").write_text("[]", encoding="utf-8")
    write_reference_files(resources, base)
    package_dir = build_package(
        market_package,
        tmp_path / "candidate",
        dataset_version="1.2.3",
        data_dir=tmp_path / "empty-data",
        resources_dir=resources,
    )
    attestation = tmp_path / "attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "dataset_version": "1.2.3",
                "reviewer": "Reviewer",
                "confirmed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "confirmations": {
                    "role_classifications": True,
                    "course_evidence": True,
                    "opportunity_dates": True,
                },
            }
        ),
        encoding="utf-8",
    )
    published = publish_package(
        package_dir, tmp_path / "published", attestation
    )
    assert (published / "release_attestation.json").is_file()
    validate_package(published, for_publication=True, require_provenance=True)

    partial = copy.deepcopy(base)
    partial["manifest"]["run_status"] = "partial"
    partial_package = write_base_package(tmp_path / "partial-market", partial)
    candidate = build_package(
        partial_package,
        tmp_path / "partial-candidate",
        dataset_version="1.2.4",
        data_dir=tmp_path / "empty-data",
        resources_dir=resources,
    )
    with pytest.raises(BundleValidationError, match="not complete"):
        publish_package(candidate, tmp_path / "blocked", attestation)
    assert not (tmp_path / "blocked").exists()


@pytest.mark.parametrize("human", [False, True])
def test_real_d6_merged_partial_candidate_reaches_bundle(tmp_path, monkeypatch, human):
    from src.opportunities import triage
    from src.opportunities.drafts import atomic_json
    from src.opportunities.review import effective_publishable_opportunity
    from src.export_bundle import _current_opportunities

    now = datetime(2026, 10, 28, 6, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr(triage, "datetime", Clock)
    data = tmp_path / "posts"
    captions = [
        "UTSFC 2026 is open for registration!\nJoin us on November 15, 2026 at Hart House. Link in bio.",
        "The runway is ready for UTSFC 2026!\nJoin us for our case competition.\nNovember 15, 2026 | 9 AM - 5 PM\nHart House\nRegister now via link in bio.",
    ]
    for index, caption in enumerate(captions):
        post_id = f"post{index}"
        atomic_json(data / f"{post_id}.json", {
            "source": "instagram", "source_post_id": post_id, "organizer_id": "ds3_utsc",
            "source_url": f"https://www.instagram.com/p/{post_id}/",
            "posted_at": f"2026-10-2{index + 3}T16:00:00Z", "caption_raw": caption,
            "caption_status": "available", "media": [], "media_completeness": "partial",
            "first_seen_at": "2026-10-28T05:00:00Z", "last_seen_at": "2026-10-28T05:00:00Z",
            "last_fetched_at": "2026-10-28T05:00:00Z", "content_hash": str(index) * 64,
            "collection_status": "partial", "errors": ["Optional image missing"]})
    report = triage.reprocess(data, now)
    assert report["counts"] == {"auto_candidate": 1, "merged_duplicate": 1}
    group = read_json(data / "event_groups/post0.json")["draft"]["opportunity"]
    ledger = data / "review_overrides" / f"{group['opportunity_id']}.json"
    if human:
        atomic_json(ledger, {"content_revision": group["content_revision"],
                            "review_status": "approved", "field_overrides": {},
                            "updated_at": "2026-10-28T05:30:00Z"})
    base = read_json(FIXTURES / "valid_bundle.json")
    market = write_base_package(tmp_path / "market", base)
    package = build_package(market, tmp_path / "candidate", dataset_version="2.0.0", data_dir=data)
    bundle = validate_package(package, require_provenance=True)
    assert len(bundle["opportunities"]) == 1
    with pytest.raises(BundleValidationError, match="unverified organizer"):
        validate_package(package, for_publication=True)
    item = bundle["opportunities"][0]
    assert item["source_post_ids"] == ["post0", "post1"]
    assert item["start_at"] == "2026-11-15T14:00:00Z"
    assert item["end_at"] == "2026-11-15T22:00:00Z"
    assert item["review_status"] == "approved"
    eligibility = read_json(package / "dataset_provenance.json")["datasets"]["opportunities"]["records"][0]["eligibility"]
    assert eligibility["decision"] == ("human_approved" if human else "auto_candidate")
    assert read_json(data / "drafts/post0.json")["opportunity"]["review_status"] != "approved"
    assert read_json(data / "post0.json")["collection_status"] == "partial"
    if not human:
        assert not ledger.exists()
    # Current rejection cannot be bypassed by the automatic path.
    atomic_json(ledger, {"content_revision": group["content_revision"],
                        "review_status": "rejected", "field_overrides": {}})
    assert _current_opportunities(data, read_json(ROOT / "resources/organizers.json"))[0] == []
    ledger.unlink()
    # Changing a contributing source invalidates the merged candidate.
    raw = read_json(data / "post1.json")
    raw["content_hash"] = "f" * 64
    atomic_json(data / "post1.json", raw)
    assert effective_publishable_opportunity(read_json(data / "drafts/post0.json"), data, now) is None


@pytest.mark.parametrize("fields, now_text, expected", [
    ({"deadline_at": "2026-10-28T13:00:00Z"}, "2026-10-28T16:00:00Z", "expired_deadline"),
    ({"deadline_at": "2026-10-28T16:00:00Z"}, "2026-10-28T16:00:00Z", "expired_deadline"),
    ({"start_date": "2026-10-27", "end_date": "2026-10-29"}, "2026-10-28T16:00:00Z", None),
    ({"start_date": "2026-10-27", "end_at": "2026-10-29T16:00:00Z"}, "2026-10-28T16:00:00Z", None),
    ({"deadline_date": "2026-10-28"}, "2026-10-29T03:59:59Z", None),
    ({"deadline_date": "2026-10-28"}, "2026-10-29T04:00:00Z", "expired_deadline"),
    ({"start_date": "2026-10-28"}, "2026-10-28T23:00:00Z", None),
])
def test_shared_expiry_policy(fields, now_text, expected):
    from src.opportunities.temporal import expiration_reason
    assert expiration_reason({"kind": "event", **fields}, datetime.fromisoformat(now_text)) == expected


def test_publication_rechecks_exact_event_deadline(tmp_path):
    from src.export_bundle import _provenance, _write_package_files
    now = datetime.now(timezone.utc)
    base = read_json(FIXTURES / "valid_bundle.json")
    item = base["opportunities"][0]
    base["organizers"][0]["last_verified_at"] = "2026-10-27T12:00:00Z"
    # Keep all fields schema-valid, then place a deadline earlier on the same day.
    item.update(kind="event", deadline_date=now.date().isoformat(),
                deadline_at=now.replace(hour=0, minute=0, second=0).isoformat().replace("+00:00", "Z"),
                deadline_precision="datetime")
    rows = [{"opportunity_id": item["opportunity_id"], "eligibility": {
        "decision": "auto_candidate", "content_revision": item["content_revision"],
        "evaluated_at": now.isoformat()}, "source_post_ids": [
            {"source_post_id": post_id, "first_seen_at": now.isoformat(),
             "last_seen_at": now.isoformat(), "last_fetched_at": now.isoformat()}
            for post_id in item["source_post_ids"]]}]
    package = tmp_path / "candidate"
    package.mkdir()
    _write_package_files(package, base, _provenance(base, rows))
    with pytest.raises(BundleValidationError, match="expired_deadline"):
        validate_package(package, for_publication=True)

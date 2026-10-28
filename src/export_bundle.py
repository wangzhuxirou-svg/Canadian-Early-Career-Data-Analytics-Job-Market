"""Offline D7 package assembly, validation, and publication gate."""

import argparse
import hashlib
import json
import logging
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from src.market_export import _skill_catalog, calculate_market_statistics


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = ROOT / "contracts"
RESOURCES_DIR = ROOT / "resources"
DATA_DIR = ROOT / "data" / "opportunities"
LOGGER = logging.getLogger("src.export_bundle")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
ATTESTATION_CHECKS = (
    "role_classifications",
    "course_evidence",
    "opportunity_dates",
)


class BundleValidationError(ValueError):
    """Raised when a package is malformed or fails the publication gate."""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BundleValidationError(f"Cannot read valid JSON from {path}: {error}") from error


def _schemas(contracts_dir: Path) -> tuple[dict, Registry]:
    schema_files = list((contracts_dir / "schemas").glob("*.schema.json"))
    if not schema_files:
        raise BundleValidationError(f"No contract schemas found under {contracts_dir}.")
    schemas = [_read_json(path) for path in schema_files]
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas
    )
    return {schema["$id"].rsplit("/", 1)[-1]: schema for schema in schemas}, registry


def _unique_values(items: list[dict], field: str, dataset: str) -> set[str]:
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise BundleValidationError(f"{dataset} must be a list of objects.")
    values = [item.get(field) for item in items]
    if any(not isinstance(value, str) or not value for value in values):
        raise BundleValidationError(f"{dataset} contains a missing or invalid {field}.")
    if len(values) != len(set(values)):
        raise BundleValidationError(f"{dataset} contains duplicate {field} values.")
    return set(values)


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise BundleValidationError(f"{field} is not a valid timestamp: {value!r}.") from error
    if parsed.tzinfo is None:
        raise BundleValidationError(f"{field} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _ensure_disjoint_paths(source: Path, destination: Path, message: str) -> None:
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    try:
        destination.relative_to(source)
        overlaps = True
    except ValueError:
        try:
            source.relative_to(destination)
            overlaps = True
        except ValueError:
            overlaps = False
    if overlaps:
        raise ValueError(message)


def _check_date_order(bundle: dict) -> None:
    manifest = bundle["manifest"]
    collected_from, collected_to = manifest["collected_from"], manifest["collected_to"]
    if collected_from and collected_to:
        if _parse_timestamp(collected_from, "manifest.collected_from") > _parse_timestamp(
            collected_to, "manifest.collected_to"
        ):
            raise BundleValidationError("Market collection start is later than its end.")
    posting_from, posting_to = manifest["posting_from"], manifest["posting_to"]
    if posting_from and posting_to and date.fromisoformat(posting_from) > date.fromisoformat(posting_to):
        raise BundleValidationError("Market posting start is later than its end.")

    for job in bundle["market_jobs"]:
        first_seen = _parse_timestamp(job["first_seen_at"], "market job first_seen_at")
        last_seen = _parse_timestamp(job["last_seen_at"], "market job last_seen_at")
        if last_seen < first_seen:
            raise BundleValidationError(f"Market job {job['job_key']} has reversed seen dates.")
        if job["posted_at"] and job["posted_date"]:
            posted_at = _parse_timestamp(job["posted_at"], "market job posted_at")
            if posted_at.date() != date.fromisoformat(job["posted_date"]):
                raise BundleValidationError(
                    f"Market job {job['job_key']} posted_at does not match posted_date."
                )

    for opportunity in bundle["opportunities"]:
        for start_key, end_key in (("start_date", "end_date"), ("start_at", "end_at")):
            start, end = opportunity[start_key], opportunity[end_key]
            if not start or not end:
                continue
            if start_key.endswith("_at"):
                ordered = _parse_timestamp(start, start_key) <= _parse_timestamp(end, end_key)
            else:
                ordered = date.fromisoformat(start) <= date.fromisoformat(end)
            if not ordered:
                raise BundleValidationError(
                    f"Opportunity {opportunity['opportunity_id']} has an end before its start."
                )


def validate_bundle(
    bundle: dict,
    contracts_dir: Path = CONTRACTS_DIR,
    *,
    for_publication: bool = False,
) -> None:
    """Validate shared JSON Schemas plus package-wide uniqueness and references."""
    schemas, registry = _schemas(Path(contracts_dir))
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(
        schemas["bundle.schema.json"],
        registry=registry,
        format_checker=FormatChecker(),
    )
    errors = sorted(
        validator.iter_errors(bundle),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        details = "; ".join(
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: "
            f"{error.message}"
            for error in errors[:10]
        )
        raise BundleValidationError(f"Bundle contract validation failed: {details}")

    manifest = bundle["manifest"]
    if not VERSION_PATTERN.fullmatch(manifest["dataset_version"]):
        raise BundleValidationError("dataset_version must use semantic version format.")
    if manifest["run_status"] == "failed":
        raise BundleValidationError("Failed market runs cannot be packaged.")
    if manifest["sample_count"] != len(bundle["market_jobs"]):
        raise BundleValidationError("manifest.sample_count does not match market_jobs.")

    job_keys = _unique_values(bundle["market_jobs"], "job_key", "market_jobs")
    if len(job_keys) != len(bundle["market_jobs"]):
        raise BundleValidationError("market_jobs contains duplicate IDs.")
    for job in bundle["market_jobs"]:
        if job["source_job_id"] is None or job["job_key"] != (
            f"{job['source']}:{job['source_job_id']}"
        ):
            raise BundleValidationError(f"Market job key does not match its source ID: {job['job_key']}.")
    skill_ids = _unique_values(bundle["skills"], "skill_id", "skills")
    organizer_ids = _unique_values(bundle["organizers"], "organizer_id", "organizers")
    _unique_values(bundle["courses"], "course_id", "courses")
    _unique_values(bundle["resources"], "resource_id", "resources")
    _unique_values(bundle["opportunities"], "opportunity_id", "opportunities")

    referenced_skills = {
        skill_id
        for job in bundle["market_jobs"]
        for skill_id in job["technical_skill_ids"] + job["soft_skill_ids"]
    }
    referenced_skills.update(
        skill_id
        for course in bundle["courses"]
        for skill_id in course["candidate_skill_ids"]
    )
    referenced_skills.update(
        skill_id
        for resource in bundle["resources"] + bundle["opportunities"]
        for skill_id in resource["skill_ids"]
    )
    if not referenced_skills <= skill_ids:
        missing = sorted(referenced_skills - skill_ids)
        raise BundleValidationError(f"Bundle references unknown skill IDs: {missing[:10]}.")
    if any(item["organizer_id"] not in organizer_ids for item in bundle["opportunities"]):
        raise BundleValidationError("An opportunity references an unknown organizer.")

    source_post_ids = [
        post_id
        for item in bundle["opportunities"]
        for post_id in item["source_post_ids"]
    ]
    if len(source_post_ids) != len(set(source_post_ids)):
        raise BundleValidationError("A source post is assigned to multiple opportunities.")
    for opportunity in bundle["opportunities"]:
        if opportunity["review_status"] != "approved" or opportunity["change_pending"]:
            raise BundleValidationError(
                f"Opportunity {opportunity['opportunity_id']} is not currently approved."
            )

    _check_date_order(bundle)
    if for_publication and manifest["run_status"] != "complete":
        raise BundleValidationError(
            "Publication is blocked because the reused market dataset is not complete."
        )
    if for_publication:
        organizer_by_id = {
            organizer["organizer_id"]: organizer for organizer in bundle["organizers"]
        }
        for opportunity in bundle["opportunities"]:
            organizer = organizer_by_id[opportunity["organizer_id"]]
            if not organizer["enabled"] or organizer["last_verified_at"] is None:
                raise BundleValidationError(
                    f"Opportunity {opportunity['opportunity_id']} has an unverified organizer."
                )


def _verify_checksums(package_dir: Path) -> None:
    checksum_path = package_dir / "SHA256SUMS.txt"
    if not checksum_path.exists():
        raise BundleValidationError(f"Package checksum file is missing: {checksum_path}.")
    entries = {}
    for line in checksum_path.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        if not match:
            raise BundleValidationError("Package checksum file has an invalid entry.")
        digest, filename = match.groups()
        if filename in entries:
            raise BundleValidationError(f"Duplicate checksum entry for {filename}.")
        entries[filename] = digest
    if "bundle.json" not in entries:
        raise BundleValidationError("Package checksum file does not cover bundle.json.")
    for filename, expected in entries.items():
        path = (package_dir / filename).resolve()
        if path.parent != package_dir.resolve() or not path.is_file():
            raise BundleValidationError(f"Checksum entry references a missing or unsafe file: {filename}.")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise BundleValidationError(f"Package checksum mismatch: {filename}.")


def _validate_provenance(bundle: dict, provenance: dict) -> None:
    if not isinstance(provenance, dict):
        raise BundleValidationError("Dataset provenance must be an object.")
    if provenance.get("generated_at") != bundle["manifest"]["generated_at"]:
        raise BundleValidationError("Provenance generated_at does not match the bundle manifest.")
    datasets = provenance.get("datasets")
    if not isinstance(datasets, dict):
        raise BundleValidationError("Dataset provenance has no datasets object.")
    market = datasets.get("market_jobs", {})
    if not isinstance(market, dict):
        raise BundleValidationError("Market provenance must be an object.")
    if (
        market.get("source") != "jsearch"
        or market.get("collected_from") != bundle["manifest"]["collected_from"]
        or market.get("collected_to") != bundle["manifest"]["collected_to"]
    ):
        raise BundleValidationError("Market provenance does not match the reused JSearch data.")

    opportunities = datasets.get("opportunities", {})
    if not isinstance(opportunities, dict):
        raise BundleValidationError("Opportunity provenance must be an object.")
    collection_rows = opportunities.get("records")
    if not isinstance(collection_rows, list):
        raise BundleValidationError("Opportunity collection provenance must be a list.")
    by_opportunity = _unique_values(
        collection_rows, "opportunity_id", "opportunity provenance"
    )
    expected_opportunities = {
        item["opportunity_id"]: set(item["source_post_ids"])
        for item in bundle["opportunities"]
    }
    if by_opportunity != set(expected_opportunities):
        raise BundleValidationError("Opportunity provenance does not match approved opportunities.")
    all_first_seen, all_last_seen = [], []
    for row in collection_rows:
        if not isinstance(row, dict):
            raise BundleValidationError("Opportunity provenance records must be objects.")
        eligibility = row.get("eligibility")
        if not isinstance(eligibility, dict) or eligibility.get("decision") not in {
            "auto_candidate", "human_approved"
        }:
            raise BundleValidationError("Opportunity provenance must identify automatic or human eligibility.")
        opportunity = next(item for item in bundle["opportunities"]
                           if item["opportunity_id"] == row["opportunity_id"])
        if eligibility.get("content_revision") != opportunity["content_revision"]:
            raise BundleValidationError("Opportunity eligibility revision does not match the bundle.")
        _parse_timestamp(eligibility.get("evaluated_at"), "eligibility.evaluated_at")
        post_rows = row.get("source_post_ids")
        if not isinstance(post_rows, list) or any(
            not isinstance(post, dict) for post in post_rows
        ):
            raise BundleValidationError("Opportunity source-post provenance must be a list.")
        post_ids = _unique_values(post_rows, "source_post_id", "opportunity source-post provenance")
        if post_ids != expected_opportunities[row["opportunity_id"]]:
            raise BundleValidationError("Opportunity source-post provenance does not match its bundle record.")
        for post in post_rows:
            first = _parse_timestamp(post.get("first_seen_at"), "opportunity first_seen_at")
            last = _parse_timestamp(post.get("last_seen_at"), "opportunity last_seen_at")
            fetched = _parse_timestamp(post.get("last_fetched_at"), "opportunity last_fetched_at")
            if first > last or fetched < last:
                raise BundleValidationError(
                    f"Opportunity source post {post['source_post_id']} has inconsistent collection times."
                )
            all_first_seen.append(post["first_seen_at"])
            all_last_seen.append(post["last_seen_at"])
    expected_from = min(all_first_seen, default=None)
    expected_to = max(all_last_seen, default=None)
    if opportunities.get("collected_from") != expected_from or opportunities.get("collected_to") != expected_to:
        raise BundleValidationError("Opportunity dataset collection range is incorrect.")

    for dataset_name, id_field, bundle_key in (
        ("courses", "course_id", "courses"),
        ("resources", "resource_id", "resources"),
        ("organizers", "organizer_id", "organizers"),
    ):
        item = datasets.get(dataset_name, {})
        if not isinstance(item, dict):
            raise BundleValidationError(f"{dataset_name} provenance must be an object.")
        records = item.get("records")
        if item.get("timestamp_field") != "verified_at" and dataset_name != "organizers":
            raise BundleValidationError(f"{dataset_name} must identify verified_at timestamps.")
        if dataset_name == "organizers" and item.get("timestamp_field") != "last_verified_at":
            raise BundleValidationError("Organizers must identify last_verified_at timestamps.")
        if not isinstance(records, list):
            raise BundleValidationError(f"{dataset_name} provenance records must be a list.")
        ids = _unique_values(records, id_field, f"{dataset_name} provenance")
        expected = {
            record[id_field]: record["last_verified_at" if dataset_name == "organizers" else "verified_at"]
            for record in bundle[bundle_key]
        }
        actual = {
            record[id_field]: record.get("last_verified_at" if dataset_name == "organizers" else "verified_at")
            for record in records
        }
        if ids != set(expected) or actual != expected:
            raise BundleValidationError(f"{dataset_name} provenance does not match bundle records.")
        for value in actual.values():
            if value is not None:
                _parse_timestamp(value, f"{dataset_name} verification timestamp")


def validate_package(
    package_dir: Path,
    contracts_dir: Path = CONTRACTS_DIR,
    *,
    for_publication: bool = False,
    require_provenance: bool = False,
) -> dict:
    package_dir = Path(package_dir)
    _verify_checksums(package_dir)
    bundle = _read_json(package_dir / "bundle.json")
    validate_bundle(bundle, contracts_dir, for_publication=for_publication)
    for filename, key in (
        ("manifest.json", "manifest"),
        ("market_jobs.json", "market_jobs"),
        ("skills.json", "skills"),
        ("organizers.json", "organizers"),
        ("courses.json", "courses"),
        ("learning_resources.json", "resources"),
        ("opportunities.json", "opportunities"),
    ):
        path = package_dir / filename
        if path.exists() and _read_json(path) != bundle[key]:
            raise BundleValidationError(f"{filename} does not match bundle.json.")
    provenance_path = package_dir / "dataset_provenance.json"
    if for_publication:
        require_provenance = True
    if require_provenance and not provenance_path.is_file():
        raise BundleValidationError("Dataset collection provenance is missing.")
    if provenance_path.is_file():
        provenance = _read_json(provenance_path)
        _validate_provenance(bundle, provenance)
        if for_publication:
            from src.opportunities.temporal import expiration_reason
            for opportunity in bundle["opportunities"]:
                expired = expiration_reason(opportunity)
                if expired:
                    raise BundleValidationError(
                        f"Opportunity {opportunity['opportunity_id']} contains an expired date: {expired}."
                    )
    return bundle


def _current_opportunities(data_dir: Path, organizers: list[dict]) -> tuple[list[dict], list[dict]]:
    from src.opportunities.review import effective_publishable_opportunity
    from src.opportunities.drafts import has_usable_evidence

    organizer_by_id = {item["organizer_id"]: item for item in organizers}
    approved, collection_rows = [], []
    drafts_dir = data_dir / "drafts"
    if not drafts_dir.exists():
        return approved, collection_rows
    for draft_path in sorted(drafts_dir.glob("*.json")):
        draft = _read_json(draft_path)
        candidate = effective_publishable_opportunity(draft, data_dir)
        if candidate is None:
            continue
        # Shared v1 bundle uses approved for release eligibility, not proof of a
        # human review. Preserve the actual decision in dataset provenance.
        opportunity = {**candidate["opportunity"], "review_status": "approved", "change_pending": False}
        organizer = organizer_by_id.get(opportunity["organizer_id"])
        if not organizer or not organizer.get("enabled"):
            LOGGER.warning(
                "Omitting approved opportunity %s: organizer is missing or disabled.",
                opportunity["opportunity_id"],
            )
            continue
        if not organizer.get("last_verified_at"):
            candidate["eligibility"].setdefault("warnings", []).append("organizer_verification_pending")
        raw_post_ids = opportunity["source_post_ids"] or [opportunity["source_post_id"]]
        post_collections = []
        for post_id in raw_post_ids:
            raw_path = data_dir / f"{post_id}.json"
            if not raw_path.is_file():
                raise BundleValidationError(
                    f"Approved opportunity {opportunity['opportunity_id']} lacks source snapshot {post_id}."
                )
            raw = _read_json(raw_path)
            if not has_usable_evidence(raw, data_dir):
                raise BundleValidationError(
                    f"Approved opportunity {opportunity['opportunity_id']} has no usable source evidence."
                )
            first_seen = raw.get("first_seen_at")
            last_seen = raw.get("last_seen_at")
            last_fetched = raw.get("last_fetched_at")
            if not first_seen or not last_seen or not last_fetched:
                raise BundleValidationError(f"Source snapshot {post_id} lacks collection timestamps.")
            if _parse_timestamp(first_seen, f"{post_id}.first_seen_at") > _parse_timestamp(
                last_seen, f"{post_id}.last_seen_at"
            ):
                raise BundleValidationError(f"Source snapshot {post_id} has reversed collection timestamps.")
            if _parse_timestamp(last_fetched, f"{post_id}.last_fetched_at") < _parse_timestamp(
                last_seen, f"{post_id}.last_seen_at"
            ):
                raise BundleValidationError(f"Source snapshot {post_id} was fetched before it was last seen.")
            post_collections.append(
                {
                    "source_post_id": post_id,
                    "first_seen_at": first_seen,
                    "last_seen_at": last_seen,
                    "last_fetched_at": raw["last_fetched_at"],
                }
            )
        approved.append(opportunity)
        collection_rows.append(
            {
                "opportunity_id": opportunity["opportunity_id"],
                "source_post_ids": post_collections,
                "eligibility": candidate["eligibility"],
            }
        )
    return approved, collection_rows


def _provenance(bundle: dict, opportunity_collections: list[dict]) -> dict:
    manifest = bundle["manifest"]
    posts = [
        post
        for record in opportunity_collections
        for post in record["source_post_ids"]
    ]
    return {
        "generated_at": manifest["generated_at"],
        "datasets": {
            "market_jobs": {
                "source": "jsearch",
                "collected_from": manifest["collected_from"],
                "collected_to": manifest["collected_to"],
            },
            "opportunities": {
                "source": "instagram",
                "collected_from": min(
                    (post["first_seen_at"] for post in posts), default=None
                ),
                "collected_to": max(
                    (post["last_seen_at"] for post in posts), default=None
                ),
                "records": opportunity_collections,
            },
            "courses": {
                "timestamp_field": "verified_at",
                "records": [
                    {"course_id": item["course_id"], "verified_at": item["verified_at"]}
                    for item in bundle["courses"]
                ],
            },
            "resources": {
                "timestamp_field": "verified_at",
                "records": [
                    {"resource_id": item["resource_id"], "verified_at": item["verified_at"]}
                    for item in bundle["resources"]
                ],
            },
            "organizers": {
                "timestamp_field": "last_verified_at",
                "records": [
                    {
                        "organizer_id": item["organizer_id"],
                        "last_verified_at": item["last_verified_at"],
                    }
                    for item in bundle["organizers"]
                ],
            },
        },
    }


def _write_checksums(package_dir: Path) -> None:
    checksums = []
    for path in sorted(package_dir.iterdir()):
        if path.name == "SHA256SUMS.txt":
            continue
        checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (package_dir / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="ascii")


def _write_package_files(package_dir: Path, bundle: dict, provenance: dict) -> None:
    files = {
        "bundle.json": bundle,
        "manifest.json": bundle["manifest"],
        "market_jobs.json": bundle["market_jobs"],
        "skills.json": bundle["skills"],
        "organizers.json": bundle["organizers"],
        "courses.json": bundle["courses"],
        "learning_resources.json": bundle["resources"],
        "opportunities.json": bundle["opportunities"],
        "reference_statistics.json": calculate_market_statistics(bundle["market_jobs"]),
        "dataset_provenance.json": provenance,
        "release_attestation.template.json": {
            "dataset_version": bundle["manifest"]["dataset_version"],
            "reviewer": "",
            "confirmed_at": "",
            "confirmations": {name: False for name in ATTESTATION_CHECKS},
        },
    }
    for filename, value in files.items():
        (package_dir / filename).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    _write_checksums(package_dir)


def _load_market_bundle(
    package_dir: Path,
    contracts_dir: Path,
    upgrade_legacy_manifest: bool,
) -> dict:
    package_dir = Path(package_dir)
    _verify_checksums(package_dir)
    bundle = _read_json(package_dir / "bundle.json")
    manifest_schema = _read_json(
        Path(contracts_dir) / "schemas" / "manifest.schema.json"
    )
    current_schema_version = manifest_schema["properties"]["schema_version"]["const"]
    if bundle["manifest"].get("schema_version") != current_schema_version:
        if (
            bundle["manifest"].get("schema_version") != "1.0.0"
            or current_schema_version != "1.1.0"
            or not upgrade_legacy_manifest
        ):
            raise BundleValidationError(
                "Market package schema version is stale or unsupported. "
                "Use the explicit legacy manifest upgrade only for schema 1.0.0."
            )
        LOGGER.warning(
            "Upgrading only the legacy manifest schema version from 1.0.0 to 1.1.0; "
            "all package data will be revalidated."
        )
        bundle["manifest"]["schema_version"] = current_schema_version
    validate_bundle(bundle, contracts_dir)
    for filename, key in (
        ("market_jobs.json", "market_jobs"),
        ("skills.json", "skills"),
        ("organizers.json", "organizers"),
        ("courses.json", "courses"),
        ("learning_resources.json", "resources"),
        ("opportunities.json", "opportunities"),
    ):
        sidecar = package_dir / filename
        if sidecar.exists() and _read_json(sidecar) != bundle[key]:
            raise BundleValidationError(f"{filename} does not match bundle.json.")
    manifest_path = package_dir / "manifest.json"
    if manifest_path.exists():
        manifest = _read_json(manifest_path)
        if bundle["manifest"]["schema_version"] != manifest.get("schema_version"):
            manifest["schema_version"] = bundle["manifest"]["schema_version"]
        if manifest != bundle["manifest"]:
            raise BundleValidationError("manifest.json does not match bundle.json.")
    return bundle


def build_package(
    market_package: Path,
    output_dir: Path,
    *,
    dataset_version: str,
    data_dir: Path = DATA_DIR,
    contracts_dir: Path = CONTRACTS_DIR,
    resources_dir: Path = RESOURCES_DIR,
    upgrade_legacy_manifest: bool = False,
) -> Path:
    """Create an offline release candidate from a validated market package."""
    if not VERSION_PATTERN.fullmatch(dataset_version):
        raise ValueError("dataset_version must use semantic version format.")
    market_package, output_dir = Path(market_package), Path(output_dir)
    _ensure_disjoint_paths(
        market_package,
        output_dir,
        "Package output must not overlap the input market package directory.",
    )
    base = _load_market_bundle(
        market_package,
        contracts_dir,
        upgrade_legacy_manifest,
    )
    bundle = json.loads(json.dumps(base))
    bundle["manifest"]["dataset_version"] = dataset_version
    bundle["manifest"]["generated_at"] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    resources_dir = Path(resources_dir)
    organizers = _read_json(resources_dir / "organizers.json")
    courses = _read_json(resources_dir / "courses.json")
    learning_resources = _read_json(resources_dir / "learning_resources.json")
    opportunities, collection_rows = _current_opportunities(Path(data_dir), organizers)
    referenced_organizers = {item["organizer_id"] for item in opportunities}
    bundle["organizers"] = [
        organizer
        for organizer in organizers
        if organizer["organizer_id"] in referenced_organizers
    ]
    bundle["opportunities"] = opportunities
    bundle["courses"] = courses
    bundle["resources"] = learning_resources
    bundle["skills"] = _skill_catalog(
        bundle["market_jobs"],
        courses,
        [
            *learning_resources,
            *({"skill_ids": item["skill_ids"]} for item in opportunities),
        ],
    )
    validate_bundle(bundle, contracts_dir)
    provenance = _provenance(bundle, collection_rows)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        raise FileExistsError(f"Package output already exists: {output_dir}")
    with tempfile.TemporaryDirectory(prefix=f".{output_dir.name}-", dir=output_dir.parent) as temp:
        staged = Path(temp) / "package"
        staged.mkdir()
        _write_package_files(staged, bundle, provenance)
        validate_package(staged, contracts_dir, require_provenance=True)
        try:
            staged.rename(output_dir)
        except FileExistsError as error:
            raise FileExistsError(f"Package output already exists: {output_dir}") from error
    LOGGER.info(
        "Built offline package %s (market status=%s, market jobs=%d, approved opportunities=%d).",
        output_dir,
        bundle["manifest"]["run_status"],
        len(bundle["market_jobs"]),
        len(opportunities),
    )
    return output_dir


def _validate_attestation(path: Path, dataset_version: str, generated_at: str) -> None:
    attestation = _read_json(path)
    if attestation.get("dataset_version") != dataset_version:
        raise BundleValidationError("Attestation dataset_version does not match the package.")
    if not isinstance(attestation.get("reviewer"), str) or not attestation["reviewer"].strip():
        raise BundleValidationError("Attestation must identify the human reviewer.")
    confirmed_at = _parse_timestamp(attestation.get("confirmed_at"), "attestation.confirmed_at")
    if confirmed_at > datetime.now(timezone.utc):
        raise BundleValidationError("Attestation confirmed_at cannot be in the future.")
    if confirmed_at < _parse_timestamp(generated_at, "manifest.generated_at"):
        raise BundleValidationError("Attestation must be made after the candidate package was built.")
    confirmations = attestation.get("confirmations")
    if not isinstance(confirmations, dict) or any(
        confirmations.get(name) is not True for name in ATTESTATION_CHECKS
    ):
        raise BundleValidationError(
            "Publication requires human confirmation of role classifications, "
            "course evidence, and opportunity dates."
        )


def publish_package(
    package_dir: Path,
    output_dir: Path,
    attestation_path: Path,
    contracts_dir: Path = CONTRACTS_DIR,
) -> Path:
    bundle = validate_package(
        package_dir,
        contracts_dir,
        for_publication=True,
        require_provenance=True,
    )
    _validate_attestation(
        Path(attestation_path),
        bundle["manifest"]["dataset_version"],
        bundle["manifest"]["generated_at"],
    )
    output_dir = Path(output_dir)
    _ensure_disjoint_paths(
        Path(package_dir),
        output_dir,
        "Published output must not overlap the candidate package directory.",
    )
    if output_dir.exists():
        raise FileExistsError(f"Published package output already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output_dir.name}-", dir=output_dir.parent) as temp:
        staged = Path(temp) / "package"
        shutil.copytree(package_dir, staged)
        shutil.copy2(attestation_path, staged / "release_attestation.json")
        _write_checksums(staged)
        validate_package(
            staged,
            contracts_dir,
            for_publication=True,
            require_provenance=True,
        )
        try:
            staged.rename(output_dir)
        except FileExistsError as error:
            raise FileExistsError(f"Published package output already exists: {output_dir}") from error
    LOGGER.info("Published verified package to %s.", output_dir)
    return output_dir


def _log_config(path: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and validate offline D7 data packages.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="Build a validated offline package.")
    build_parser.add_argument("--market-package", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    build_parser.add_argument("--dataset-version", required=True)
    build_parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    build_parser.add_argument(
        "--upgrade-legacy-manifest",
        action="store_true",
        help="Explicitly upgrade a validated 1.0.0 manifest to current 1.1.0.",
    )
    build_parser.add_argument("--log-file", type=Path)
    validate_parser = subparsers.add_parser("validate", help="Validate a package without publishing.")
    validate_parser.add_argument("--package", type=Path, required=True)
    validate_parser.add_argument("--for-publication", action="store_true")
    validate_parser.add_argument("--attestation", type=Path)
    validate_parser.add_argument("--log-file", type=Path)
    publish_parser = subparsers.add_parser("publish", help="Copy a package after all release gates pass.")
    publish_parser.add_argument("--package", type=Path, required=True)
    publish_parser.add_argument("--output", type=Path, required=True)
    publish_parser.add_argument("--attestation", type=Path, required=True)
    publish_parser.add_argument("--log-file", type=Path)
    schedule_parser = subparsers.add_parser(
        "schedule", help="Print an offline build command for Windows Task Scheduler."
    )
    schedule_parser.add_argument("--market-package", type=Path, required=True)
    schedule_parser.add_argument("--output", type=Path, required=True)
    schedule_parser.add_argument("--dataset-version", required=True)
    schedule_parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    schedule_parser.add_argument("--upgrade-legacy-manifest", action="store_true")
    arguments = parser.parse_args(argv)
    _log_config(getattr(arguments, "log_file", None))
    try:
        if arguments.command == "build":
            result = build_package(
                arguments.market_package,
                arguments.output,
                dataset_version=arguments.dataset_version,
                data_dir=arguments.data_dir,
                upgrade_legacy_manifest=arguments.upgrade_legacy_manifest,
            )
            bundle = validate_package(result)
            print(
                json.dumps(
                    {
                        "status": "built",
                        "package": str(result),
                        "market_status": bundle["manifest"]["run_status"],
                        "market_complete": bundle["manifest"]["run_status"] == "complete",
                        "publication_requires_attestation": True,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if arguments.command == "validate":
            bundle = validate_package(arguments.package, for_publication=arguments.for_publication)
            if arguments.for_publication:
                if arguments.attestation is None:
                    raise BundleValidationError(
                        "--for-publication requires the version-matched human --attestation."
                    )
                _validate_attestation(
                    arguments.attestation,
                    bundle["manifest"]["dataset_version"],
                    bundle["manifest"]["generated_at"],
                )
            print(
                json.dumps(
                    {
                        "status": "valid",
                        "dataset_version": bundle["manifest"]["dataset_version"],
                        "market_status": bundle["manifest"]["run_status"],
                    }
                )
            )
            return 0
        if arguments.command == "publish":
            result = publish_package(
                arguments.package, arguments.output, arguments.attestation
            )
            print(json.dumps({"status": "published", "package": str(result)}))
            return 0
        command = (
            f'"{sys.executable}" -m src.export_bundle build '
            f'--market-package "{arguments.market_package}" '
            f'--output "{arguments.output}" '
            f'--dataset-version "{arguments.dataset_version}" '
            f'--data-dir "{arguments.data_dir}"'
        )
        if arguments.upgrade_legacy_manifest:
            command += " --upgrade-legacy-manifest"
        print("Offline D7 build command for a scheduled task (no JSearch/API call):")
        print(command)
        print("Use a unique output path/version for each run; publication still requires manual attestation.")
        return 0
    except (BundleValidationError, ValidationError) as error:
        LOGGER.error("%s", error)
        return 1
    except (OSError, ValueError) as error:
        LOGGER.error("%s", error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

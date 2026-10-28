import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource
from src.market_export import export_market_bundle, to_market_job
from config.settings import SOFT_SKILL_KEYWORDS, TECHNICAL_SKILLS_KEYWORDS


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = ROOT / "contracts" / "schemas"
FIXTURES_DIR = ROOT / "contracts" / "fixtures"
RESOURCES_DIR = ROOT / "resources"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def schemas_and_registry():
    schemas = {
        path.name.removesuffix(".schema.json"): load_json(path)
        for path in SCHEMAS_DIR.glob("*.schema.json")
    }
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas.values()
    )
    return schemas, registry


def validate(instance, schema, registry):
    Draft202012Validator(
        schema,
        registry=registry,
        format_checker=FormatChecker(),
    ).validate(instance)


def test_all_contract_schemas_are_valid(schemas_and_registry):
    schemas, _ = schemas_and_registry
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(
    ("fixture_name", "schema_name"),
    [
        ("valid_organizer.json", "organizer"),
        ("valid_raw_post.json", "raw-post"),
        ("valid_ocr_result.json", "ocr-result"),
        ("valid_opportunity_draft.json", "opportunity-draft"),
        ("valid_review_patch.json", "review-patch"),
        ("valid_experience_skill_candidates.json", "experience-skill-extraction"),
        ("private_user_record.json", "user-record"),
    ],
)
def test_ingestion_fixtures_match_contracts(fixture_name, schema_name, schemas_and_registry):
    schemas, registry = schemas_and_registry
    validate(load_json(FIXTURES_DIR / fixture_name), schemas[schema_name], registry)


@pytest.mark.parametrize("fixture_name", ["valid_bundle.json", "empty_bundle.json"])
def test_bundle_fixtures_match_contract(fixture_name, schemas_and_registry):
    schemas, registry = schemas_and_registry
    bundle = load_json(FIXTURES_DIR / fixture_name)
    validate(bundle, schemas["bundle"], registry)
    assert bundle["manifest"]["sample_count"] == len(bundle["market_jobs"])


@pytest.mark.parametrize(
    ("filename", "schema_name", "id_field", "skill_field"),
    [
        ("courses.json", "course", "course_id", "candidate_skill_ids"),
        ("learning_resources.json", "resource", "resource_id", "skill_ids"),
    ],
)
def test_d5_reference_files_match_contract_and_catalog(
    filename, schema_name, id_field, skill_field, schemas_and_registry
):
    schemas, registry = schemas_and_registry
    records = load_json(RESOURCES_DIR / filename)
    valid_skills = set(TECHNICAL_SKILLS_KEYWORDS) | set(SOFT_SKILL_KEYWORDS)
    ids = [record[id_field] for record in records]

    assert len(ids) == len(set(ids))
    for record in records:
        validate(record, schemas[schema_name], registry)
        assert set(record[skill_field]) <= valid_skills
        if schema_name == "resource" and record[skill_field]:
            assert record.get("mapping_evidence")


def test_d6_organizer_targets_match_contract_without_claiming_verification(
    schemas_and_registry,
):
    schemas, registry = schemas_and_registry
    organizers = load_json(RESOURCES_DIR / "organizers.json")
    organizer_ids = [record["organizer_id"] for record in organizers]
    usernames = [record["username"].casefold() for record in organizers]

    assert len(organizers) == 21
    assert len(organizer_ids) == len(set(organizer_ids))
    assert len(usernames) == len(set(usernames))
    for organizer in organizers:
        validate(organizer, schemas["organizer"], registry)
        assert organizer["enabled"] is True
        assert organizer["last_verified_at"] is None


def test_market_package_includes_d5_references_and_their_skills(tmp_path):
    package_dir = export_market_bundle(
        [{
            "job_id": "d5-package-job",
            "source": "jsearch",
            "job_title": "Data Analyst Intern",
            "classified_role_family": "data_analyst",
            "role_status": "target",
            "classified_career_level": "internship",
            "market_area": "unknown",
            "collected_at_utc": "2026-10-11T12:00:00Z",
            "job_description": None,
            "technical_skills": ["python"],
            "soft_skills": [],
        }],
        tmp_path / "package",
        dataset_version="1.0.1",
        query_config_version="jsearch-role-career-matrix-v1",
    )
    bundle = load_json(package_dir / "bundle.json")
    skills = {skill["skill_id"] for skill in bundle["skills"]}

    assert bundle["courses"] == load_json(RESOURCES_DIR / "courses.json")
    assert bundle["resources"] == load_json(RESOURCES_DIR / "learning_resources.json")
    assert "r" in skills
    assert (package_dir / "courses.json").exists()
    assert (package_dir / "learning_resources.json").exists()


def test_resource_mapping_evidence_is_optional_for_existing_records(schemas_and_registry):
    schemas, registry = schemas_and_registry
    bundle = load_json(FIXTURES_DIR / "valid_bundle.json")
    resource = copy.deepcopy(bundle["resources"][0])
    assert resource["mapping_evidence"]
    validate(resource, schemas["resource"], registry)

    resource.pop("mapping_evidence")
    validate(resource, schemas["resource"], registry)

    resource["mapping_evidence"] = None
    validate(resource, schemas["resource"], registry)

    resource["mapping_evidence"] = ""
    with pytest.raises(ValidationError):
        validate(resource, schemas["resource"], registry)


def test_market_statistics_golden_fixture_matches_contracts(schemas_and_registry):
    schemas, registry = schemas_and_registry
    fixture = load_json(FIXTURES_DIR / "market_statistics_cases.json")
    for job in fixture["market_jobs"]:
        validate(job, schemas["market-job"], registry)
    for case in fixture["cases"]:
        validate(case["expected"], schemas["market-statistics"], registry)


def test_python_market_export_emits_a_contract_valid_job(schemas_and_registry):
    schemas, registry = schemas_and_registry
    job = to_market_job({
        "job_id": "contract-export-1",
        "job_title": "Data Analyst Intern",
        "classified_role_family": "data_analyst",
        "role_status": "target",
        "classified_career_level": "internship",
        "market_area": "Toronto GTA",
        "job_posted_at_datetime_utc": "2026-10-10T12:00:00Z",
        "collected_at_utc": "2026-10-11T12:00:00Z",
        "job_description": None,
        "technical_skills": ["python"],
        "soft_skills": [],
    })
    validate(job, schemas["market-job"], registry)


def test_course_skill_mapping_examples_are_explicit_review_candidates():
    rules = load_json(ROOT / "contracts" / "rules" / "course-skill-mapping.json")
    fixture = load_json(FIXTURES_DIR / "course-skill-mapping-cases.json")
    bundle = load_json(FIXTURES_DIR / "valid_bundle.json")
    skill_ids = {skill["skill_id"] for skill in bundle["skills"]}
    assert rules["evidence"]["human_review_required_before_publication"] is True
    assert rules["evidence"]["do_not_infer_from_course_title_alone"] is True
    assert all(case["source_type"] == "official_calendar_excerpt" for case in fixture["cases"])
    assert fixture["cases"][1]["expected_candidate_skill_ids"] == []
    assert {
        skill_id
        for case in fixture["cases"]
        for skill_id in case["expected_candidate_skill_ids"]
    } <= skill_ids


def test_bundle_rejects_bad_classification_dates_and_missing_descriptions(schemas_and_registry):
    schemas, registry = schemas_and_registry
    bundle = load_json(FIXTURES_DIR / "invalid_bundle.json")
    with pytest.raises(ValidationError):
        validate(bundle, schemas["bundle"], registry)


def test_failed_bundle_and_private_user_data_are_not_publishable(schemas_and_registry):
    schemas, registry = schemas_and_registry
    bundle = load_json(FIXTURES_DIR / "valid_bundle.json")
    bundle["manifest"]["run_status"] = "failed"
    with pytest.raises(ValidationError):
        validate(bundle, schemas["bundle"], registry)

    bundle = load_json(FIXTURES_DIR / "valid_bundle.json")
    bundle["user_records"] = [load_json(FIXTURES_DIR / "private_user_record.json")]
    with pytest.raises(ValidationError):
        validate(bundle, schemas["bundle"], registry)


def test_bundle_only_allows_approved_opportunities(schemas_and_registry):
    schemas, registry = schemas_and_registry
    bundle = copy.deepcopy(load_json(FIXTURES_DIR / "valid_bundle.json"))
    bundle["opportunities"][0]["review_status"] = "needs_review"
    with pytest.raises(ValidationError):
        validate(bundle, schemas["bundle"], registry)


def test_bundle_opportunity_organizers_are_resolvable():
    bundle = load_json(FIXTURES_DIR / "valid_bundle.json")
    organizer_id_list = [organizer["organizer_id"] for organizer in bundle["organizers"]]
    organizer_ids = set(organizer_id_list)
    assert len(organizer_ids) == len(organizer_id_list)
    assert {opportunity["organizer_id"] for opportunity in bundle["opportunities"]} <= organizer_ids


def test_opportunity_rejects_unsupported_compensation_and_ccr_statuses(schemas_and_registry):
    schemas, registry = schemas_and_registry
    for field in ("compensation_status", "ccr_status"):
        bundle = copy.deepcopy(load_json(FIXTURES_DIR / "valid_bundle.json"))
        bundle["opportunities"][0][field] = "unknown"
        with pytest.raises(ValidationError):
            validate(bundle, schemas["bundle"], registry)


def test_fixture_skill_references_are_resolvable():
    bundle = load_json(FIXTURES_DIR / "valid_bundle.json")
    experience_fixture = load_json(FIXTURES_DIR / "valid_experience_skill_candidates.json")
    skill_ids = {skill["skill_id"] for skill in bundle["skills"]}
    assert len(skill_ids) == len(bundle["skills"])
    referenced_skills = {
        skill_id
        for job in bundle["market_jobs"]
        for skill_id in job["technical_skill_ids"] + job["soft_skill_ids"]
    }
    referenced_skills.update(
        skill_id
        for record in bundle["courses"]
        for skill_id in record["candidate_skill_ids"]
    )
    referenced_skills.update(
        skill_id
        for record in bundle["resources"] + bundle["opportunities"]
        for skill_id in record["skill_ids"]
    )
    referenced_skills.update(candidate["skill_id"] for candidate in experience_fixture["candidates"])
    assert referenced_skills <= skill_ids


def test_shared_rules_are_versioned_json():
    rules_dir = ROOT / "contracts" / "rules"
    rules = [load_json(path) for path in rules_dir.glob("*.json")]
    assert {rule["rule_set_version"] for rule in rules} == {"1.0.0"}

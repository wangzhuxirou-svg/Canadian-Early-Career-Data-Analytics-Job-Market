import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = ROOT / "contracts" / "schemas"
FIXTURES_DIR = ROOT / "contracts" / "fixtures"


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

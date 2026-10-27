import json
import hashlib
import math
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from config.settings import (
    CORE_CAREER_LEVELS,
    CORE_ROLE_FAMILIES,
    MIN_DESCRIPTION_LENGTH,
    SOFT_SKILL_KEYWORDS,
    TECHNICAL_SKILLS_KEYWORDS,
)


MARKET_AREA_IDS = {
    'toronto gta': 'toronto_gta',
    'montreal metro': 'montreal_metro',
    'other canada': 'other_canada',
    'unknown': 'unknown',
    'toronto_gta': 'toronto_gta',
    'montreal_metro': 'montreal_metro',
    'other_canada': 'other_canada',
}


def _jsearch_source_job_id(record: dict) -> str | None:
    source_job_id = record.get('source_job_id') or record.get('job_id')
    return str(source_job_id) if source_job_id is not None else None


SOURCE_ID_ADAPTERS = {
    'jsearch': _jsearch_source_job_id,
}


def _skill_ids(record: dict, field: str) -> list[str]:
    values = record.get(field) or []
    if not isinstance(values, (list, tuple, set)):
        raise ValueError(f'{field} must be a list of skill IDs.')
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError(f'{field} must contain non-empty string skill IDs.')
    return sorted(set(values))


def _posting_date(record: dict) -> tuple[str | None, str | None, str]:
    value = record.get('posted_at') or record.get('job_posted_at_datetime_utc')
    if value is None:
        value = record.get('posted_date')
    if value is None:
        return None, None, 'unknown'
    if isinstance(value, date) and not isinstance(value, datetime):
        return None, value.isoformat(), 'date'
    text = str(value).strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError as error:
            raise ValueError(f'Invalid job posting date: {value!r}.') from error
        return None, parsed_date.isoformat(), 'date'
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError as error:
            raise ValueError(f'Invalid job posting date: {value!r}.') from error
        return None, parsed_date.isoformat(), 'date'
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return _timestamp_text(parsed), parsed.date().isoformat(), 'datetime'


def to_market_job(record: dict) -> dict:
    """Map a cleaned/featured JSearch record to the public market-job contract."""
    source, source_job_id = _identity(record)
    if not source_job_id:
        raise ValueError(f'Market record from {source!r} is missing its source job ID.')

    role_family = record.get('classified_role_family')
    allowed_role_labels = CORE_ROLE_FAMILIES | {'hybrid', 'adjacent_hybrid', 'other', 'outside_scope'}
    if role_family is not None and role_family not in allowed_role_labels:
        raise ValueError(f'Unsupported classified role family: {role_family!r}.')
    role_status = record.get('role_status')
    role_id = role_family if role_family in CORE_ROLE_FAMILIES and role_status == 'target' else None
    legacy_review_labels = {'hybrid', 'adjacent_hybrid', 'other', 'outside_scope'}
    classification_label = role_family if role_family in legacy_review_labels else None
    classification_reasons = []
    if role_status == 'outside_scope' or role_family == 'outside_scope':
        role_id = None
        role_status = 'outside_scope'
        classification_label = 'outside_scope'
    elif role_id is None:
        role_status = 'needs_review'
        if record.get('is_senior'):
            classification_reasons.append('seniority_conflict')
    else:
        role_status = 'classified'
        classification_label = None

    career_level = record.get('classified_career_level', 'unknown')
    if career_level not in CORE_CAREER_LEVELS and career_level not in {'unknown', 'potential_senior'}:
        raise ValueError(f'Unsupported classified career level: {career_level!r}.')
    career_level_status = 'classified' if career_level in CORE_CAREER_LEVELS else 'needs_review'

    area = str(record.get('market_area') or 'unknown').strip().lower()
    market_area = MARKET_AREA_IDS.get(area)
    if market_area is None:
        raise ValueError(f'Unsupported market area: {record.get("market_area")!r}.')

    posted_at, posted_date, posted_precision = _posting_date(record)
    collected_at = record.get('collected_at_utc')
    first_seen = record.get('first_seen_at') or collected_at
    last_seen = record.get('last_seen_at') or collected_at or first_seen
    if first_seen is None or last_seen is None:
        raise ValueError(f'Market record {source}:{source_job_id} is missing collection timestamps.')
    first_seen = _timestamp_text(_utc_datetime(first_seen))
    last_seen = _timestamp_text(_utc_datetime(last_seen))

    description = record.get('job_description')
    has_description = isinstance(description, str) and bool(description.strip())
    description_status = 'available' if has_description else 'missing'
    analyzable = (
        has_description
        and len(description.strip()) >= MIN_DESCRIPTION_LENGTH
        and record.get('description_analyzable', True) is not False
    )
    technical_skill_ids = _skill_ids(record, 'technical_skills')
    soft_skill_ids = _skill_ids(record, 'soft_skills')
    job_title = str(record.get('job_title') or '').strip()
    if not job_title:
        raise ValueError(f'Market record {source}:{source_job_id} is missing its job title.')
    role_subtype = record.get('classified_role_subtype') if role_id == 'financial_risk_analyst' else None
    if role_id == 'financial_risk_analyst' and role_subtype not in {'financial_analysis', 'risk_analysis'}:
        raise ValueError(f'Market record {source}:{source_job_id} has an invalid financial/risk subtype.')

    return {
        'job_key': f'{source}:{source_job_id}',
        'source': source,
        'source_job_id': source_job_id,
        'job_title': job_title,
        'employer_name': record.get('employer_name'),
        'source_url': record.get('source_url') or record.get('job_apply_link'),
        'role_id': role_id,
        'role_subtype': role_subtype,
        'role_classification_status': role_status,
        'role_classification_label': classification_label,
        'classification_reasons': classification_reasons,
        'career_level': career_level,
        'career_level_status': career_level_status,
        'market_area': market_area,
        'posted_at': posted_at,
        'posted_date': posted_date,
        'posted_precision': posted_precision,
        'source_timezone': record.get('source_timezone'),
        'first_seen_at': first_seen,
        'last_seen_at': last_seen,
        'technical_skill_ids': technical_skill_ids,
        'soft_skill_ids': soft_skill_ids,
        'description_status': description_status,
        'description_analyzable': analyzable,
    }


def calculate_market_statistics(
    market_jobs: list[dict],
    *,
    role_ids: list[str] | None = None,
    career_levels: list[str] | None = None,
    market_areas: list[str] | None = None,
    posted_from: str | None = None,
    posted_to: str | None = None,
    skill_ids: list[str] | None = None,
) -> dict:
    """Reference market counts; selectors OR within a field and AND across fields."""
    start = date.fromisoformat(posted_from) if posted_from else None
    end = date.fromisoformat(posted_to) if posted_to else None
    if start and end and start > end:
        raise ValueError('posted_from must be on or before posted_to.')

    selected_roles = set(role_ids or [])
    selected_levels = set(career_levels or [])
    selected_areas = set(market_areas or [])
    invalid_roles = selected_roles - CORE_ROLE_FAMILIES
    invalid_levels = selected_levels - CORE_CAREER_LEVELS
    invalid_areas = selected_areas - set(MARKET_AREA_IDS.values())
    if invalid_roles or invalid_levels or invalid_areas:
        raise ValueError(
            'Unsupported market-statistics filter values: '
            f'roles={sorted(invalid_roles)}, levels={sorted(invalid_levels)}, '
            f'areas={sorted(invalid_areas)}.'
        )
    if skill_ids is not None and any(not isinstance(skill_id, str) or not skill_id for skill_id in skill_ids):
        raise ValueError('skill_ids must contain non-empty strings.')
    sample = []
    for job in market_jobs:
        if job.get('role_classification_status') != 'classified' or job.get('role_id') not in CORE_ROLE_FAMILIES:
            continue
        if job.get('career_level_status') != 'classified' or job.get('career_level') not in CORE_CAREER_LEVELS:
            continue
        if selected_roles and job.get('role_id') not in selected_roles:
            continue
        if selected_levels and job.get('career_level') not in selected_levels:
            continue
        if selected_areas and job.get('market_area') not in selected_areas:
            continue
        if start or end:
            posted_date = job.get('posted_date')
            parsed_date = date.fromisoformat(posted_date) if posted_date else None
            if parsed_date is None or (start and parsed_date < start) or (end and parsed_date > end):
                continue
        sample.append(job)

    selected_skills = sorted(set(skill_ids or {
        skill_id
        for job in sample
        for skill_id in (job.get('technical_skill_ids') or []) + (job.get('soft_skill_ids') or [])
    }))
    analyzable_sample = [job for job in sample if job.get('description_analyzable') is True]
    skill_stats = []
    for skill_id in selected_skills:
        mention_count = sum(
            skill_id in set(job.get('technical_skill_ids') or []) | set(job.get('soft_skill_ids') or [])
            for job in analyzable_sample
        )
        denominator = len(analyzable_sample)
        skill_stats.append({
            'skill_id': skill_id,
            'mention_count': mention_count,
            'mention_rate': mention_count / denominator if denominator else None,
        })
    return {
        'job_count': len(sample),
        'analyzable_description_count': len(analyzable_sample),
        'skills': skill_stats,
    }


def _skill_catalog(market_jobs: list[dict]) -> list[dict]:
    categories = {
        **{skill_id: ('technical', aliases) for skill_id, aliases in TECHNICAL_SKILLS_KEYWORDS.items()},
        **{skill_id: ('soft', aliases) for skill_id, aliases in SOFT_SKILL_KEYWORDS.items()},
    }
    used_ids = {
        skill_id
        for job in market_jobs
        for skill_id in job['technical_skill_ids'] + job['soft_skill_ids']
    }
    labels = {
        'aws': 'AWS',
        'bi': 'BI',
        'gcp': 'Google Cloud Platform',
        'r': 'R',
        'sql': 'SQL',
        'power_bi': 'Power BI',
        'scikit_learn': 'scikit-learn',
    }
    catalog = []
    for skill_id in sorted(used_ids):
        category, aliases = categories.get(skill_id, ('technical', [skill_id.replace('_', ' ')]))
        catalog.append({
            'skill_id': skill_id,
            'label': labels.get(skill_id, skill_id.replace('_', ' ').title()),
            'category': category,
            'aliases': sorted(set(aliases)),
        })
    return catalog


def _date_range(values: list[str | None]) -> tuple[str | None, str | None]:
    dates = sorted(value for value in values if value)
    return (dates[0], dates[-1]) if dates else (None, None)


def _validate_bundle(bundle: dict, contracts_dir: Path) -> None:
    from jsonschema import Draft202012Validator, FormatChecker
    from referencing import Registry, Resource

    schemas_dir = contracts_dir / 'schemas'
    schemas = [
        json.loads(path.read_text(encoding='utf-8'))
        for path in schemas_dir.glob('*.schema.json')
    ]
    registry = Registry().with_resources(
        (schema['$id'], Resource.from_contents(schema))
        for schema in schemas
    )
    bundle_schema = next(schema for schema in schemas if schema['$id'].endswith('/bundle.schema.json'))
    validator = Draft202012Validator(
        bundle_schema,
        registry=registry,
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(bundle), key=lambda error: list(error.absolute_path))
    if errors:
        details = '; '.join(
            f'{"/".join(str(part) for part in error.absolute_path) or "<root>"}: {error.message}'
            for error in errors[:10]
        )
        raise ValueError(f'Generated market bundle violates the shared contract: {details}')

    job_keys = [job['job_key'] for job in bundle['market_jobs']]
    skill_ids = [skill['skill_id'] for skill in bundle['skills']]
    if len(job_keys) != len(set(job_keys)):
        raise ValueError('Generated market bundle contains duplicate job keys.')
    if len(skill_ids) != len(set(skill_ids)):
        raise ValueError('Generated market bundle contains duplicate skill IDs.')
    referenced_skills = {
        skill_id
        for job in bundle['market_jobs']
        for skill_id in job['technical_skill_ids'] + job['soft_skill_ids']
    }
    if not referenced_skills <= set(skill_ids):
        raise ValueError('Generated market bundle references skills absent from its catalog.')
    if bundle['manifest']['sample_count'] != len(bundle['market_jobs']):
        raise ValueError('Generated market bundle sample_count does not match market_jobs.')


def export_market_bundle(
    records: list[dict],
    output_dir: Path,
    *,
    dataset_version: str,
    query_config_version: str,
    run_status: str = 'partial',
    contracts_dir: Path | None = None,
) -> Path:
    """Write a validated, description-free contract bundle and reference statistics."""
    if run_status not in {'complete', 'partial'}:
        raise ValueError('Only complete or partial runs can be exported as a market bundle.')
    if not re.fullmatch(r'\d+\.\d+\.\d+', dataset_version):
        raise ValueError('dataset_version must use semantic version format.')
    if not query_config_version:
        raise ValueError('query_config_version must be non-empty.')

    market_jobs = [to_market_job(record) for record in records]
    market_jobs.sort(key=lambda job: job['job_key'])
    collected_from, collected_to = _date_range([
        _timestamp_text(_utc_datetime(record.get('first_seen_at') or record.get('collected_at_utc')))
        for record in records
    ] + [
        _timestamp_text(_utc_datetime(record.get('last_seen_at') or record.get('collected_at_utc')))
        for record in records
    ])
    posting_from, posting_to = _date_range([job['posted_date'] for job in market_jobs])
    skills = _skill_catalog(market_jobs)
    generated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    bundle = {
        'manifest': {
            'schema_version': '1.0.0',
            'dataset_version': dataset_version,
            'generated_at': generated_at,
            'collected_from': collected_from,
            'collected_to': collected_to,
            'posting_from': posting_from,
            'posting_to': posting_to,
            'query_config_version': query_config_version,
            'run_status': run_status,
            'sample_count': len(market_jobs),
        },
        'market_jobs': market_jobs,
        'skills': skills,
        'organizers': [],
        'courses': [],
        'resources': [],
        'opportunities': [],
    }
    contracts_dir = contracts_dir or Path(__file__).resolve().parents[1] / 'contracts'
    _validate_bundle(bundle, Path(contracts_dir))
    reference_statistics = calculate_market_statistics(market_jobs)

    output_dir = Path(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        raise FileExistsError(f'Market package output already exists: {output_dir}')
    temporary_dir_context = tempfile.TemporaryDirectory(
        prefix=f'.{output_dir.name}-',
        dir=output_dir.parent,
    )
    temporary_dir = Path(temporary_dir_context.name)
    files = {
        'bundle.json': bundle,
        'manifest.json': bundle['manifest'],
        'market_jobs.json': market_jobs,
        'skills.json': skills,
        'reference_statistics.json': reference_statistics,
    }
    for filename, content in files.items():
        temporary_path = temporary_dir / f'{filename}.tmp'
        temporary_path.write_text(
            json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
            encoding='utf-8',
        )
        os.replace(temporary_path, temporary_dir / filename)
    digest = hashlib.sha256((temporary_dir / 'bundle.json').read_bytes()).hexdigest()
    (temporary_dir / 'SHA256SUMS.txt').write_text(f'{digest}  bundle.json\n', encoding='ascii')
    try:
        os.rename(temporary_dir, output_dir)
    except FileExistsError as error:
        raise FileExistsError(f'Market package output already exists: {output_dir}') from error
    finally:
        temporary_dir_context.cleanup()
    return output_dir


def publish_staged_market_outputs(staged_dir: Path, history_dir: Path, snapshot_id: str) -> tuple[Path, Path, Path]:
    """Atomically switch a validated staged history, snapshot, and package into place."""
    staged_dir = Path(staged_dir)
    history_dir = Path(history_dir)
    staged_market = staged_dir / 'market_jobs.jsonl'
    staged_snapshot = staged_dir / 'snapshots' / snapshot_id
    staged_package = staged_dir / 'packages' / snapshot_id
    target_market = history_dir / 'market_jobs.jsonl'
    target_snapshot = history_dir / 'snapshots' / snapshot_id
    target_package = history_dir / 'packages' / snapshot_id
    required_staged = (staged_market, staged_snapshot, staged_package)
    if any(not path.exists() for path in required_staged):
        raise FileNotFoundError('Staged market outputs are incomplete; no outputs were published.')
    if target_snapshot.exists() or target_package.exists():
        raise FileExistsError(f'Market snapshot or package already exists for run {snapshot_id}.')

    target_snapshot.parent.mkdir(parents=True, exist_ok=True)
    target_package.parent.mkdir(parents=True, exist_ok=True)
    moved_snapshot = False
    moved_package = False
    try:
        os.rename(staged_snapshot, target_snapshot)
        moved_snapshot = True
        os.rename(staged_package, target_package)
        moved_package = True
        os.replace(staged_market, target_market)
    except OSError:
        if moved_package and target_package.exists():
            os.rename(target_package, staged_package)
        if moved_snapshot and target_snapshot.exists():
            os.rename(target_snapshot, staged_snapshot)
        raise
    return target_market, target_snapshot, target_package


def _normalize_json(value):
    if value is None or type(value).__name__ in {'NAType', 'NaTType'}:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _normalize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if hasattr(value, 'item'):
        return _normalize_json(value.item())
    raise TypeError(f'Cannot serialize market-history value of type {type(value).__name__}.')


def _utc_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError(f'Invalid collection timestamp: {value!r}.') from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime | None) -> str | None:
    return value.isoformat().replace('+00:00', 'Z') if value else None


def _observation(record: dict) -> dict:
    return {
        'collected_at_utc': record.get('collected_at_utc'),
        'role_family': record.get('role_family'),
        'search_level': record.get('search_level'),
        'search_query': record.get('search_query'),
        'page_number': record.get('page_number'),
    }


def _deduplicate_observations(observations: list[dict]) -> list[dict]:
    unique = {}
    for item in observations:
        normalized = _normalize_json(item)
        key = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        unique[key] = normalized
    return sorted(
        unique.values(),
        key=lambda item: (
            item.get('collected_at_utc') or '',
            item.get('role_family') or '',
            item.get('search_level') or '',
            item.get('page_number') or 0,
            item.get('search_query') or '',
        ),
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding='utf-8') as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f'Invalid market-history JSON at {path}:{line_number}.') from error
            if not isinstance(record, dict):
                raise ValueError(f'Expected an object at {path}:{line_number}.')
            records.append(record)
    return records


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + '.tmp')
    with temporary_path.open('w', encoding='utf-8', newline='\n') as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, path)


def _identity(record: dict) -> tuple[str, str]:
    source = record.get('source') or 'jsearch'
    if source in {'job_title', 'job_description', 'unknown'}:
        source = record.get('provider_source') or 'jsearch'
    adapter = SOURCE_ID_ADAPTERS.get(source)
    if adapter is None:
        raise ValueError(f'No source ID adapter is registered for {source!r}.')
    source_job_id = adapter(record)
    if not source_job_id:
        raise ValueError(f'Market record from {source!r} is missing its source job ID.')
    return source, source_job_id


def _merge_record(existing: dict | None, incoming: dict, observations: list[dict]) -> dict:
    source, source_job_id = _identity(incoming)
    prior = existing or {}
    first_values = [
        _utc_datetime(prior.get('first_seen_at')),
        _utc_datetime(prior.get('collected_at_utc')),
    ]
    prior_last = _utc_datetime(prior.get('last_seen_at'))
    last_values = [prior_last]
    incoming_times = [_utc_datetime(incoming.get('collected_at_utc'))]
    for observation in observations:
        observed_at = _utc_datetime(observation.get('collected_at_utc'))
        first_values.append(observed_at)
        last_values.append(observed_at)
        incoming_times.append(observed_at)
    incoming_time = max(
        (value for value in incoming_times if value is not None),
        default=None,
    )

    merged = dict(prior)
    if not existing or prior_last is None or incoming_time is None or incoming_time >= prior_last:
        merged.update(_normalize_json(incoming))
    history = prior.get('collection_history', [])
    if not isinstance(history, list):
        raise ValueError(f'Invalid collection history for {source}:{source_job_id}.')
    merged.update({
        'source': source,
        'source_job_id': source_job_id,
        'collection_history': _deduplicate_observations(history + observations),
    })
    first_seen = min((value for value in first_values if value is not None), default=None)
    last_seen = max((value for value in last_values if value is not None), default=None)
    merged['first_seen_at'] = _timestamp_text(first_seen)
    merged['last_seen_at'] = _timestamp_text(last_seen)
    merged['collected_at_utc'] = prior.get('collected_at_utc') or _timestamp_text(first_seen)
    return merged


def export_market_history(
    jobs: list[dict],
    snapshot_id: str,
    history_dir: Path,
    search_history: list[dict] | None = None,
) -> tuple[Path, Path]:
    """Merge a completed source batch and create a new immutable cumulative snapshot."""
    if not re.fullmatch(r'[A-Za-z0-9_-]+', snapshot_id):
        raise ValueError('Snapshot ID may contain only letters, numbers, underscores, and hyphens.')

    history_dir = Path(history_dir)
    market_path = history_dir / 'market_jobs.jsonl'
    snapshot_dir = history_dir / 'snapshots' / snapshot_id
    if snapshot_dir.exists():
        raise FileExistsError(f'Historical snapshot already exists: {snapshot_dir}')

    merged_records = {}
    for record in _read_jsonl(market_path):
        key = _identity(record)
        if key in merged_records:
            raise ValueError(f'Duplicate source job in persisted history: {key[0]}:{key[1]}.')
        merged_records[key] = record

    observations_by_key = {}
    for record in search_history or []:
        source, source_job_id = _identity(record)
        observations_by_key.setdefault((source, source_job_id), []).append(_observation(record))

    normalized_jobs = [_normalize_json(job) for job in jobs]
    for job in normalized_jobs:
        key = _identity(job)
        row_observations = observations_by_key.pop(key, [])
        if not row_observations:
            row_observations = [_observation(job)]
        else:
            row_observations.append(_observation(job))
        merged_records[key] = _merge_record(merged_records.get(key), job, row_observations)

    if observations_by_key:
        unresolved = sorted(f'{source}:{source_job_id}' for source, source_job_id in observations_by_key)
        raise ValueError(f'Search history references jobs absent from the source batch: {unresolved[:5]}')

    ordered_records = [
        merged_records[key]
        for key in sorted(merged_records)
    ]
    jsonl_content = ''.join(
        json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
        for record in ordered_records
    )
    manifest = {
        'snapshot_id': snapshot_id,
        'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'source': 'jsearch',
        'batch_record_count': len(normalized_jobs),
        'cumulative_job_count': len(ordered_records),
        'query_coverage_is_not_market_growth': True,
    }
    snapshot_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f'.{snapshot_id}-', dir=snapshot_dir.parent) as temp_dir:
        temporary_dir = Path(temp_dir)
        (temporary_dir / 'market_jobs.jsonl').write_text(jsonl_content, encoding='utf-8')
        (temporary_dir / 'manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        os.rename(temporary_dir, snapshot_dir)
    _write_atomic(market_path, jsonl_content)
    return market_path, snapshot_dir

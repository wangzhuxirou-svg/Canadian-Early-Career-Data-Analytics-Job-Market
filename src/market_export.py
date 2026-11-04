import json
import math
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path


def _jsearch_source_job_id(record: dict) -> str | None:
    source_job_id = record.get('source_job_id') or record.get('job_id')
    return str(source_job_id) if source_job_id is not None else None


SOURCE_ID_ADAPTERS = {
    'jsearch': _jsearch_source_job_id,
}


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

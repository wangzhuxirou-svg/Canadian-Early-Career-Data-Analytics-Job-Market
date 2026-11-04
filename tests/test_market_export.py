import json

import pytest

from src.market_export import export_market_history


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


def test_replaying_same_batch_is_idempotent_and_preserves_original_seen_times(tmp_path):
    job = {
        'job_id': 'job-1',
        'job_title': 'Data Analyst',
        'collected_at_utc': '2026-10-01T10:00:00Z',
        'role_family': 'data_analyst',
        'search_level': 'internship',
        'search_query': 'data analyst internship',
        'page_number': 1,
    }

    market_path, first_snapshot = export_market_history(
        [job],
        snapshot_id='first-run',
        history_dir=tmp_path,
        search_history=[job],
    )
    first = read_jsonl(market_path)[0]
    assert first['source'] == 'jsearch'
    assert first['source_job_id'] == 'job-1'
    assert first['first_seen_at'] == '2026-10-01T10:00:00Z'
    assert first['last_seen_at'] == '2026-10-01T10:00:00Z'
    assert len(first['collection_history']) == 1

    _, second_snapshot = export_market_history(
        [job],
        snapshot_id='replay-run',
        history_dir=tmp_path,
        search_history=[job],
    )
    replayed = read_jsonl(market_path)
    manifest = json.loads((second_snapshot / 'manifest.json').read_text(encoding='utf-8'))

    assert len(replayed) == 1
    assert replayed[0]['first_seen_at'] == first['first_seen_at']
    assert replayed[0]['last_seen_at'] == first['last_seen_at']
    assert replayed[0]['collected_at_utc'] == first['collected_at_utc']
    assert replayed[0]['collection_history'] == first['collection_history']
    assert manifest['cumulative_job_count'] == 1
    assert manifest['query_coverage_is_not_market_growth'] is True
    assert (first_snapshot / 'market_jobs.jsonl').exists()


def test_new_observation_updates_last_seen_and_preserves_prior_snapshot(tmp_path):
    first = {
        'source': 'jsearch',
        'source_job_id': 'job-2',
        'job_id': 'job-2',
        'job_title': 'Data Analyst',
        'collected_at_utc': '2026-10-01T10:00:00Z',
        'role_family': 'data_analyst',
        'search_level': 'internship',
        'search_query': 'data analyst internship',
        'page_number': 1,
    }
    newer = {
        **first,
        'job_title': 'Senior Data Analyst',
        'collected_at_utc': '2026-10-08T10:00:00Z',
        'role_family': 'business_analyst',
        'search_level': 'entry_level',
        'search_query': 'business analyst entry level',
    }

    _, first_snapshot = export_market_history(
        [first], 'snapshot-1', tmp_path, search_history=[first],
    )
    first_snapshot_bytes = (first_snapshot / 'market_jobs.jsonl').read_bytes()
    market_path, second_snapshot = export_market_history(
        [newer], 'snapshot-2', tmp_path, search_history=[newer],
    )
    current = read_jsonl(market_path)[0]

    assert len(read_jsonl(market_path)) == 1
    assert current['job_title'] == 'Senior Data Analyst'
    assert current['first_seen_at'] == '2026-10-01T10:00:00Z'
    assert current['last_seen_at'] == '2026-10-08T10:00:00Z'
    assert current['collected_at_utc'] == '2026-10-01T10:00:00Z'
    assert len(current['collection_history']) == 2
    assert (first_snapshot / 'market_jobs.jsonl').read_bytes() == first_snapshot_bytes
    assert (second_snapshot / 'market_jobs.jsonl').exists()

    third_path, _ = export_market_history(
        [first], 'old-replay', tmp_path, search_history=[first],
    )
    after_old_replay = read_jsonl(third_path)[0]
    assert after_old_replay['job_title'] == 'Senior Data Analyst'
    assert after_old_replay['last_seen_at'] == '2026-10-08T10:00:00Z'


def test_snapshot_is_immutable_and_missing_source_ids_fail(tmp_path):
    job = {'job_id': 'job-3'}
    _, snapshot = export_market_history([job], 'immutable-run', tmp_path)
    original = (snapshot / 'market_jobs.jsonl').read_bytes()

    with pytest.raises(FileExistsError, match='already exists'):
        export_market_history([job], 'immutable-run', tmp_path)
    assert (snapshot / 'market_jobs.jsonl').read_bytes() == original

    with pytest.raises(ValueError, match='missing its source job ID'):
        export_market_history([{'job_title': 'Missing ID'}], 'missing-id', tmp_path)


def test_unknown_source_requires_an_explicit_id_adapter(tmp_path):
    with pytest.raises(ValueError, match='No source ID adapter'):
        export_market_history(
            [{'source': 'future_source', 'source_job_id': 'id-1'}],
            'future-source',
            tmp_path,
        )

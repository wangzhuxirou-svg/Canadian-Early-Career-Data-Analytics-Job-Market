import json

import pandas as pd
import pytest
from src import pipeline
from src.extract import CollectionRunIncomplete


def test_run_pipeline_can_skip_saving_supplied_raw_batch(tmp_path, monkeypatch):
    empty = pd.DataFrame()
    features = pd.DataFrame([{
        'job_id': 'job-1',
        'source': 'jsearch',
        'job_title': 'Data Analyst Intern',
        'employer_name': 'Example Employer',
        'job_apply_link': 'https://jobs.example/job-1',
        'classified_role_family': 'data_analyst',
        'classified_role_subtype': None,
        'role_status': 'target',
        'classified_career_level': 'internship',
        'career_level_status': 'classified',
        'market_area': 'Toronto GTA',
        'job_country': 'Canada',
        'job_posted_at_datetime_utc': '2026-10-10T12:00:00Z',
        'collected_at_utc': '2026-10-11T12:00:00Z',
        'first_seen_at': '2026-10-11T12:00:00Z',
        'last_seen_at': '2026-10-11T12:00:00Z',
        'job_description': 'A' * 600,
        'technical_skills': ['python'],
        'soft_skills': [],
        'is_senior': False,
        'is_old_posting': False,
        'is_future_posting': False,
        'has_invalid_posting_date': False,
        'location_status': 'valid',
    }])

    monkeypatch.setattr(pipeline, '_configure_logging', lambda: None)
    monkeypatch.setattr(pipeline, 'PROCESSED_DATA_DIR', tmp_path / 'data' / 'processed')
    monkeypatch.setattr(pipeline, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(pipeline, 'clean_jobs', lambda df: (df, empty, empty, empty, empty))
    monkeypatch.setattr(pipeline, 'combine_issues', lambda *issues: empty)
    monkeypatch.setattr(pipeline, 'engineer_features', lambda df: features)
    monkeypatch.setattr(pipeline, 'build_review_queue', lambda df: empty)
    monkeypatch.setattr(pipeline, 'calculate_feature_coverage', lambda df: empty)
    monkeypatch.setattr(pipeline, '_select_analysis_jobs', lambda df: df)

    def unexpected_raw_save(jobs):
        raise AssertionError('Offline processing must not save another raw batch.')

    monkeypatch.setattr(pipeline, 'save_raw_jobs', unexpected_raw_save)

    outputs = pipeline.run_pipeline([{'job_id': 'job-1'}], save_raw_batch=False)

    assert 'raw_jobs' not in outputs
    assert outputs['analysis_jobs'].exists()
    assert outputs['market_history'].exists()
    assert outputs['market_snapshot'].is_dir()
    assert (outputs['market_package'] / 'bundle.json').exists()
    assert outputs['run_archive'].is_dir()
    assert (outputs['run_archive'] / 'processed' / 'analysis_jobs.jsonl').exists()
    assert (outputs['run_archive'] / 'issues' / 'data_issues.jsonl').exists()
    assert not (tmp_path / 'data' / 'raw').exists()

    second_outputs = pipeline.run_pipeline([{'job_id': 'job-1'}], save_raw_batch=False)

    assert second_outputs['run_archive'] != outputs['run_archive']
    assert len(second_outputs['market_history'].read_text(encoding='utf-8').splitlines()) == 1
    assert second_outputs['market_snapshot'] != outputs['market_snapshot']
    assert second_outputs['market_package'] != outputs['market_package']
    assert outputs['run_archive'].exists()
    assert second_outputs['run_archive'].exists()


def test_incomplete_collection_does_not_replace_formal_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, '_configure_logging', lambda: None)
    monkeypatch.setattr(pipeline, 'PROCESSED_DATA_DIR', tmp_path / 'data' / 'processed')
    monkeypatch.setattr(pipeline, 'PROJECT_ROOT', tmp_path)

    def incomplete_collection(*args, **kwargs):
        raise CollectionRunIncomplete('partial-run', 'data_analyst:internship', 'Timeout')

    monkeypatch.setattr(pipeline, 'collect_all_jobs', incomplete_collection)

    with pytest.raises(CollectionRunIncomplete, match='No processed dataset was published'):
        pipeline.run_pipeline()

    assert not (tmp_path / 'data' / 'processed').exists()
    assert not (tmp_path / 'data' / 'runs').exists()


def test_legacy_clean_history_rebuilds_features_from_raw_archives(tmp_path, monkeypatch):
    history_path = tmp_path / 'data' / 'history' / 'market_jobs.jsonl'
    history_path.parent.mkdir(parents=True)
    history_path.write_text(json.dumps({
        'source': 'jsearch',
        'source_job_id': 'legacy-job',
        'job_id': 'legacy-job',
        'job_title': 'Data Analyst Intern',
        'collected_at_utc': '2026-10-11T12:00:00Z',
    }) + '\n', encoding='utf-8')
    raw_dir = tmp_path / 'data' / 'raw'
    raw_dir.mkdir(parents=True)
    raw_job = {
        'job_id': 'legacy-job',
        'job_title': 'Data Analyst Intern',
        'employer_name': 'Example Employer',
        'job_state': 'Ontario',
        'job_country': 'Canada',
        'job_city': 'Toronto',
        'job_employment_type': 'Internship',
        'job_posted_at_datetime_utc': '2026-10-10T12:00:00Z',
        'job_description': 'Internship role. ' + 'Uses Python and SQL. ' * 60,
        'job_apply_link': 'https://jobs.example/legacy-job',
        'source': 'jsearch',
        'source_job_id': 'legacy-job',
        'role_family': 'data_analyst',
        'role_query': 'data analyst',
        'search_level': 'internship',
        'search_query': 'data analyst internship',
        'page_number': 1,
        'collected_at_utc': '2026-10-11T12:00:00Z',
    }
    (raw_dir / 'jobs_legacy.jsonl').write_text(json.dumps(raw_job) + '\n', encoding='utf-8')
    monkeypatch.setattr(pipeline, 'PROJECT_ROOT', tmp_path)

    rebuilt = pipeline._rebuild_legacy_market_rows(history_path, [raw_job])

    assert rebuilt is not None
    rebuilt_jobs, _ = rebuilt
    assert len(rebuilt_jobs) == 1
    assert rebuilt_jobs[0]['classified_role_family'] == 'data_analyst'
    assert rebuilt_jobs[0]['classified_career_level'] == 'internship'
    assert rebuilt_jobs[0]['technical_skills'] == ['python', 'sql']
    assert rebuilt_jobs[0]['source'] == 'jsearch'


def test_legacy_history_without_raw_archives_fails_before_rebuild(tmp_path, monkeypatch):
    history_path = tmp_path / 'data' / 'history' / 'market_jobs.jsonl'
    history_path.parent.mkdir(parents=True)
    history_path.write_text(json.dumps({
        'source': 'jsearch',
        'source_job_id': 'unrecoverable-job',
        'job_id': 'unrecoverable-job',
    }) + '\n', encoding='utf-8')
    monkeypatch.setattr(pipeline, 'PROJECT_ROOT', tmp_path)

    with pytest.raises(RuntimeError, match='no archived raw batches'):
        pipeline._rebuild_legacy_market_rows(history_path, [])


def test_package_validation_failure_does_not_publish_history_or_replace_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, '_configure_logging', lambda: None)
    monkeypatch.setattr(pipeline, 'PROCESSED_DATA_DIR', tmp_path / 'data' / 'processed')
    monkeypatch.setattr(pipeline, 'PROJECT_ROOT', tmp_path)
    history_dir = tmp_path / 'data' / 'history'
    history_dir.mkdir(parents=True)
    prior_history = {
        'source': 'jsearch',
        'source_job_id': 'prior-job',
        'job_id': 'prior-job',
        'job_title': 'Data Analyst Intern',
        'employer_name': 'Prior Employer',
        'job_country': 'Canada',
        'job_city': 'Toronto',
        'job_province_clean': 'ON',
        'job_city_clean': 'Toronto',
        'location_status': 'valid',
        'job_employment_type': 'Internship',
        'job_posted_at_datetime_utc': '2026-10-10T12:00:00Z',
        'collected_at_utc': '2026-10-11T12:00:00Z',
        'first_seen_at': '2026-10-11T12:00:00Z',
        'last_seen_at': '2026-10-11T12:00:00Z',
        'job_description': 'Internship role. ' + 'Uses Python and SQL. ' * 60,
        'job_apply_link': 'https://jobs.example/prior-job',
        'market_area': 'Toronto GTA',
        'classified_role_family': 'data_analyst',
        'classified_role_subtype': None,
        'classified_career_level': 'internship',
        'role_status': 'target',
        'career_level_status': 'classified',
        'technical_skills': ['python', 'sql'],
        'soft_skills': [],
        'is_senior': False,
        'is_old_posting': False,
        'is_future_posting': False,
        'has_invalid_posting_date': False,
    }
    history_path = history_dir / 'market_jobs.jsonl'
    original_history = (json.dumps(prior_history) + '\n').encode('utf-8')
    history_path.write_bytes(original_history)
    formal_analysis = tmp_path / 'data' / 'processed' / 'analysis_jobs.jsonl'
    formal_analysis.parent.mkdir(parents=True)
    formal_analysis.write_text('previous formal output\n', encoding='utf-8')

    def reject_package(*args, **kwargs):
        raise ValueError('contract validation failed')

    monkeypatch.setattr(pipeline, 'export_market_bundle', reject_package)
    raw_job = {
        'job_id': 'staged-job',
        'job_title': 'Data Analyst Intern',
        'employer_name': 'Example Employer',
        'job_state': 'Ontario',
        'job_country': 'Canada',
        'job_city': 'Toronto',
        'job_employment_type': 'Internship',
        'job_posted_at_datetime_utc': '2026-10-10T12:00:00Z',
        'job_description': 'Internship role. ' + 'Uses Python and SQL. ' * 60,
        'job_apply_link': 'https://jobs.example/staged-job',
        'source': 'jsearch',
        'source_job_id': 'staged-job',
        'role_family': 'data_analyst',
        'role_query': 'data analyst',
        'search_level': 'internship',
        'search_query': 'data analyst internship',
        'page_number': 1,
        'collected_at_utc': '2026-10-11T12:00:00Z',
    }

    with pytest.raises(ValueError, match='contract validation failed'):
        pipeline.run_pipeline([raw_job], save_raw_batch=False)

    assert history_path.read_bytes() == original_history
    assert not (history_dir / 'snapshots').exists()
    assert not (history_dir / 'packages').exists()
    assert formal_analysis.read_text(encoding='utf-8') == 'previous formal output\n'

import pandas as pd
import pytest
from src import pipeline
from src.extract import CollectionRunIncomplete


def test_run_pipeline_can_skip_saving_supplied_raw_batch(tmp_path, monkeypatch):
    empty = pd.DataFrame()
    features = pd.DataFrame({'job_id': ['job-1']})

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
    assert outputs['run_archive'].is_dir()
    assert (outputs['run_archive'] / 'processed' / 'analysis_jobs.jsonl').exists()
    assert (outputs['run_archive'] / 'issues' / 'data_issues.jsonl').exists()
    assert not (tmp_path / 'data' / 'raw').exists()

    second_outputs = pipeline.run_pipeline([{'job_id': 'job-1'}], save_raw_batch=False)

    assert second_outputs['run_archive'] != outputs['run_archive']
    assert len(second_outputs['market_history'].read_text(encoding='utf-8').splitlines()) == 1
    assert second_outputs['market_snapshot'] != outputs['market_snapshot']
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

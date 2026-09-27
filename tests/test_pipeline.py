import pandas as pd
from src import pipeline


def test_run_pipeline_can_skip_saving_supplied_raw_batch(tmp_path, monkeypatch):
    empty = pd.DataFrame()
    features = pd.DataFrame({"job_id": ["job-1"]})

    monkeypatch.setattr(pipeline, "_configure_logging", lambda: None)
    monkeypatch.setattr(pipeline, "PROCESSED_DATA_DIR", tmp_path / "data" / "processed")
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "clean_jobs", lambda df: (df, empty, empty, empty, empty))
    monkeypatch.setattr(pipeline, "combine_issues", lambda *issues: empty)
    monkeypatch.setattr(pipeline, "engineer_features", lambda df: features)
    monkeypatch.setattr(pipeline, "build_review_queue", lambda df: empty)
    monkeypatch.setattr(pipeline, "calculate_feature_coverage", lambda df: empty)
    monkeypatch.setattr(pipeline, "_select_analysis_jobs", lambda df: df)

    def unexpected_raw_save(jobs):
        raise AssertionError("Offline processing must not save another raw batch.")

    monkeypatch.setattr(pipeline, "save_raw_jobs", unexpected_raw_save)

    outputs = pipeline.run_pipeline([{"job_id": "job-1"}], save_raw_batch=False)

    assert "raw_jobs" not in outputs
    assert outputs["analysis_jobs"].exists()
    assert not (tmp_path / "data" / "raw").exists()

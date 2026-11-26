import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from config.settings import MAX_PAGES, SEARCH_QUERIES, START_PAGE
from src.extract import collect_all_jobs
from src.formal_collection import export_completed_round
from src.history import CollectionHistory


def test_formal_collection_exports_coverage_and_deduplicated_sample(tmp_path):
    history = CollectionHistory(tmp_path)
    posted = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    job = {
        "job_id": "fixture-id",
        "job_title": "Data Analyst Intern",
        "job_employment_type": "Internship",
        "job_description": "Python and SQL internship. " * 10,
        "job_location": "Toronto, ON, Canada",
        "job_city": "Toronto",
        "job_state": "ON",
        "job_country": "CA",
        "job_posted_at_datetime_utc": posted,
    }
    failed_once = False

    def fetch(task, _page):
        nonlocal failed_once
        if task["role_family"] != "data_analyst":
            return []
        if not failed_once:
            failed_once = True
            raise requests.Timeout("fixture")
        return [job]

    collect_all_jobs(
        SEARCH_QUERIES, START_PAGE, MAX_PAGES, 0, history=history, fetch_page=fetch,
        sleep=lambda _: None, run_id="fixture-run",
    )
    restored = CollectionHistory(tmp_path)
    restored.restore_completed_run("fixture-run")
    output = export_completed_round(restored, "fixture-usage")
    coverage = json.loads((output / "market-coverage.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "market" / "manifest.json").read_text(encoding="utf-8"))
    samples = json.loads((output / "role-review-sample.json").read_text(encoding="utf-8"))
    assert (len(coverage["queries"]), coverage["raw_rows"], coverage["unique_source_jobs"]) == (24, 3, 1)
    assert sum(query["attempts"] for query in coverage["queries"]) == 25
    assert sum(query["result_count"] == 0 for query in coverage["queries"]) == 21
    assert coverage["collected_from"] == manifest["collected_from"]
    assert coverage["collected_to"] == manifest["collected_to"]
    assert manifest["run_status"] == "complete"
    assert manifest["sample_count"] == 1
    assert samples["data_analyst"][0]["decision"] is None
    with pytest.raises(FileExistsError):
        export_completed_round(restored, "fixture-usage")

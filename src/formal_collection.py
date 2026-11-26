"""Collect one approved JSearch round or export a completed checkpoint offline."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

from config.settings import MAX_PAGES, REQUEST_DELAY_SECONDS, SEARCH_QUERIES, START_PAGE
from src.clean import clean_jobs
from src.extract import CollectionRunIncomplete, collect_all_jobs
from src.feature_engineering import engineer_features
from src.history import CollectionHistory
from src.market_export import export_market_bundle
from src.pipeline import _select_analysis_jobs


QUERY_CONFIG_VERSION = "jsearch-role-career-matrix-v1"


def export_completed_round(history: CollectionHistory, usage_ref: str) -> Path:
    """Write a release input and review sample beside the private request ledger."""
    checkpoint = history.checkpoint
    expected = {history.query_key(task): task for task in SEARCH_QUERIES}
    expected_pages = [f"{key}:{page}" for key in expected for page in range(START_PAGE, MAX_PAGES + 1)]
    if checkpoint["status"] != "complete" or checkpoint["query_keys"] != expected_pages:
        raise ValueError("Release input requires a completed round of the configured query matrix.")
    queries = []
    for key, task in expected.items():
        spec = checkpoint["query_specs"][key]
        query = checkpoint["queries"][key]
        if spec["query"] != task["query"] or query["status"] != "complete":
            raise ValueError("The saved checkpoint does not match the configured query matrix.")
        count = query["record_count"]
        queries.append({
            "role_id": task["role_family"],
            "career_level": task["search_level"],
            "status": "verified_publishable" if count else "checked_no_valid_results",
            "attempts": query["attempt_count"],
            "result_count": count,
        })

    jobs = history.load_run_jobs()
    if len(jobs) != sum(row["result_count"] for row in queries):
        raise ValueError("Stored JSearch responses do not match checkpoint counts.")
    job_ids = {str(job["job_id"]) for job in jobs if job.get("job_id")}
    analysis_rows = []
    if jobs:
        cleaned = clean_jobs(pd.DataFrame(jobs))[0]
        if not cleaned.empty:
            analysis = _select_analysis_jobs(engineer_features(cleaned))
            analysis_rows = json.loads(analysis.to_json(orient="records", date_format="iso"))
    collection_window = (checkpoint["started_at"], checkpoint["completed_at"])
    report = {
        "run_id": checkpoint["run_id"],
        "query_config_version": QUERY_CONFIG_VERSION,
        "collected_from": collection_window[0],
        "collected_to": collection_window[1],
        "usage_ref": usage_ref,
        "raw_rows": len(jobs),
        "unique_source_jobs": len(job_ids),
        "queries": queries,
    }

    output = history.history_dir / "formal-runs" / checkpoint["run_id"]
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError("This completed round already has an immutable release input.")
    with tempfile.TemporaryDirectory(prefix=".round-", dir=output.parent) as scratch:
        stage = Path(scratch) / "round"
        export_market_bundle(
            analysis_rows, stage / "market",
            dataset_version=f"1.0.{checkpoint['completed_at'].translate(str.maketrans('', '', '-:TZ.'))}",
            query_config_version=QUERY_CONFIG_VERSION,
            run_status="complete",
            collection_window=collection_window,
        )
        (stage / "market-coverage.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        review_sample = {}
        for role in dict.fromkeys(task["role_family"] for task in SEARCH_QUERIES):
            candidates = sorted(
                (job for job in analysis_rows if job["classified_role_family"] == role),
                key=lambda job: str(job["job_id"]),
            )
            review_sample[role] = [
                {
                    "job_id": str(job["job_id"]),
                    "title": job["job_title"],
                    "career_level_candidate": job["classified_career_level"],
                    "decision": None,
                    "basis": None,
                }
                for job in candidates[:10]
            ]
        (stage / "role-review-sample.json").write_text(
            json.dumps(review_sample, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        stage.rename(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--collect", action="store_true", help="Make authorized requests within the ledger budget.")
    mode.add_argument("--from-completed-run", action="store_true", help="Reuse saved responses without network calls.")
    parser.add_argument("--run-id", help="Resume or export this checkpoint from the same ledger.")
    parser.add_argument("--usage-ref", required=True, help="Reference to the source's actual authorized usage scope.")
    args = parser.parse_args()
    if not args.usage_ref.strip():
        parser.error("--usage-ref must identify the approved source-usage record.")
    history = CollectionHistory()
    try:
        if args.collect:
            collect_all_jobs(
                SEARCH_QUERIES, START_PAGE, MAX_PAGES, REQUEST_DELAY_SECONDS,
                history=history, run_id=args.run_id,
            )
        else:
            if not args.run_id:
                parser.error("--from-completed-run requires --run-id.")
            history.restore_completed_run(args.run_id)
        print(export_completed_round(history, args.usage_ref))
        return 0
    except (CollectionRunIncomplete, ValueError, OSError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

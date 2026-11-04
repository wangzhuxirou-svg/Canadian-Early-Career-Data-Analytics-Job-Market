import logging
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import pandas as pd
from config.settings import (
    CORE_CAREER_LEVELS,
    CORE_ROLE_FAMILIES,
    MAX_PAGES,
    PROJECT_ROOT,
    REQUEST_DELAY_SECONDS,
    SEARCH_QUERIES,
    START_PAGE,
)
from src.clean import clean_jobs, combine_issues
from src.extract import collect_all_jobs, save_raw_jobs
from src.feature_engineering import engineer_features
from src.market_export import (
    export_market_bundle,
    export_market_history,
    publish_staged_market_outputs,
)
from src.review import build_review_queue
from src.validate import calculate_feature_coverage


PROCESSED_DATA_DIR = PROJECT_ROOT / 'data' / 'processed'
LOG_FILE = PROJECT_ROOT / 'outputs' / 'logs' / 'pipeline.log'
LOGGER = logging.getLogger(__name__)


def _configure_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler(),
        ],
    )


def _select_analysis_jobs(df: pd.DataFrame) -> pd.DataFrame:
    '''Keep only core role families and career levels for formal analysis.'''
    country = df['job_country'].astype('string').str.strip().str.upper()
    eligible = (
        df['classified_role_family'].isin(CORE_ROLE_FAMILIES)
        & df['classified_career_level'].isin(CORE_CAREER_LEVELS)
        & ~df['is_senior'].fillna(False)
        & ~df['is_old_posting'].fillna(False)
        & ~df['is_future_posting'].fillna(False)
        & ~df['has_invalid_posting_date'].fillna(False)
        & country.isin(['CA', 'CAN', 'CANADA'])
        & df['location_status'].ne('invalid')
    ).fillna(False)
    return df.loc[eligible].copy()


def _save_jsonl(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_json(path, orient='records', lines=True, force_ascii=False, date_format='iso')
    LOGGER.info('Saved %s rows to %s', len(df), path)


def _create_run_archive_dir() -> Path:
    runs_dir = PROJECT_ROOT / 'data' / 'runs'
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    run_dir = runs_dir / timestamp
    suffix = 1
    while True:
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_dir
        except FileExistsError:
            run_dir = runs_dir / f"{timestamp}_{suffix}"
            suffix += 1


def _read_archived_raw_jobs(raw_dir: Path) -> list[dict]:
    records = []
    for path in sorted(raw_dir.glob('jobs_*.jsonl')):
        with path.open(encoding='utf-8') as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(f'Invalid archived raw JSON at {path}:{line_number}.') from error
                if not isinstance(record, dict):
                    raise ValueError(f'Expected an object at {path}:{line_number}.')
                records.append(record)
    return records


def _rebuild_legacy_market_rows(
    history_path: Path,
    incoming_jobs: list[dict],
) -> tuple[list[dict], list[dict]] | None:
    if not history_path.exists():
        return None
    existing_rows = [
        json.loads(line)
        for line in history_path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]
    required_features = {
        'classified_role_family',
        'classified_role_subtype',
        'classified_career_level',
        'technical_skills',
        'soft_skills',
    }
    if not existing_rows or all(required_features <= row.keys() for row in existing_rows):
        return None

    raw_archives = _read_archived_raw_jobs(PROJECT_ROOT / 'data' / 'raw')
    if not raw_archives:
        raise RuntimeError(
            'Legacy cumulative market history lacks classification/features, and no archived raw batches '
            'are available to rebuild it. Restore data/raw/jobs_*.jsonl before retrying.'
        )

    raw_frame = pd.DataFrame(raw_archives + incoming_jobs)
    cleaned, _, search_history, _, _ = clean_jobs(raw_frame)
    features = engineer_features(cleaned)
    rebuilt_ids = set(features['job_id'].astype(str))
    legacy_ids = {
        str(row.get('source_job_id') or row.get('job_id'))
        for row in existing_rows
        if row.get('source_job_id') or row.get('job_id')
    }
    missing_ids = legacy_ids - rebuilt_ids
    if missing_ids:
        raise RuntimeError(
            'Legacy cumulative history contains jobs missing from archived raw batches; '
            f'cannot safely rebuild: {sorted(missing_ids)[:5]}.'
        )
    valid_ids = set(cleaned['job_id'].astype(str))
    market_rows = json.loads(features.to_json(orient='records', date_format='iso'))
    search_rows = json.loads(search_history.to_json(orient='records', date_format='iso'))
    search_rows = [row for row in search_rows if str(row.get('job_id')) in valid_ids]
    LOGGER.info(
        'Rebuilt legacy cumulative history from %s archived raw rows into %s featured jobs',
        len(raw_archives),
        len(market_rows),
    )
    return market_rows, search_rows


def run_pipeline(
    jobs: list[dict] | None = None,
    save_raw_batch: bool = True,
    collection_run_id: str | None = None,
) -> dict[str, Path]:
    '''Run all pipeline steps and return paths to the generated JSONL files.
    When jobs is omitted, postings are fetched from the configured job API.
    Passing jobs directly is useful for reruns and offline processing.
    Set save_raw_batch=False to process supplied historical jobs without
    adding another raw collection batch. Each run is archived under data/runs;
    returned output paths continue to point to the latest files.
    '''
    _configure_logging()
    LOGGER.info('Pipeline started')
    if jobs is None:
        LOGGER.info('Collecting jobs from the API')
        jobs = collect_all_jobs(
            SEARCH_QUERIES,
            START_PAGE,
            MAX_PAGES,
            REQUEST_DELAY_SECONDS,
            run_id=collection_run_id,
        )
    else:
        LOGGER.info('Running pipeline on %s supplied raw rows', len(jobs))

    if not jobs:
        raise ValueError('No jobs were collected; pipeline cannot continue.')

    raw_path = None
    if save_raw_batch:
        raw_path = save_raw_jobs(jobs)
        LOGGER.info('Saved %s raw rows to %s', len(jobs), raw_path)
    else:
        LOGGER.info('Skipping raw batch save for supplied jobs')

    df_raw = pd.DataFrame(jobs)
    LOGGER.info('Cleaning %s raw rows', len(df_raw))
    (df_clean,
     id_issues,
     search_history,
     employment_type_issues,
     location_issues,
     ) = clean_jobs(df_raw)
    LOGGER.info(
        'Cleaning complete: %s retained, %s missing IDs, %s invalid employment types, '
        '%s invalid locations',
        len(df_clean),
        len(id_issues),
        len(employment_type_issues),
        len(location_issues),
    )
    data_issues = combine_issues(
        id_issues,
        employment_type_issues,
        location_issues,
    )
    LOGGER.info('Combined %s cleaning issues', len(data_issues))

    LOGGER.info('Engineering features for %s cleaned rows', len(df_clean))
    df_features = engineer_features(df_clean)
    review_queue = build_review_queue(df_features)
    coverage = calculate_feature_coverage(df_features)
    LOGGER.info('Feature coverage:\n%s', coverage.to_string(index=False))
    LOGGER.info('Review queue contains %s rows', len(review_queue))

    analysis_df = _select_analysis_jobs(df_features)
    LOGGER.info(
        'Analysis filter retained %s of %s feature rows',
        len(analysis_df),
        len(df_features),
    )

    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    issues_dir = PROJECT_ROOT / 'data' / 'issues'
    issues_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        'cleaned_jobs': PROCESSED_DATA_DIR / 'cleaned_jobs.jsonl',
        'featured_jobs': PROCESSED_DATA_DIR / 'featured_jobs.jsonl',
        'analysis_jobs': PROCESSED_DATA_DIR / 'analysis_jobs.jsonl',
        'review_queue': PROCESSED_DATA_DIR / 'review_queue.jsonl',
        'search_history': PROCESSED_DATA_DIR / 'search_history.jsonl',
        'data_issues': issues_dir / 'data_issues.jsonl',
    }
    run_archive_dir = _create_run_archive_dir()
    archive_outputs = {
        'cleaned_jobs': run_archive_dir / 'processed' / 'cleaned_jobs.jsonl',
        'featured_jobs': run_archive_dir / 'processed' / 'featured_jobs.jsonl',
        'analysis_jobs': run_archive_dir / 'processed' / 'analysis_jobs.jsonl',
        'review_queue': run_archive_dir / 'processed' / 'review_queue.jsonl',
        'search_history': run_archive_dir / 'processed' / 'search_history.jsonl',
        'data_issues': run_archive_dir / 'issues' / 'data_issues.jsonl',
    }
    outputs['run_archive'] = run_archive_dir
    if raw_path is not None:
        outputs['raw_jobs'] = raw_path
    valid_job_ids = set(df_clean['job_id'].astype(str))
    market_rows = json.loads(df_features.to_json(orient='records', date_format='iso'))
    search_rows = json.loads(search_history.to_json(orient='records', date_format='iso'))
    search_rows = [row for row in search_rows if str(row.get('job_id')) in valid_job_ids]
    history_dir = PROJECT_ROOT / 'data' / 'history'
    history_path = history_dir / 'market_jobs.jsonl'
    rebuilt = _rebuild_legacy_market_rows(history_path, jobs)
    if rebuilt is not None:
        market_rows, search_rows = rebuilt
    history_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.market-stage-', dir=history_dir) as stage_dir:
        staged_dir = Path(stage_dir)
        if history_path.exists():
            shutil.copy2(history_path, staged_dir / 'market_jobs.jsonl')
        staged_history, _, = export_market_history(
            market_rows,
            snapshot_id=run_archive_dir.name,
            history_dir=staged_dir,
            search_history=search_rows,
        )
        cumulative_market_rows = [
            json.loads(line)
            for line in staged_history.read_text(encoding='utf-8').splitlines()
            if line.strip()
        ]
        cumulative_analysis_rows = _select_analysis_jobs(
            pd.DataFrame(cumulative_market_rows)
        ).to_dict(orient='records')
        export_market_bundle(
            cumulative_analysis_rows,
            staged_dir / 'packages' / run_archive_dir.name,
            dataset_version=f'1.0.{datetime.now(timezone.utc):%Y%m%d%H%M%S}',
            query_config_version='jsearch-role-career-matrix-v1',
            run_status='partial',
        )
        market_path, market_snapshot, market_package = publish_staged_market_outputs(
            staged_dir,
            history_dir,
            run_archive_dir.name,
        )
    outputs['market_history'] = market_path
    outputs['market_snapshot'] = market_snapshot
    outputs['market_package'] = market_package
    dataframes = {
        'cleaned_jobs': df_clean,
        'featured_jobs': df_features,
        'analysis_jobs': analysis_df,
        'review_queue': review_queue,
        'search_history': search_history,
        'data_issues': data_issues,
    }
    for name, dataframe in dataframes.items():
        _save_jsonl(dataframe, outputs[name])
        _save_jsonl(dataframe, archive_outputs[name])

    LOGGER.info('Pipeline completed successfully')
    return outputs


if __name__ == '__main__':
    run_pipeline()
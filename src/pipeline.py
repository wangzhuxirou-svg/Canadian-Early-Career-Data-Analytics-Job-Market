import logging
from pathlib import Path
from datetime import datetime, timezone
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


def run_pipeline(jobs: list[dict] | None = None, save_raw_batch: bool = True,) -> dict[str, Path]:
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
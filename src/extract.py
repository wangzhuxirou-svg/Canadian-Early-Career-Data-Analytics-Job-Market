import os
import requests
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from pathlib import Path
load_dotenv()

from config.settings import RAW_DATA_DIR, REQUEST_DELAY_SECONDS, MAX_PAGES
from config.settings import API_FIELDS, SEARCH_QUERIES, COUNTRY, DATE_POST, START_PAGE
from src.history import CollectionHistory, RequestBudgetExceeded

API_KEY = os.getenv('JSEARCH_API_KEY')
API_URL = 'https://api.openwebninja.com/jsearch/search-v2'
CONNECT_TIMEOUT_SECONDS = float(os.getenv('JSEARCH_CONNECT_TIMEOUT_SECONDS', '5'))
READ_TIMEOUT_SECONDS = float(os.getenv('JSEARCH_READ_TIMEOUT_SECONDS', '30'))
MAX_RETRIES = int(os.getenv('JSEARCH_MAX_RETRIES', '2'))
BACKOFF_BASE_SECONDS = float(os.getenv('JSEARCH_BACKOFF_BASE_SECONDS', '1'))
MAX_BACKOFF_SECONDS = float(os.getenv('JSEARCH_MAX_BACKOFF_SECONDS', '30'))
if CONNECT_TIMEOUT_SECONDS <= 0 or READ_TIMEOUT_SECONDS <= 0:
    raise ValueError('JSearch connection and read timeouts must be positive.')
if MAX_RETRIES < 0 or BACKOFF_BASE_SECONDS < 0 or MAX_BACKOFF_SECONDS < 0:
    raise ValueError('JSearch retry and backoff settings must be non-negative.')


class CollectionRunIncomplete(RuntimeError):
    def __init__(self, run_id: str, query_key: str, reason: str):
        super().__init__(
            f'Collection run {run_id} is incomplete at {query_key}: {reason}. '
            'No processed dataset was published; retry to resume the checkpoint.'
        )
        self.run_id = run_id
        self.query_key = query_key


def add_track_fields(jobs: list[dict], search_task: dict, page_number: int) -> list[dict]:
    '''Reformat the information from jobs and add track fields to API_FIELDS with search_tasks and page_number.'''
    collected_at = datetime.now(timezone.utc).isoformat()
    tracked_jobs = []
    for job in jobs:
        record = {field: clean_invalid_unicode(job.get(field)) for field in API_FIELDS}
        record.update({
            'source': 'jsearch',
            'source_job_id': str(job['job_id']) if job.get('job_id') is not None else None,
            'role_family': search_task['role_family'],
            'role_query': search_task['role_query'],
            'search_level': search_task['search_level'],
            'search_query': search_task['query'],
            'page_number': page_number,
            'collected_at_utc': collected_at,
        })
        tracked_jobs.append(record)
    return tracked_jobs

def fetch_jobs_page(search_task: dict, page_number: int) -> list[dict]:
    '''Catch 1 page of jobs use page_number with search_task.'''
    if not API_KEY:
        raise ValueError('API key is not found.')
    headers = {
        'X-Api-Key': API_KEY,
    }
    params = {
        'query': search_task['query'],
        'page': page_number,
        'number_pages': 1,
        'country': COUNTRY,
        'date_posted': DATE_POST,
    }
    response = requests.get(
        API_URL,
        headers=headers,
        params=params,
        timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
    )
    response.raise_for_status()
    response_data = response.json()
    data = response_data.get('data', [])
    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        jobs = data.get('jobs', [])
    else:
        raise TypeError(f'Unexpected API type: {type(data)}')
    return jobs


def _response_status(error: Exception) -> int | None:
    response = getattr(error, 'response', None)
    return getattr(response, 'status_code', None)


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return True
    status = _response_status(error)
    return status == 429 or (status is not None and 500 <= status <= 599)


def _retry_delay(
    error: Exception,
    attempt: int,
    backoff_base_seconds: float,
) -> float:
    response = getattr(error, 'response', None)
    retry_after = (getattr(response, 'headers', None) or {}).get('Retry-After')
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                delay = backoff_base_seconds * (2 ** (attempt - 1))
    else:
        delay = backoff_base_seconds * (2 ** (attempt - 1))
    return min(MAX_BACKOFF_SECONDS, max(0.0, delay))


def collect_all_jobs(
    search_task: list[dict],
    start_page: int,
    max_page: int,
    request_delay: float,
    *,
    history: CollectionHistory | None = None,
    fetch_page=None,
    max_retries: int = MAX_RETRIES,
    backoff_base_seconds: float = BACKOFF_BASE_SECONDS,
    sleep=None,
    run_id: str | None = None,
) -> list[dict]:
    '''Collect each query page with a durable request budget and resume checkpoint.'''
    if max_retries < 0:
        raise ValueError('Maximum retries cannot be negative.')
    if start_page < 1 or max_page < start_page:
        raise ValueError('Page range must start at 1 or later and include at least one page.')
    if backoff_base_seconds < 0 or request_delay < 0:
        raise ValueError('Backoff and request delay values cannot be negative.')

    if fetch_page is None and not API_KEY:
        raise ValueError('API key is not found.')
    history = history or CollectionHistory()
    fetch_page = fetch_page or fetch_jobs_page
    sleep = sleep or time.sleep
    run_id = history.begin_run(search_task, start_page, max_page, run_id)
    for task_number, task in enumerate(search_task, start=1):
        query_key = history.query_key(task)
        if history.query_complete(query_key):
            continue
        print(f'search task {task_number}/{len(search_task)}: {query_key}')
        print(f'search query: {task["query"]}')
        empty_page = False
        for page_number in range(start_page, max_page + 1):
            stored_page = history.read_page(query_key, page_number)
            if history.page_complete(query_key, page_number) or stored_page is not None:
                if stored_page is None:
                    raise FileNotFoundError(
                        f'Missing stored response for {query_key}, page {page_number}.'
                    )
                if not history.page_complete(query_key, page_number):
                    history.complete_page(task, page_number, len(stored_page))
                if not stored_page:
                    empty_page = True
                    break
                continue

            attempt = history.next_attempt(query_key, page_number)
            retries_this_call = 0
            while True:
                try:
                    history.start_attempt(task, page_number, attempt)
                except RequestBudgetExceeded as error:
                    history.fail_query(task, page_number, type(error).__name__, str(error))
                    raise CollectionRunIncomplete(run_id, query_key, str(error)) from error
                try:
                    jobs = fetch_page(task, page_number)
                except Exception as error:
                    status = _response_status(error)
                    history.finish_attempt(
                        task,
                        page_number,
                        attempt,
                        outcome='failed',
                        response_status=status,
                        error_type=type(error).__name__,
                    )
                    if _is_retryable(error) and retries_this_call < max_retries:
                        next_attempt = attempt + 1
                        try:
                            history.assert_can_attempt(next_attempt)
                        except RequestBudgetExceeded as budget_error:
                            history.fail_query(
                                task,
                                page_number,
                                type(budget_error).__name__,
                                str(budget_error),
                            )
                            raise CollectionRunIncomplete(
                                run_id, query_key, str(budget_error)
                            ) from budget_error
                        delay = _retry_delay(error, attempt, backoff_base_seconds)
                        sleep(delay)
                        attempt = next_attempt
                        retries_this_call += 1
                        continue

                    reason = f'{type(error).__name__} (HTTP {status})' if status else type(error).__name__
                    history.fail_query(task, page_number, type(error).__name__, reason)
                    raise CollectionRunIncomplete(run_id, query_key, reason) from error

                tracked_jobs = add_track_fields(jobs, task, page_number)
                history.write_page(query_key, page_number, tracked_jobs)
                history.finish_attempt(
                    task,
                    page_number,
                    attempt,
                    outcome='succeeded',
                    response_status=200,
                    result_count=len(tracked_jobs),
                )
                history.complete_page(task, page_number, len(tracked_jobs))
                print(f'collected jobs: {len(tracked_jobs)}')
                if not tracked_jobs:
                    print(f'no jobs found for this page: {page_number}')
                    empty_page = True
                if request_delay:
                    sleep(request_delay)
                break
            if empty_page:
                break
        history.complete_query(task, empty_page=empty_page)

    history.complete_run()
    return history.load_run_jobs()

def clean_invalid_unicode(value):
    '''Remove non-ASCII characters from a string.'''
    if isinstance(value, str):
        return value.encode('utf-8', errors='replace').decode('utf-8')
    return value

def save_raw_jobs(jobs: list[dict]) -> Path:
    '''Save collected jobs to RAW_DATA_DIR.'''
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    output_path = RAW_DATA_DIR / f'jobs_{timestamp}.jsonl'
    df_jobs = pd.DataFrame(jobs)
    df_jobs.to_json(output_path, orient='records', lines=True, force_ascii=False)
    return output_path




if __name__ == '__main__':
    all_jobs = collect_all_jobs(SEARCH_QUERIES, START_PAGE, MAX_PAGES, REQUEST_DELAY_SECONDS)
    if all_jobs:
        output_path = save_raw_jobs(all_jobs)
        df_jobs = pd.DataFrame(all_jobs)
        print(f'Raw rows collected: {len(df_jobs)}')
        print(f'Unique job IDs: {df_jobs['job_id'].nunique()}')
        print(f'Raw jobs save to {output_path}')
    else:
        print('No jobs were collected.')

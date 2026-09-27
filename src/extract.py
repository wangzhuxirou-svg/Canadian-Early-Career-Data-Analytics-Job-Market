import os
import requests
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime, timezone
import time
from pathlib import Path
from config.settings import RAW_DATA_DIR, REQUEST_DELAY_SECONDS, MAX_PAGES
from config.settings import API_FIELDS, SEARCH_QUERIES, COUNTRY, DATE_POST, START_PAGE

load_dotenv()
API_KEY = os.getenv('JSEARCH_API_KEY')
API_URL = 'https://api.openwebninja.com/jsearch/search-v2'


def add_track_fields(jobs: list[dict], search_task: dict, page_number: int) -> list[dict]:
    '''Reformat the information from jobs and add track fields to API_FIELDS with search_tasks and page_number.'''
    collected_at = datetime.now(timezone.utc).isoformat()
    tracked_jobs = []
    for job in jobs:
        record = {field: clean_invalid_unicode(job.get(field)) for field in API_FIELDS}
        record.update({
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
    response = requests.get(API_URL, headers=headers, params=params, timeout=1800)
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


def collect_all_jobs(search_task: list[dict], start_page: int, max_page: int, request_delay: float) -> list[dict]:
    '''Collect all jobs using search_task and page_number.'''
    all_jobs = []
    for task_number, search_task in enumerate(search_task, start=1):
        print(f'search task {task_number}/ {search_task}')
        print(f'search query: {search_task["query"]}')
        for page_number in range(start_page, max_page + 1):
            jobs = fetch_jobs_page(search_task, page_number)
            if not jobs:
                print(f'no jobs found for this page: {page_number}')
                break
            tracked_jobs = add_track_fields(jobs, search_task, page_number)
            all_jobs.extend(tracked_jobs)
            print(f'collected jobs: {len(tracked_jobs)}')
            print(f'number of jobs so far: {len(all_jobs)}')
            time.sleep(request_delay)
    return all_jobs

def clean_invalid_unicode(value):
    '''Remove non-ASCII characters from a string.'''
    if isinstance(value, str):
        return value.encode('utf-8', errors='replace').decode('utf-8')
    return value

def save_raw_jobs(jobs: list[dict]) -> Path:
    '''Save collected jobs to RAW_DATA_DIR.'''
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
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





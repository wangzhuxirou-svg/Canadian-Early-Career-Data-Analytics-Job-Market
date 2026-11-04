import json

import pytest
import requests

from src import extract
from src.history import CollectionHistory


def task(role_family='data_analyst', search_level='internship'):
    return {
        'role_family': role_family,
        'search_level': search_level,
        'role_query': role_family.replace('_', ' '),
        'query': f'{role_family} {search_level}',
    }


def test_timeout_retries_once_and_records_attempt_and_query_completion(tmp_path):
    history = CollectionHistory(tmp_path, monthly_budget=3, retry_reserve=1)
    calls = []
    delays = []

    def fetch_page(search_task, page_number):
        calls.append((search_task['role_family'], page_number))
        if len(calls) == 1:
            raise requests.Timeout('simulated timeout')
        return [{'job_id': 'job-1', 'job_title': 'Data Analyst'}]

    jobs = extract.collect_all_jobs(
        [task()],
        1,
        1,
        0,
        history=history,
        fetch_page=fetch_page,
        max_retries=1,
        backoff_base_seconds=0.25,
        sleep=delays.append,
        run_id='timeout-test',
    )

    assert len(jobs) == 1
    assert calls == [('data_analyst', 1), ('data_analyst', 1)]
    assert delays == [0.25]
    assert history.request_counts() == (1, 1)
    events = [json.loads(line) for line in history.ledger_path.read_text(encoding='utf-8').splitlines()]
    assert [event['outcome'] for event in events if event['event'] == 'request_attempt_finished'] == [
        'failed', 'succeeded',
    ]
    assert [event['event'] for event in events if event['event'] == 'query_completed'] == [
        'query_completed',
    ]


def test_rate_limit_retry_after_and_empty_page_are_recorded(tmp_path):
    history = CollectionHistory(tmp_path, monthly_budget=3, retry_reserve=1)
    rate_limit_response = requests.Response()
    rate_limit_response.status_code = 429
    rate_limit_response.headers['Retry-After'] = '0'
    rate_limit = requests.HTTPError('simulated rate limit', response=rate_limit_response)
    responses = [rate_limit, []]
    delays = []

    def fetch_page(*_):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    jobs = extract.collect_all_jobs(
        [task()],
        1,
        3,
        0,
        history=history,
        fetch_page=fetch_page,
        max_retries=1,
        sleep=delays.append,
        run_id='rate-limit-test',
    )

    assert jobs == []
    assert delays == [0]
    checkpoint = json.loads(
        (tmp_path / 'jsearch_checkpoint_rate-limit-test.json').read_text(encoding='utf-8')
    )
    query = checkpoint['queries']['data_analyst:internship']
    assert query['status'] == 'complete'
    assert query['empty_page'] is True
    assert query['pages'] == {'1': 0}
    assert history.request_counts() == (1, 1)


def test_mid_run_failure_resumes_completed_query_without_repeating_it(tmp_path):
    history = CollectionHistory(tmp_path, monthly_budget=8, retry_reserve=4)
    tasks = [task(), task('business_analyst')]
    calls = []

    def fail_second_query(search_task, page_number):
        calls.append(search_task['role_family'])
        if search_task['role_family'] == 'business_analyst':
            raise requests.HTTPError('simulated non-retryable response')
        return [{'job_id': 'job-1', 'job_title': 'Data Analyst'}]

    with pytest.raises(extract.CollectionRunIncomplete, match='No processed dataset was published'):
        extract.collect_all_jobs(
            tasks,
            1,
            1,
            0,
            history=history,
            fetch_page=fail_second_query,
            max_retries=1,
            sleep=lambda _: None,
            run_id='resume-test',
        )

    def complete_second_query(search_task, page_number):
        calls.append(search_task['role_family'])
        return [{'job_id': 'job-2', 'job_title': 'Business Analyst'}]

    resumed_jobs = extract.collect_all_jobs(
        tasks,
        1,
        1,
        0,
        history=history,
        fetch_page=complete_second_query,
        max_retries=1,
        sleep=lambda _: None,
    )

    assert calls == ['data_analyst', 'business_analyst', 'business_analyst']
    assert {job['job_id'] for job in resumed_jobs} == {'job-1', 'job-2'}
    checkpoint = json.loads(
        (tmp_path / 'jsearch_checkpoint_resume-test.json').read_text(encoding='utf-8')
    )
    assert checkpoint['status'] == 'complete'
    assert all(query['status'] == 'complete' for query in checkpoint['queries'].values())


def test_monthly_base_budget_stops_collection_before_an_extra_request(tmp_path):
    history = CollectionHistory(tmp_path, monthly_budget=2, retry_reserve=1)
    tasks = [task(), task('business_analyst')]

    with pytest.raises(extract.CollectionRunIncomplete, match='base-request budget exhausted'):
        extract.collect_all_jobs(
            tasks,
            1,
            1,
            0,
            history=history,
            fetch_page=lambda *_: [],
            run_id='budget-test',
        )

    assert history.request_counts() == (1, 0)
    checkpoint = json.loads(
        (tmp_path / 'jsearch_checkpoint_budget-test.json').read_text(encoding='utf-8')
    )
    assert checkpoint['queries']['data_analyst:internship']['status'] == 'complete'
    assert checkpoint['queries']['business_analyst:internship']['status'] == 'failed'


def test_confirmed_budget_reserves_thirty_attempts_from_base_capacity(tmp_path):
    history = CollectionHistory(tmp_path, monthly_budget=150, retry_reserve=30)

    assert history.base_request_capacity == 120
    history.assert_can_attempt(1)
    history.assert_can_attempt(2)


def test_fetch_jobs_page_uses_separate_configured_timeouts(monkeypatch):
    monkeypatch.setattr(extract, 'API_KEY', 'local-test-key')
    monkeypatch.setattr(extract, 'CONNECT_TIMEOUT_SECONDS', 2)
    monkeypatch.setattr(extract, 'READ_TIMEOUT_SECONDS', 8)
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {'data': []}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(extract.requests, 'get', fake_get)

    assert extract.fetch_jobs_page(task(), 1) == []
    assert captured['timeout'] == (2, 8)

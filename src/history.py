import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from config.settings import PROJECT_ROOT


DEFAULT_MONTHLY_REQUEST_BUDGET = 150
DEFAULT_RETRY_REQUEST_RESERVE = 30


class RequestBudgetExceeded(RuntimeError):
    pass


class CollectionHistory:
    """Durable request ledger and resumable page checkpoint for JSearch runs."""

    def __init__(
        self,
        history_dir: Path | None = None,
        monthly_budget: int | None = None,
        retry_reserve: int | None = None,
        clock=None,
    ):
        if monthly_budget is None:
            monthly_budget = int(os.getenv(
                'JSEARCH_MONTHLY_REQUEST_BUDGET',
                str(DEFAULT_MONTHLY_REQUEST_BUDGET),
            ))
        if retry_reserve is None:
            retry_reserve = int(os.getenv(
                'JSEARCH_RETRY_REQUEST_RESERVE',
                str(DEFAULT_RETRY_REQUEST_RESERVE),
            ))
        if monthly_budget < 1:
            raise ValueError('Monthly request budget must be positive.')
        if retry_reserve < 0 or retry_reserve >= monthly_budget:
            raise ValueError('Retry reserve must be non-negative and below the monthly budget.')
        self.history_dir = Path(history_dir or PROJECT_ROOT / 'data' / 'history')
        self.monthly_budget = monthly_budget
        self.retry_reserve = retry_reserve
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.month = self.clock().astimezone(timezone.utc).strftime('%Y-%m')
        self.ledger_path = self.history_dir / f'jsearch_requests_{self.month}.jsonl'
        self.active_path = self.history_dir / f'jsearch_active_{self.month}.json'
        self.run_id = None
        self.checkpoint = None

    @staticmethod
    def query_key(task: dict) -> str:
        return f'{task["role_family"]}:{task["search_level"]}'

    def _checkpoint_path(self, run_id: str) -> Path:
        return self.history_dir / f'jsearch_checkpoint_{run_id}.json'

    def _now_text(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

    def _write_atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + '.tmp')
        with temporary_path.open('w', encoding='utf-8', newline='\n') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)

    def _save_checkpoint(self) -> None:
        self.checkpoint['updated_at'] = self._now_text()
        self._write_atomic(
            self._checkpoint_path(self.run_id),
            json.dumps(self.checkpoint, ensure_ascii=False, indent=2) + '\n',
        )
        self._write_atomic(
            self.active_path,
            json.dumps({'run_id': self.run_id, 'status': self.checkpoint['status']}) + '\n',
        )

    def _append_event(self, event: dict) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open('a', encoding='utf-8', newline='\n') as file:
            file.write(json.dumps(event, ensure_ascii=False) + '\n')
            file.flush()
            os.fsync(file.fileno())

    def _events(self):
        if not self.ledger_path.exists():
            return
        with self.ledger_path.open(encoding='utf-8') as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f'Invalid request ledger JSON at {self.ledger_path}:{line_number}.'
                    ) from error

    def request_counts(self) -> tuple[int, int]:
        base_requests = 0
        retry_requests = 0
        for event in self._events() or ():
            if event.get('event') != 'request_attempt_started':
                continue
            if event.get('is_retry'):
                retry_requests += 1
            else:
                base_requests += 1
        return base_requests, retry_requests

    @property
    def base_request_capacity(self) -> int:
        return self.monthly_budget - self.retry_reserve

    def next_attempt(self, query_key: str, page_number: int) -> int:
        return 1 + sum(
            1
            for event in self._events() or ()
            if event.get('event') == 'request_attempt_started'
            and event.get('run_id') == self.run_id
            and event.get('query_key') == query_key
            and event.get('page_number') == page_number
        )

    def assert_can_attempt(self, attempt: int) -> None:
        base_requests, retry_requests = self.request_counts()
        if attempt == 1:
            base_limit = self.base_request_capacity
            if base_requests >= base_limit:
                raise RequestBudgetExceeded(
                    f'Monthly base-request budget exhausted ({self.base_request_capacity} requests).'
                )
        elif retry_requests >= self.retry_reserve:
            raise RequestBudgetExceeded(
                f'Monthly retry reserve exhausted ({self.retry_reserve} requests).'
            )

    def begin_run(self, tasks: list[dict], start_page: int, max_page: int, run_id: str | None = None) -> str:
        query_specs = {}
        for task in tasks:
            key = self.query_key(task)
            query_specs[key] = {
                'role_family': task['role_family'],
                'search_level': task['search_level'],
                'query': task['query'],
            }
        query_keys = [
            f'{self.query_key(task)}:{page_number}'
            for task in tasks
            for page_number in range(start_page, max_page + 1)
        ]

        if run_id is None and self.active_path.exists():
            active = json.loads(self.active_path.read_text(encoding='utf-8'))
            candidate = active.get('run_id')
            candidate_path = self._checkpoint_path(candidate) if candidate else None
            if candidate_path and candidate_path.exists():
                saved = json.loads(candidate_path.read_text(encoding='utf-8'))
                if saved.get('status') != 'complete':
                    if saved.get('query_keys') != query_keys:
                        raise ValueError('Cannot resume a collection run with a changed query schedule.')
                    run_id = candidate
                    self.checkpoint = saved

        if run_id is None:
            run_id = self.clock().astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', run_id):
            raise ValueError('Collection run ID may contain only letters, numbers, underscores, and hyphens.')
        self.run_id = run_id

        if self.checkpoint is None:
            checkpoint_path = self._checkpoint_path(run_id)
            if checkpoint_path.exists():
                self.checkpoint = json.loads(checkpoint_path.read_text(encoding='utf-8'))
                if self.checkpoint.get('query_keys') != query_keys:
                    raise ValueError('Cannot resume a collection run with a changed query schedule.')
            else:
                self.checkpoint = {
                    'run_id': run_id,
                    'month': self.month,
                    'status': 'in_progress',
                    'started_at': self._now_text(),
                    'query_keys': query_keys,
                    'query_specs': query_specs,
                    'queries': {
                        key: {'status': 'pending', 'pages': {}, 'attempt_count': 0}
                        for key in query_specs
                    },
                }
        if self.checkpoint.get('status') == 'complete':
            raise ValueError(f'Collection run {run_id} is already complete.')
        self.checkpoint['status'] = 'in_progress'
        for key in query_specs:
            self.checkpoint['queries'].setdefault(
                key, {'status': 'pending', 'pages': {}, 'attempt_count': 0}
            )
        self._save_checkpoint()
        return run_id

    def _query_state(self, query_key: str) -> dict:
        return self.checkpoint['queries'][query_key]

    def query_complete(self, query_key: str) -> bool:
        return self._query_state(query_key)['status'] == 'complete'

    def page_complete(self, query_key: str, page_number: int) -> bool:
        return str(page_number) in self._query_state(query_key)['pages']

    def _page_path(self, query_key: str, page_number: int) -> Path:
        token = hashlib.sha256(f'{self.run_id}:{query_key}:{page_number}'.encode()).hexdigest()
        return self.history_dir / 'jsearch_pages' / self.run_id / f'{token}.json'

    def read_page(self, query_key: str, page_number: int) -> list[dict] | None:
        page_path = self._page_path(query_key, page_number)
        if not page_path.exists():
            return None
        page = json.loads(page_path.read_text(encoding='utf-8'))
        if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
            raise ValueError(f'Invalid page checkpoint: {page_path}')
        return page

    def write_page(self, query_key: str, page_number: int, jobs: list[dict]) -> None:
        self._write_atomic(
            self._page_path(query_key, page_number),
            json.dumps(jobs, ensure_ascii=False) + '\n',
        )

    def start_attempt(
        self,
        task: dict,
        page_number: int,
        attempt: int,
    ) -> None:
        self.assert_can_attempt(attempt)
        query_key = self.query_key(task)
        self._append_event({
            'event': 'request_attempt_started',
            'timestamp': self._now_text(),
            'month': self.month,
            'run_id': self.run_id,
            'query_key': query_key,
            'role_family': task['role_family'],
            'search_level': task['search_level'],
            'search_query': task['query'],
            'page_number': page_number,
            'attempt': attempt,
            'is_retry': attempt > 1,
        })
        state = self._query_state(query_key)
        state['status'] = 'in_progress'
        state['attempt_count'] = state.get('attempt_count', 0) + 1
        self._save_checkpoint()

    def finish_attempt(
        self,
        task: dict,
        page_number: int,
        attempt: int,
        outcome: str,
        response_status: int | None = None,
        result_count: int | None = None,
        error_type: str | None = None,
    ) -> None:
        self._append_event({
            'event': 'request_attempt_finished',
            'timestamp': self._now_text(),
            'month': self.month,
            'run_id': self.run_id,
            'query_key': self.query_key(task),
            'role_family': task['role_family'],
            'search_level': task['search_level'],
            'page_number': page_number,
            'attempt': attempt,
            'outcome': outcome,
            'response_status': response_status,
            'result_count': result_count,
            'error_type': error_type,
        })

    def complete_page(
        self,
        task: dict,
        page_number: int,
        result_count: int,
    ) -> None:
        key = self.query_key(task)
        self._query_state(key)['pages'][str(page_number)] = result_count
        self._save_checkpoint()

    def complete_query(self, task: dict, empty_page: bool = False) -> None:
        key = self.query_key(task)
        state = self._query_state(key)
        state['status'] = 'complete'
        state['record_count'] = sum(state['pages'].values())
        state['empty_page'] = empty_page
        spec = self.checkpoint['query_specs'][key]
        self._append_event({
            'event': 'query_completed',
            'timestamp': self._now_text(),
            'month': self.month,
            'run_id': self.run_id,
            'query_key': key,
            'role_family': spec['role_family'],
            'search_level': spec['search_level'],
            'search_query': spec['query'],
            'record_count': state['record_count'],
            'empty_page': empty_page,
        })
        self._save_checkpoint()

    def fail_query(
        self,
        task: dict,
        page_number: int,
        error_type: str,
        reason: str,
    ) -> None:
        key = self.query_key(task)
        state = self._query_state(key)
        state.update({
            'status': 'failed',
            'failed_page': page_number,
            'failure_type': error_type,
            'failure_reason': reason,
        })
        self.checkpoint.update({'status': 'incomplete', 'failure_query': key})
        self._append_event({
            'event': 'query_failed',
            'timestamp': self._now_text(),
            'month': self.month,
            'run_id': self.run_id,
            'query_key': key,
            'role_family': task['role_family'],
            'search_level': task['search_level'],
            'page_number': page_number,
            'error_type': error_type,
            'reason': reason,
        })
        self._save_checkpoint()

    def complete_run(self) -> None:
        self.checkpoint['status'] = 'complete'
        self._save_checkpoint()
        self._append_event({
            'event': 'run_completed',
            'timestamp': self._now_text(),
            'month': self.month,
            'run_id': self.run_id,
            'query_count': len(self.checkpoint['query_keys']),
        })

    def load_run_jobs(self) -> list[dict]:
        jobs = []
        for key in self.checkpoint['query_specs']:
            for page_number in sorted(
                (int(page) for page in self._query_state(key)['pages'])
            ):
                page = self.read_page(key, page_number)
                if page is None:
                    raise FileNotFoundError(
                        f'Missing stored response for {key}, page {page_number}.'
                    )
                jobs.extend(page)
        return jobs

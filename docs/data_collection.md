# JSearch collection budget, checkpoints, and snapshots

## Request schedule and budget

The configured search matrix has 8 role families × 3 career levels = 24
queries. `MAX_PAGES=1`, so one complete weekly collection run makes 24 initial
requests; five weekly runs use 120 base requests. The configured monthly limit
is 150 request attempts, with 30 reserved exclusively for retries. The user
confirmed these request-count limits on 2026-10-27. They do not establish the
provider's credit or monetary billing; verify that separately with JSearch
before enabling collection.

Override the limits in `.env` or the process environment:

```text
JSEARCH_MONTHLY_REQUEST_BUDGET=150
JSEARCH_RETRY_REQUEST_RESERVE=30
```

The base pool is the monthly budget less the retry reserve. A request attempt
is durably recorded before dispatch. Timeouts, connection failures, HTTP 429,
and HTTP 5xx receive at most two retries per invocation, subject to the
monthly reserve. `Retry-After` is honored when present; otherwise exponential
backoff starts at one second. All backoff delays are capped at 30 seconds.
Connection and read timeouts are independently configurable with
`JSEARCH_CONNECT_TIMEOUT_SECONDS` and `JSEARCH_READ_TIMEOUT_SECONDS`.

The application does not schedule weekly runs. An authorized caller invokes
the normal pipeline once per weekly round. If collection stops, the next call
automatically resumes the incomplete run; `run_pipeline(collection_run_id=...)`
can explicitly select a checkpoint to resume. A successful empty page counts
as a completed query and ends pagination for it.

## Attempts, incomplete runs, and formal outputs

Runtime collection state is local and ignored by Git:

- `data/history/jsearch_requests_YYYY-MM.jsonl`: request attempts, outcomes,
  completed queries, failures, and completed runs.
- `data/history/jsearch_checkpoint_<run-id>.json`: per-query page status and
  resume state.
- `data/history/jsearch_pages/<run-id>/`: durable raw page responses used to
  resume without repeating completed requests.

If a query remains unsuccessful after retries or the budget is exhausted,
collection raises `CollectionRunIncomplete` and leaves the checkpoint for
resumption. The pipeline does not save that partial collection as a raw batch
or replace the processed formal outputs. No real API request was made for this
implementation; timeout, 429, empty-page, budget-exhaustion, and mid-run
failure behavior is exercised with local fakes in the tests.

## Cumulative JSearch history

Completed pipeline batches merge by `(source, source_job_id)`; the first
adapter is JSearch, where `source_job_id` maps to the provider's `job_id`.
Records retain `first_seen_at`, `last_seen_at`, the original
`collected_at_utc`, and deduplicated query/time observations in
`collection_history`. The current cumulative JSONL is
`data/history/market_jobs.jsonl`. Each completed pipeline run also creates a
new immutable snapshot under `data/history/snapshots/<run-id>/`; an existing
snapshot ID is never overwritten.

The key includes `source`, so equal IDs from different sources are not merged.
No cross-source deduplication is implemented. Changes in query coverage,
budget, or search terms affect observed counts and must not be interpreted by
themselves as market growth.

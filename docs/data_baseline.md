# D0 data baseline and offline validation

## Baseline environment

Baseline captured on 2026-10-27 from commit `d33149a` before the D1 role
classification changes. No `AGENTS.md` was present at the repository root or
its parent.

- Python: 3.14.0
- pandas: 3.0.5
- NumPy: 2.5.3
- pytest: 9.1.1
- requests: 2.34.2
- python-dotenv: 1.2.3
- jsonschema: 4.26.0
- Existing test suite before D0 changes: `29 passed`
- `engineer_features()` calls `validate_features()` after creating role,
  career-level, skill, major, and experience features. The regression test
  `test_engineer_features_runs_validation` confirms invalid duplicate IDs fail
  through the public feature-engineering entry point.

## Fixed offline input

Use the four versioned raw batches that define the original archived snapshot.
They contain 643 rows and 500 unique `job_id` values. Later raw batches in
`data/raw/` are intentionally excluded so the baseline does not drift.

| File | Rows | SHA-256 |
|---|---:|---|
| `data/raw/jobs_20260911_071658.jsonl` | 450 | `a3eb3821c9f9d71d6a17e5526877cac9152a69737f312dd92a24b612eb1202d0` |
| `data/raw/jobs_20260920_005742.jsonl` | 83 | `0cfbdafe9d10423061b8941938fe1448e5280321a9beeb2fae1b7343b3d3a382` |
| `data/raw/jobs_20260920_011920.jsonl` | 60 | `b7fd907d73e64918cada30ffcf8483881ac12d8f35c7cb7883e281754d676530` |
| `data/raw/jobs_20260920_012618.jsonl` | 50 | `465ae7cc59417da7bcfe01aa1b7c8708852ef073de0a1e5f85f07e7cd6f7f313` |

## Reproducible checks

Run the test suite:

```powershell
python -m pytest -q
```

Replay the fixed inputs without calling the API or writing into the project's
`data/processed/` and `data/runs/` directories:

```powershell
@'
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import src.pipeline as pipeline

files = [
    Path(r"data\raw\jobs_20260911_071658.jsonl"),
    Path(r"data\raw\jobs_20260920_005742.jsonl"),
    Path(r"data\raw\jobs_20260920_011920.jsonl"),
    Path(r"data\raw\jobs_20260920_012618.jsonl"),
]
jobs = [
    json.loads(line)
    for path in files
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
assert len(jobs) == 643
assert len({job["job_id"] for job in jobs}) == 500

with TemporaryDirectory() as output_dir:
    pipeline.PROJECT_ROOT = Path(output_dir)
    pipeline.PROCESSED_DATA_DIR = Path(output_dir) / "data" / "processed"
    pipeline._configure_logging = lambda: None
    outputs = pipeline.run_pipeline(jobs, save_raw_batch=False)
    for name in ("cleaned_jobs", "featured_jobs", "analysis_jobs", "review_queue"):
        count = sum(1 for line in outputs[name].read_text(encoding="utf-8").splitlines() if line)
        print(f"{name}: {count}")
'@ | python -
```

The pipeline is invoked with supplied rows, so `collect_all_jobs()` and the
network client are not used. `save_raw_batch=False` prevents a new collection
batch from being written. Output files live in a temporary directory.

## Baseline results and caveats

The pre-change tests passed (`29 passed`). Fixed replay observed:

- cleaned rows: 492
- featured rows: 492
- analysis rows: 180
- review queue rows: 282

The replay exercises the full cleaning, feature-engineering, validation,
review, and analysis-filter path without an API request.

After D1, replaying the same fixed input produced 169 analysis rows and 293
review-queue rows (492 cleaned and featured rows). The analysis count differs
from the pre-D1 baseline because title rules were narrowed and additional
families were included. This is a classifier/filter change, not new market
growth.

Test/schema success demonstrates structural validity only; it is not evidence
of classification accuracy. The four batches are the fixed input for
before/after comparisons. Keep collection timestamps from those raw rows
unchanged during replay.

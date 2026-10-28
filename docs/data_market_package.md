# D4 historical market package

The pipeline now carries provider identity independently from
`career_level_evidence_source`, stores featured rows in cumulative history,
filters the cumulative records with the same analysis eligibility rules, maps
them to the shared `market-job` contract, validates the complete bundle, and
writes reference statistics. Raw descriptions and internal extraction fields
are not included in the exported package. A date-only posting stays
date-precision and has no fabricated midnight timestamp.

On detecting a legacy cumulative history without classifier and skill
features, the pipeline rebuilds it from all archived `data/raw/jobs_*.jsonl`
batches plus the current input. It fails before publishing if the archive is
missing or does not contain every legacy job ID. The rebuilt records flow
through the current cleaner and feature engineer before export.

History updates, the immutable run snapshot, and package are first built in a
temporary staging directory. The package is contract-validated before the
snapshot/package are moved into place and the cumulative-history file is
atomically replaced. If package construction or validation fails, staged data
is discarded and existing history and formal processed files remain unchanged.

## Reproducible offline build

This uses every archived JSONL batch in `data/raw/`. It supplies those rows to
the pipeline directly, so collection and the network client are not invoked.
Pipeline state is redirected to a temporary directory to avoid replacing
formal processed outputs. Only the sanitized draft package is copied to the
ignored `data/history/` directory. The supplied archive rows are all re-cleaned
and re-featured; normal runs also trigger the documented migration when they
detect an old cumulative history without features.

```powershell
@'
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import src.pipeline as pipeline

raw_files = sorted(Path(r"data\raw").glob("jobs_*.jsonl"))
if not raw_files:
    raise RuntimeError("No archived raw JSONL batches found.")
jobs = [
    json.loads(line)
    for path in raw_files
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
print(f"Raw batches: {len(raw_files)}; raw rows: {len(jobs)}")

with TemporaryDirectory(prefix="market-package-build-") as temp_dir:
    temp_root = Path(temp_dir)
    pipeline.PROJECT_ROOT = temp_root
    pipeline.PROCESSED_DATA_DIR = temp_root / "data" / "processed"
    pipeline._configure_logging = lambda: None
    outputs = pipeline.run_pipeline(jobs, save_raw_batch=False)
    package = outputs["market_package"]
    target = Path("data/history") / f"d4-market-package-{outputs['run_archive'].name}"
    shutil.copytree(package, target)
    bundle = json.loads((target / "bundle.json").read_text(encoding="utf-8"))
    stats = json.loads((target / "reference_statistics.json").read_text(encoding="utf-8"))
    print(f"Draft package: {target}")
    print(f"Market jobs: {bundle['manifest']['sample_count']}")
    print(f"Analyzable descriptions: {stats['analyzable_description_count']}")
    print("Package run_status:", bundle["manifest"]["run_status"])
'@ | python -
```

The package contains `bundle.json` for contract validation and standalone
`manifest.json`, `market_jobs.json`, `skills.json`, `courses.json`,
`learning_resources.json`, and `reference_statistics.json` for convenient
client loading. The skills catalog is the union of skills referenced by market
jobs, courses, and learning resources. `SHA256SUMS.txt` contains the bundle
digest. `run_status` is deliberately `partial`: the
historical classifier output has not received the required manual review.
Keep this package for internal frontend/data-contract integration only. Do not
present it as a verified or current market snapshot until the role review is
completed and the package is explicitly approved. Counts describe archived
query coverage and are not counts of currently open positions.

The source archive does not contain a clear product-analyst positive example.
Review the stratified samples documented in `data_role_review.md`, including
small-sample families and ambiguous classifications, before any public release.

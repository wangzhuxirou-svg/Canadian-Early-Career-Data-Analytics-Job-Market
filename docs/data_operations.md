# D7 data package operations

## Formal release

The production contract is `2.0.0`. It uses the merged D7 payload shape,
production schema overlays, and two release evidence documents. The `1.0.0`
website baseline and `1.1.0` integration packages remain suitable for their
original versioned readers. Older incomplete market runs, including the
archived cumulative data, cannot authorize production.

### Authorized collection

Run in the controlled environment that holds the authorized JSearch
credential, existing request ledger, and actual usage/budget evidence.
See [data collection](data_collection.md) for the 24-task matrix and
the 150-attempt monthly budget including 30 retry attempts. Confirm the
account balance and provider terms before the first request, and keep a single
active collector on the existing ledger. Set `JSEARCH_API_KEY` through the
approved secret store or session environment.

The configured transport uses the direct JSearch `/search-v2` API and
`x-api-key` header. If the existing approved plan uses another gateway,
verify the corresponding endpoint and credential before collecting. The
matrix reads the first response page per task. [JSearch API](https://www.openwebninja.com/api/jsearch)

```powershell
python -m src.formal_collection --collect --usage-ref "<source-usage-record>"
# Resume the same incomplete round using the same ledger and identity.
python -m src.formal_collection --collect --run-id "<run-id>" --usage-ref "<source-usage-record>"
# Recover an already completed checkpoint without a provider request.
python -m src.formal_collection --from-completed-run --run-id "<run-id>" --usage-ref "<source-usage-record>"
```

Use a clean checkout on controlled storage. The command saves raw pages
and attempt details only in ignored `data/history/`. Root `data/raw/` also
ignores new files from the older pipeline. Seven historical inputs were
already tracked upstream for offline replay, including full descriptions.
Release packages and website artifacts use the description-free bundle.
Malformed/error response payloads do not count as empty
successes. The release input under `data/history/formal-runs/<run-id>/`
appears after the entire matrix completes. `market-coverage.json` records
query status, attempts, response rows and deduplicated counts.
`role-review-sample.json` lists up to ten eligible postings per role with
empty decision and basis fields for manual review. Empty roles stay explicit.
The underlying ledger records outcomes, HTTP status, retries and checkpoint
state for unsuccessful rounds. The resulting market window describes this
round rather than the existing cumulative historical snapshot.

### Candidate and approval

Create a release-evidence input in controlled storage using
`../contracts/schemas/release-evidence.schema.json`. Copy the completed
`market-coverage.json` object into `market`, set `data_mode` to the correct
mode, and inventory actual course, resource, organizer, and opportunity source
decisions under `sources`. Only publish records with checked sources and an
applicable permission reference. Use `--resources-dir` for a reviewed
catalog export, and preserve original verification timestamps until rechecked.
An official event import is optional and uses `--official-opportunities`;
see [source recheck](source-recheck-20261126.md) for the explicitly unsigned
review examples. A zero-opportunity release must have an actually checked
opportunity source. Instagram remains subject to its separate access scope.

```powershell
python -m src.export_bundle build `
  --market-package 'data/history/formal-runs/<run-id>/market' `
  --output 'data/history/d7-candidate-<version>' `
  --dataset-version '<version>' `
  --release-evidence 'data/history/release-evidence-<version>.json'
python -m src.export_bundle validate --package 'data/history/d7-candidate-<version>'
```

The command reports `candidate_sha256`. A human reviewer verifies the
source license and public scope, role sample, course/resource basis, event
dates, and exact package version and digest against the private evidence.
Record the approval in a protected system, then copy the unsigned template
outside the candidate and fill in the digest, reviewer, UTC time, approval
reference, and confirmations. Never create a production approval from a
fixture or approve by merely changing a version value. In the website release
PR, pin the same version and candidate digest against that protected record.

```powershell
python -m src.export_bundle validate `
  --package 'data/history/d7-candidate-<version>' --for-publication `
  --attestation 'data/history/release-attestation-<version>.json'
python -m src.export_bundle publish `
  --package 'data/history/d7-candidate-<version>' `
  --attestation 'data/history/release-attestation-<version>.json' `
  --output 'data/history/d7-published-<version>'
python -m src.export_bundle validate --package 'data/history/d7-published-<version>' --for-publication
```

Hand off the new immutable package through the approved private channel.
Review materials and approval references stay controlled. Confirm a manual
website update and deliberately rejected update first. Keep the last approved
site/package pair for rollback. Enable a scheduled candidate collection only
after that recovery check and actual account authorization. Scheduling
collection does not enable automatic publication or renew the human approval.

## Offline package build

`src.export_bundle` assembles an offline package from an already saved market
package. It does not import the collection pipeline, extract jobs, or call
JSearch. The market bundle's `collected_from` and `collected_to` are retained;
the new `dataset_provenance.json` records Instagram snapshot times for included
eligible opportunities and verification times for courses, resources, and
organizers. The current local course/resource catalogs are reused rather than
the potentially older copies embedded in the market package; their
`verified_at` values are preserved. The skills catalog is rebuilt from the
reused market data and current references. Verification timestamps are not
presented as collection times.

```powershell
python -m src.export_bundle build `
  --market-package "data\history\d4-market-package-<run-id>" `
  --output "data\history\d7-candidate-<version>" `
  --dataset-version "<major.minor.patch>" `
  --upgrade-legacy-manifest `
  --log-file "data\history\d7-build.log"

python -m src.export_bundle validate `
  --package "data\history\d7-candidate-<version>"
```

The builder re-evaluates D6 eligibility using the current source versions and
merged event records. It includes automatic candidates and current human-approved
records; secondary duplicate posts, rejected/held records, stale approvals and
expired opportunities are omitted. Partial snapshots with usable caption/OCR
evidence are allowed under D6 rules. Missing or disabled organizers are omitted;
missing organizer verification is retained as a warning for internal integration
and still blocks formal publication.

Formal publication additionally requires individual current human approval for
each included opportunity, including Instagram. A newer source revision or a
verification in the future blocks publication. Ongoing records must have been
verified within 30 days. Registration URLs, when provided, require public HTTPS.

For compatibility with the shared v1 bundle, exported `review_status: approved`
means **accepted for the package**, not necessarily reviewed by a human. The
private D6 draft and human ledger are never rewritten. Read
`dataset_provenance.json` -> `datasets.opportunities.records`, joining by
`opportunity_id`, for the actual `eligibility.decision`: `auto_candidate` or
`human_approved`, with evaluated time, revision, reasons and warnings. Consumers
must not label all packaged records as human-reviewed. The provenance is checked
against packaged IDs and revisions and covered by package checksums.

The builder validates contracts, references, unique IDs, date ordering, versions,
sidecar consistency and checksums. Existing output directories are never overwritten.
D6 and D7 share expiry rules: exact deadlines/end times are compared as instants;
date-only limits last through the Toronto calendar day; ongoing multi-day events
remain eligible until their end (unless an earlier registration deadline expires).
Publication rechecks expiry even if a saved candidate previously passed.

The currently saved D4 package uses the older manifest schema version `1.0.0`,
while the active contract requires `1.1.0`. The explicit
`--upgrade-legacy-manifest` option changes only this manifest version, then
revalidates every market record and package reference before building. It does
not alter market collection timestamps, query configuration, or market data;
unsupported schema versions are rejected.

## Required D7 files

A candidate requires `SHA256SUMS.txt` and all of the following files, each with
one matching SHA-256 entry:

- `bundle.json`, `manifest.json`, `market_jobs.json` and `skills.json`
- `courses.json`, `learning_resources.json`, `organizers.json` and `opportunities.json`
- `dataset_provenance.json`, `reference_statistics.json` and `release_attestation.template.json`

A published copy also includes and covers `release_attestation.json`. The
validator checks the statistics schema and recomputes the counts and raw rates
from the bundled market jobs. With a zero denominator, rates remain JSON
`null`. The legacy D4 input can still use its bundle-only checksum list.

## Integration and formal publication gate

A contract-valid candidate is not necessarily releasable. Publication requires
the formal 2.0 candidate, a verified complete market run, current eligible opportunities, verified organizers,
valid package checksums, and a human attestation tied to the exact dataset digest. Start from
`release_attestation.template.json`, fill in the reviewer and UTC confirmation
time, and set all four confirmations to `true` only after review:

- `role_classifications`: confirm the eight role families and classification
  evidence. The archived sample has no real positive product-analyst example.
- `course_evidence`: confirm course-to-skill evidence from official sources;
  course titles alone are insufficient.
- `opportunity_dates`: check the source posts and confirm that published event
  and deadline dates are still current.
- `sources_and_usage`: inspect the source index and authorization for the
  actual public fields and exact candidate.

```powershell
python -m src.export_bundle validate `
  --package "data\history\d7-candidate-<version>" `
  --for-publication `
  --attestation "data\history\release-attestation-<version>.json"

python -m src.export_bundle publish `
  --package "data\history\d7-candidate-<version>" `
  --output "data\history\d7-published-<version>" `
  --attestation "data\history\release-attestation-<version>.json" `
  --log-file "data\history\d7-publish.log"
```

`publish` validates again and copies to a new directory only after every gate
passes. A malformed, damaged, partial-market, stale-date, or unattested package
is rejected without creating the published output. It does not publish to a
website or modify a database. The intentionally invalid package case is
covered by `tests/test_export_bundle.py`; `contracts/fixtures/invalid_bundle.json`
is also retained as a known-bad contract fixture.

## Scheduled entry point and exit codes

The build command is suitable as the action for a Windows Task Scheduler task.
To print a ready-to-copy offline command:

```powershell
python -m src.export_bundle schedule `
  --market-package "data\history\d4-market-package-<run-id>" `
  --output "data\history\d7-candidate-<version>" `
  --dataset-version "<major.minor.patch>" `
  --upgrade-legacy-manifest
```

Use a unique version and output directory for each run. Scheduling a build does
not schedule market or Instagram collection and does not bypass the manual
publication attestation. `--log-file` writes timestamped build/validation/
publication messages. Exit codes are `0` for success, `1` for a contract or
publication-gate rejection, and `2` for input, filesystem, or other operational
errors; argparse usage errors use its standard exit code `2`.

## Current handoff status

The saved D4 market package at the time D7 was implemented has
`run_status: partial`, and the role review explicitly leaves classification
work outstanding. Therefore it can be used to build and validate an internal
integration candidate, but it cannot pass the publication gate. No production
package should be handed off as verified until the human confirmations above
are completed and a complete market dataset is available. The package builder
does not refresh or otherwise change the underlying market collection.

## Teammate loading convention

Load `bundle.json` for the shared contract, or its matching sidecar files:
`market_jobs.json`, `skills.json`, `courses.json`, `learning_resources.json`,
`organizers.json` and `opportunities.json`. `learning_resources.json` corresponds
to the `resources` key in the bundle. Activities are plain opportunity records,
not D6 `{opportunity, eligibility}` wrappers. Read eligibility from provenance
when displaying review origin. Keep `dataset_provenance.json`, `manifest.json`
and `SHA256SUMS.txt` with the handoff; do not copy private snapshots, media,
OCR caches, credentials or human review ledgers into the package.

An internal candidate can have a partial market run or pending organizer
verification and must be labelled as integration data. `publish` continues to
require a complete market run, organizer verification and release attestation.
Package creation and automated tests do not constitute teammate-side UI testing.

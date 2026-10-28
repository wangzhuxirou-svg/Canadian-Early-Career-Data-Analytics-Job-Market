# D7 data package operations

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

## Publication gate

A contract-valid candidate is not necessarily releasable. Publication requires
a complete reused market dataset, current eligible opportunities, verified organizers, valid package
checksums, and a human attestation tied to the exact dataset version. Start from
`release_attestation.template.json`, fill in the reviewer and UTC confirmation
time, and set all three confirmations to `true` only after review:

- `role_classifications`: confirm the eight role families and classification
  evidence. The archived sample has no real positive product-analyst example.
- `course_evidence`: confirm course-to-skill evidence from official sources;
  course titles alone are insufficient.
- `opportunity_dates`: check the source posts and confirm that published event
  and deadline dates are still current.

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

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

## Instagram single-post trial

The local D6b entry point opens a visible Chromium browser and persists its
login profile only under the Git-ignored `.local/instagram-profile/` directory.
The operator must log in manually if Instagram asks; CAPTCHA, checkpoints, and
access restrictions stop collection without an attempt to bypass them.

Run one post against an organizer already listed in `resources/organizers.json`:

```powershell
.\.venv\Scripts\python.exe -m src.opportunities.collect `
  --url "https://www.instagram.com/p/POST_ID/" `
  --organizer-id ds3_utsc
```

The private, Git-ignored output is written to `data/opportunities/`. Caption
text and exact publication time are read from page metadata and time elements.
When Instagram provides structured post data in the page, caption, publication
timestamp, and every carousel child are extracted from that post-specific
payload. Images are downloaded at the best supplied resolution, decoded, and
hashed; profile, recommendation, and login-prompt images are excluded. The
collector confirms completeness against the embedded carousel child list and
marks the post `partial` if any item is missing or fails validation. If only a
single preview image is available and the page cannot confirm whether it is a
carousel, completeness remains `unknown`.

## Local poster OCR

Run OCR after a post snapshot and its downloaded media are available:

```powershell
.\.venv-ocr\Scripts\python.exe -m src.opportunities.ocr --post-id POST_ID
```

OCR uses EasyOCR's English model on CPU. The first run downloads its model into
the Git-ignored `.local/easyocr-models/`; OCR results and their per-image cache
are written under the Git-ignored `data/opportunities/ocr/`. Each cache key
includes the original image hash, EasyOCR version, language list, and
preprocessing configuration. Images are EXIF-oriented and converted to RGB
for recognition; the saved source image is not modified. Empty, corrupt, and
low-confidence results remain distinguishable. Confidence describes OCR
recognition only and does not establish that extracted event details are
correct. Video-only media is not passed to OCR.

On this Windows ARM64 computer, the main `.venv` uses Python 3.14 ARM64 and
could not resolve a compatible PyTorch distribution. OCR is installed in a
separate `.venv-ocr` using the existing Python 3.10.11 x64 interpreter. The main
pipeline environment is retained. To recreate this CPU environment locally:

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe" -m venv .venv-ocr
.\.venv-ocr\Scripts\python.exe -m pip install --upgrade pip
.\.venv-ocr\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple
.\.venv-ocr\Scripts\python.exe -m pip install -r requirements-ocr.txt
```

Verified on 2026-10-28: EasyOCR 1.7.2, PyTorch 2.14.1+cpu, torchvision
0.29.1+cpu and OpenCV 5.0.0 imported successfully in `.venv-ocr`; `pip check`
reported no broken requirements. English detection and recognition models were
downloaded to `.local/easyocr-models/`. CPU inference correctly read a generated
test image as `UTSC Workshop 2026`, and the project's OCR command accepted
`--help`. This smoke test does not measure accuracy on Instagram posters.
Both `.venv-ocr/` and the model directory are Git-ignored.

## Offline opportunity draft extraction

Generate a conservative local draft from the raw caption and successful OCR
results only:

```powershell
.\.venv\Scripts\python.exe -m src.opportunities extract --post-id POST_ID
```

Drafts are private local files under `data/opportunities/drafts/`. Caption and
per-image OCR evidence remain separate; low-confidence OCR is not used to
generate candidate values. The rules preserve explicit date text and labeled
locations, do not infer a registration URL from “link in bio,” and leave
conflicting dates or locations unset. A post without accessible text that
clearly indicates an opportunity does not generate a draft. Generated
opportunities remain `needs_review` and are not publishable.

## Local opportunity review

Export reviewable drafts to an Excel-friendly UTF-8 CSV and a local-only HTML
preview with downloaded images and escaped caption/OCR evidence:

```powershell
.\.venv\Scripts\python.exe -m src.opportunities review export
```

Edit only `review_status`, reviewer metadata, and the `override__*` columns.
Arrays must be entered as JSON (for example `["workshop"]`); use the literal
`null` to explicitly clear a nullable field, and leave an override cell blank
to retain an earlier override for this revision, or the candidate if no override
exists. Formula-like CSV cells are escaped on export. Import
validates the review-patch contract, current draft ID/source/revision, allowed
fields, organizer and skill references, and the required reviewer, UTC review
time, and reason:

```powershell
.\.venv\Scripts\python.exe -m src.opportunities review import `
  --csv data\opportunities\review\review.csv
```

Each import writes a row-level report. Valid rows are saved in private local
`data/opportunities/review_overrides/` ledgers with their complete review
history; a later review merges overrides without discarding earlier decisions.
An approved or rejected record leaves the export queue for its current content
revision. A new draft revision returns it to review. These internal records
are not public package data; publication still requires a separate validated
bundle build.

## Local opportunity check state and schedule

The private SQLite state lives at `data/opportunities/state.sqlite3`. It
records run status, per-account attempts and successful checks, coverage,
current complete post snapshots, content revisions, and post history. Partial
or failed snapshots are retained in history but do not replace the current
complete snapshot. The database uses SQLite WAL mode; keep it on a persistent
local disk and include it in private backups. Do not commit the database or
move it to an unverified shared runner.

Show the next Toronto-time poll targets and current coverage:

```powershell
.\.venv\Scripts\python.exe -m src.opportunities schedule
.\.venv\Scripts\python.exe -m src.opportunities report
```

The default poll targets are 08:00, 12:00, 16:00, and 20:00
`America/Toronto`; IANA timezone data handles seasonal time changes. These
are target times, not a guarantee of uptime or successful checks. Offline
planning is explicit and makes no network requests or success records:

```powershell
.\.venv\Scripts\python.exe -m src.opportunities run --offline --dry-run `
  --accounts ds3_utsc create_uoft amacss_utsc --limit 3
```

Live account-level checks use one visible browser session with the persisted
local profile. The scanner visits enabled organizer profiles in priority
order, discovers at most 24 post links per account across at most 6 scrolls,
and collects only posts within the default 30-day window that have explicit
registration, recruitment, or volunteer actions, or a named upcoming activity
with a date/future signal. Retrospectives, sponsor/team promotions,
discount-only posts, and posts without an actionable opportunity signal are
skipped. At most one recent image
post with an empty or unavailable caption per account is collected as an OCR
fallback; its poster and all carousel images are OCR-processed locally. Usable
captions can generate drafts even when media collection is partial. Original
collection status is preserved. Semantic event/deadline dates drive expiry;
multiple conflicting dates require review rather than an unrelated past date
silently expiring an upcoming activity.
OCR can be wrong; drafts remain `needs_review`. Unchanged candidates retain
their draft; changed source content or extracted candidates create a new
review revision and archive the previous draft.

Run a live scan explicitly (this opens the visible browser and may require
manual login):

```powershell
.\.venv\Scripts\python.exe -m src.opportunities run `
  --lookback-days 30 `
  --max-posts-per-account 24 `
  --max-scrolls 6 `
  --max-collected-per-account 5
```

`--accounts` and `--limit` can narrow an operator-authorized run. A scan is
reported as partial when its profile boundary, post timestamp, OCR, or configured
scan limits prevent complete coverage. Pinned posts do not cause the scanner to
stop at the first old timestamp. Login challenges, CAPTCHA, checkpoints, and
access restrictions stop the entire run without retries or workarounds. The
scanner never opens comments or recommendations and does not download videos.
Only successfully collected complete snapshots replace current SQLite records;
partial snapshots remain in immutable run history. The SQLite state records
each attempted account and post snapshot.

Offline planning makes no browser or network requests:

```powershell
.\.venv\Scripts\python.exe -m src.opportunities run --offline --dry-run `
  --accounts ds3_utsc create_uoft amacss_utsc --limit 3
```

The orchestration layer records each account attempt separately and allows one
retry for a transient timeout or OS/network error. Authentication and access
block results are not retried, and a block halts later account checks in that
run. The configured scan is deliberately bounded; its result is not a claim
that all historical posts or all opportunities have been collected.

If a process is interrupted while holding the lock, `report` shows the owner.
Only after confirming that process has stopped, manually recover that exact run:

```powershell
.\.venv\Scripts\python.exe -m src.opportunities recover-lock --run-id RUN_ID
```

Recovery marks that run failed and requires the precise lock owner ID; the
program does not silently expire a lock that may belong to a slow live run.

## D6 revision and approval handoff

`draft_versions/POST_ID.json` records the source hash, review revision and
readiness separately from the public opportunity contract. A changed source
invalidates readiness before OCR starts. Media failures preserve usable caption
evidence for review; missing all usable evidence blocks approval. Expired or
filtered posts cannot retain publication eligibility. Previously known drafts are inspected even if their new
caption no longer passes the opportunity relevance prefilter (within the
configured lookback and discovery limits).

Previous draft versions are retained under `draft_history/POST_ID/REVISION.json`.
Review ledgers and their history are preserved. Same-revision patches merge
manual overrides; new revisions require a new decision and do not automatically
carry forward old overrides. The prior decisions remain available in ledger
history for the reviewer. The first refresh of a legacy draft without version
metadata is rebuilt and re-triaged; ambiguous records require a new review.
The scanner's SQLite source revision and
the review revision may differ because corrected OCR can change candidates
without changing source bytes.

Review import validates the final merged record, including earlier overrides,
organizer/skill references, date precision, and start/end ordering. For D7,
use `src.opportunities.review.effective_approved_opportunity(draft)` rather than
copying drafts or review ledgers directly. It returns `None` unless a current
source has an approval for exactly that review revision; otherwise it applies
the manual overrides, validates the result, clears `change_pending`, and sets
`verified_at` to the human review time. This helper serves human approvals only;
use the candidate helper below for automatic routing. D7 still needs
package-wide uniqueness/reference checks and public-data selection.
Legacy drafts without version metadata must first be refreshed with `extract`
and re-triaged before this handoff can return a record.

Live OCR is always invoked with `--offline --json`: missing models do not
trigger downloads. Per-image corrupt/error results fail the affected snapshot;
empty or low-confidence text remains distinguishable and makes account
coverage partial. Reliable caption text may still establish a real activity;
optional media gaps are reported as warnings. Neither low-confidence
text nor empty OCR is treated as established evidence.

## Running a due schedule slot

The `schedule` command previews times only. The explicit live entry point is:

```powershell
.\.venv\Scripts\python.exe -m src.opportunities run --scheduled
```

It claims the most recent due slot on the current Toronto day and checks all
enabled accounts once. A second invocation for that slot is a no-op. It does
not backfill missed older slots. Failed or interrupted slots remain claimed;
inspect the report and lock, then use an explicit manual `run` for a recovery
check. `--scheduled` cannot be narrowed with `--accounts` or `--limit`.
The machine must be awake, logged in and able to show the visible browser.
For Windows Task Scheduler, create four daily triggers at 08:00, 12:00, 16:00
and 20:00 when the computer timezone is Toronto. Use **Run only when user is
logged on** and **Do not start a new instance**. Set:

- Program: the absolute path to `.venv\Scripts\python.exe`.
- Arguments: `-m src.opportunities run --scheduled`.
- Start in: the absolute project directory.

No task is installed or activated by these commands or by this code change.
If the computer uses another timezone, configure equivalent trigger times;
the Python slot calculation always uses Toronto time.

Exit status is `0` for a complete run, dry run or already-claimed/no-due slot,
`1` for partial coverage and `2` for a failed run or invalid arguments. A
`partial` result must not be counted as a successful scheduled check.

`report --json` preserves the last complete success even after a later failed
attempt, counts actual successful account checks in the preceding 24 hours,
and shows seven Toronto calendar days of per-account shortfalls against the
default four-check target. Counts include manual runs; they do not prove four
distinct scheduled slots ran. A seven-day trial and spot-checks of real
activities are still required acceptance evidence. The current day is marked
in progress. No live collection or trial is started by offline tests.

## Automatic candidates and exception review

Same-event consolidation retains posts excluded only for expired start/end or
deadline dates as evidence. An undated explicit speaker call may join its named
event from the same organizer even when classified as an ongoing opportunity;
the merged event is then checked for expiry. Other recruitment types remain
separate. Posts more than 120 days apart are not automatically consolidated,
in addition to the existing year, date and recurring-session guards.

The 2026-10-29 local rebuild groups Women in Tech speaker call `DeCt0uyphlq`
with dated announcement `DeaQaC2jAPE`. The latter supplies `2026-10-17`, so the
combined event is excluded as `expired_start`; neither appears in the current
review queue or publishable candidates. Raw evidence is retained for audit.

Rebuild **existing local snapshots and OCR only**, without a browser or downloads:

```powershell
.\.venv\Scripts\python.exe -m src.opportunities triage
.\.venv\Scripts\python.exe -m src.opportunities review export --output-dir data\opportunities\review\triage-current
```

The private outputs are `triage-report.html` (all decisions with reasons),
`triage-report.json`, individual `triage/POST_ID.json` decisions, and
`publishable-candidates.json` (automatic or human-approved candidates with their eligibility
record). The default review export contains exceptions only; `--include-auto`
also includes automatic candidates for optional spot checks. The CSV includes
`triage_reasons_json` to explain each exception. A record with no accessible
evidence appears in the report as requiring collection repair; it cannot be
approved by editing the old draft alone.

Routing has three outcomes:

- `auto_candidate`: a current source version from a known enabled organizer;
  valid raw/draft contracts and skill references; a caption-backed title and
  clear evidence of a real, actionable activity; no unresolved critical date
  or clock conflicts. Missing dates/locations/direct registration URLs for an
  established event are warnings, not exclusion or automatic review gates.
  The official source post is the entry point for
  “link in bio”; no registration URL is invented. Snapshots older than three
  days need refreshing. Missing video alone does not block a clear caption.
- `needs_review`: unclear event identity, contradictory dates/clocks,
  unresolved relative timing, OCR-only identity, or a pending/outdated human
  decision. Open-ended recruitment/ambassador calls without a known deadline
  remain in this queue rather than being discarded. Optional OCR-only tags
  are omitted from automatic candidate projections.
- `excluded`: explicit retrospective/non-actionable content or a dated event or
  application that has expired. Historical dates are not an expiry heuristic.

Short dates use the nearest unambiguous year within 120 days of publication,
not the current computer year, and retain an inference warning. Date-only deadlines
remain valid through that local date; no exact cutoff time is fabricated.
Applications, “last chance to apply,” abbreviated month names, emoji locations,
address labels and explicit tour/event titles are supported. Deadline lines
are not treated as event dates. Generic discussion of “recruitment” inside an
AI workshop does not classify it as a recruiting opportunity.

Human decisions and override histories are never synthesized or overwritten.
A human hold/rejection cannot be bypassed by auto-triage. Changed source or
candidate content receives a new review revision; unchanged reruns retain
their version and archives.

For D7 call `review.effective_publishable_opportunity(draft)`. It returns
`None` or a wrapper `{opportunity, eligibility}` after checking current source
and time again. `eligibility.decision` is `auto_candidate` or `human_approved`.
Automatic candidates keep the draft's `needs_review` status inside this
private wrapper; that legacy field alone does **not** determine the new routing.
Human-approved records retain `approved`. No shared enum or public schema was
changed. D7 must validate the eligibility wrapper, group membership and package uniqueness, and
make its final publication decision before building the public bundle, whose
existing contract still requires `approved`. Do not concatenate this private
candidate file directly into a public bundle or treat cached decisions as
permanent approvals.

## D6 v2: caption intent, clocks, and one card per activity

The caption decides whether a call is still open. A thank-you to people who
already applied, an event recap, or a closed call is excluded even if an old
carousel poster says “APPLY NOW.” A pure “save the date / more details coming
soon” teaser is excluded; an announcement that actually opens registration is
retained. These same rules apply to both live prefiltering and offline rebuilds.

Explicit caption event dates outrank unrelated pricing windows on carousel
slides. Labeled event dates outrank historical dates. Common line-wrapped OCR
month/day dates and multiline registration-closing labels are supported.
“Today/tomorrow,” “next Friday,” and “N days left to sign up” are anchored to
the original Toronto publication date, not the extraction day. If an explicit
calendar date is present on the same line, it takes precedence over relative
language such as “apply today ... by Oct 22.” Unresolved “next week” remains
a review issue; an old post whose entire relative window has passed is excluded.

Explicit clock ranges (9 AM–5 PM, 09:00–17:00, 3–5 PM, 5:15–8:00 p.m.) and
single labeled start/deadline times are converted with `America/Toronto` zone
rules to UTC. The original lines are retained as evidence and date text. The
report displays local clocks; clients must convert UTC for display. Dates
without clocks remain date-only. Ambiguous DST clocks, uncertain overnight
ranges and explicit unsupported timezones require confirmation. A missing end
time does not manufacture a duration. A known start with unknown end stays
eligible through that calendar day rather than disappearing at its start minute.

`events.py` groups only matching named events/identifiers within one organizer
and opportunity kind, with compatible dates and session numbers. Generic words
like “case competition,” shared months, different organizers, and different
dated sessions are insufficient to merge. Attendee events and speaker calls
are kept separate. A stable canonical post is retained when an older source is
discovered later. `source_post_ids` contains every contributing post; a source
post is never deleted. Additional posts become `merged_duplicate` report
records, not extra activity cards or extra review rows.

The private `event_groups/` view merges complementary facts and records the
source of each selected field. Original per-post drafts remain unchanged.
Conflicting dates/clocks require review; an explicit later deadline update or
multiple later agreeing deadline posts can supersede an older deadline while
preserving it in source history. Groups have independent content revisions and
archives in `event_group_history/`. Any changed member invalidates the old
group; a stale group CSV cannot apply to the new revision. The report and
review preview link to all contributing source posts.

Live runs finalize these local views while holding the run lock. The offline
`triage` command and single-post `extract` command also refresh the consolidated
views; neither visits Instagram. After refresh, export a new review CSV.
Manual approvals appear as `human_approved` and enter the candidate snapshot;
same-revision manual rejections remain excluded. Existing manual decisions
are never replaced by automatic approvals.

## Internal meetings and critical OCR rows

General/annual/yearly meetings and executive, committee, developer or member
meetings without an explicit public invitation are excluded as
`internal_club_meeting`. “Open to all members” is not a public invitation.
Explicit invitations to all students/everyone and public open houses remain
eligible. The rule is shared by collection filtering, extraction and triage,
including cases where the meeting heading is only readable in OCR.

The OCR preprocessing configuration is now
`exif_rgb_critical_fields_deskew_v3`. Its cache key differs from previous
versions; older results remain available. For a weak or malformed deadline
row, the processor first tries a bounded local line recovery: estimate skew,
segment words, separate smaller superscript ordinal components, recognize
the month alphabetically and the day numerically, and recognize the clock
with a clock character set. Each recovered field must reach 0.85 confidence.
The recovered line's confidence is the minimum of its accepted fields.
Clock punctuation such as `1159PM` is normalized to `11:59PM` only when its
hour/minute are valid. An ambiguous `71H` is not blindly rewritten as `7TH`.
The processor must read the digit from image pixels.

If field recovery cannot establish the date, at most three general ROI
contrast/upscale/rotation retries are attempted. Empty or globally weak images
receive one full-image contrast/upscale retry. Source images are never changed.
Unresolved critical rows retain `low_confidence`, even when unrelated logo or
job-title text raises the mean confidence, or retries split the bad row into
high-confidence fragments. Conflicting reliable date readings are preserved
for review. This remains heuristic OCR, not a guarantee of poster accuracy.

Private `data/opportunities/ocr/attempts/CACHE_KEY.json` files retain the
original read, transforms, field readings/confidences, and recovered evidence.
Final evidence coordinates refer to the original image. `BY <date>` alone is
recognized as a deadline, rather than an event start. Run on an existing local
snapshot without model downloads:

```powershell
.\.venv-ocr\Scripts\python.exe -m src.opportunities.ocr --post-id POST_ID --offline --json
.\.venv\Scripts\python.exe -m src.opportunities triage
```

Local validation on 2026-10-28: the EA last-call poster changed from the old
read `BY OCT71HAT1159PM` to `BY OCT 7 AT 11:59PM`. Separate month/day/clock
recognition exceeded 0.97 confidence on that image. With its publication year,
the resulting deadline is `2026-10-07` at `2026-10-08T03:59:00Z` (23:59 Toronto
time), so it is excluded as expired. This is a single-image regression check,
not a measured accuracy claim for the full dataset.

# Shared data contract

This directory is the v1 interface between the Python data pipeline and the
website. The schemas describe exchanged data, not a claim that the existing
pipeline already produces it. Fixtures are synthetic and must never be shown as
real market or club data.

## Version and validation

- Contract version: `1.0.0` (JSON Schema Draft 2020-12).
- Breaking field, meaning, or enum changes require a new major version and
  coordinated changes on both sides. New optional fields may use a minor
  version; corrections that do not change accepted data may use a patch version.
- `manifest.schema_version` identifies this contract. `dataset_version`
  identifies a built data package.
- The package `run_status` may be `complete`, `partial`, or `failed`; failed
  runs are diagnostic only and must not be published.
- Validate the bundle shape with `bundle.schema.json`; implementations must
  additionally check cross-file references, uniqueness, time ordering,
  `sample_count`, and the contents of skill arrays. JSON Schema alone cannot
  validate those relationships.
- Datetimes in the public contract are UTC ISO 8601 strings ending in `Z`.
  Date-only values use `YYYY-MM-DD`; never manufacture a time for them. Preserve
  the source timezone and original date text when available.

## Role and classification decisions

The only counted primary roles are `data_analyst`, `business_analyst`,
`bi_analyst`, `data_scientist`, `data_engineer`, `product_analyst`,
`operations_analyst`, and `financial_risk_analyst`. Financial and risk
subcategories are `financial_analysis` and `risk_analysis` on the eighth role;
they are not additional primary roles. A job must have at most one `role_id`.

Use `role_id: null` and `role_classification_status: "needs_review"` for
cross-class, unclear, or seniority-conflicted jobs. Keep the legacy classifier
outcome in `role_classification_label` (`hybrid`, `adjacent_hybrid`, `other`,
or `outside_scope`) when applicable; these labels are not counted as primary
roles. `outside_scope` is excluded, not a ninth role. Career levels are
`internship`, `new_grad`, and `entry_level`; `unknown` and `potential_senior`
are review states, not counted levels.

## Public package contents

`bundle.schema.json` is a validation envelope for the files emitted separately
by the publisher: manifest, market jobs, skills, courses, resources, and
approved opportunities. Do not put user records, credentials, API keys,
Instagram session state, raw descriptions, downloaded media, or private review
data in that package. Market descriptions are represented only by
`description_status` and `description_analyzable`.

The `opportunity` schema retains the original singular `source_post_id` and
`date_precision` fields for compatibility, while adding `source_post_ids`,
per-date fields and precision, registration and source update fields, revision,
and pending-change state. For a `date` precision, populate the corresponding
`*_date` and leave `*_at` null. For `datetime`, provide both the local calendar
date and the exact UTC instant. For `unknown`, both are null. `timezone` is the
source/event IANA timezone when known.

The legacy `date_precision` is retained for compatibility; filtering and
calendar export must use `start_precision`, `end_precision`, and
`deadline_precision`. Only a fully reviewed opportunity with a confirmed
datetime may be exported to ICS.

## Personal skills and recommendations

`user-record.schema.json` describes private user data for interface agreement;
it is never part of a public bundle. Course and experience matches are
candidates, not confirmed profile skills. A user-confirmed skill records its
source and confirmation time. `experience-skill-extraction.schema.json`
requires evidence and distinguishes affirmed, negated, planned, and ambiguous
mentions. Even an affirmed extraction requires explicit user confirmation.
`contracts/rules/experience-skill-extraction.json` is separate from future
job-description skill extraction rules.

Recommendation order and unknown eligibility/date behavior are recorded in
`contracts/rules/recommendation.json`. Multi-select filters use OR within a
dimension and AND across dimensions. Skill mention rate is unique postings
mentioning a skill divided by postings with analyzable descriptions in the
same filtered sample; report total and analyzable counts, and return null for
a zero denominator.

## Role and opportunity enums

Opportunity `activity_type_ids`: `workshop`, `competition`, `talk`,
`networking`, `social`, `recruitment`, `volunteering`, `research`, `other`.
`topic_ids`: `data_ai`, `software`, `finance`, `business`, `arts_culture`,
`community`, `other`. These are multi-value arrays. `skill_ids` reuse skill IDs
from the shared skill catalog; do not make duplicate opportunity-only labels.
Unknown values are explicit (`unknown`) or null according to the schema.

Only opportunities with `review_status: "approved"` are publishable. A pending
change to a previously approved opportunity must keep `change_pending: true`
until review and must not be used to export calendar reminders.

## Files

- `schemas/`: JSON Schemas for package data and the Instagram ingestion/review
  handoff.
- `fixtures/`: synthetic valid, empty, invalid, and ingestion examples.
- `rules/`: reserved for the agreed skill extraction and recommendation rules;
  job-description extraction and personal-experience extraction are separate
  rule sets.

# D1 role classification spot-check

## Scope and method

Reviewed the archived JSearch inputs locally; no API requests were made. The
audit population is one first-seen record per `job_id` across the seven
versioned files in `data/raw/` (1,165 IDs). For each role with enough records,
the first 10 records in sorted file/row order were checked by title and
description excerpt. For a role with fewer than 10 matches, all matches were
checked. This is a deterministic spot-check, not a random accuracy estimate.

The five pre-existing role families had 10 records each reviewed. The new
families had 0 distinct product-analyst titles, 9 operations-analyst records,
and 9 financial/risk-analyst records. The operations and financial/risk
samples contain repeated title patterns, so their effective diversity is
lower than the record count. No product-analyst true positive exists in the
archived sample; its rule is covered by synthetic tests only and needs a real
sample after collection.

## Reviewed sample and decisions

| Family | Reviewed records | Sample titles / decision |
|---|---:|---|
| `data_analyst` | 10 | Data Analyst Intern; Data Analyst I (Winter 2027, two title variants); Intern, GIS Data Analyst; Intern, HR Data Analyst; Data Analyst (two distinct postings); ERP Data Analyst; TD Asset Management Portfolio Analytics Data Analyst Intern/Co-op; Velocity Data Analyst Internship/Co-op. Explicit analyst titles retained as data analyst. |
| `business_analyst` | 10 | Business Analyst, Data and Change Management; Market Data Business Analyst; IT Business Analyst; Business Analyst (multiple postings). The `Analyst, Product Management` posting describes work in a “Business Analyst team” (requirements, user stories, and functional specifications), so it maps to business analyst rather than product analyst. |
| `bi_analyst` | 10 | Data Reporting Analyst; Analyst, Business Intelligence; Business Intelligence Analyst (several postings); Analyste IB / BI Analyst; Analyste, Intelligence d’Affaires / Business Intelligence Analyst. Reporting and BI work supported the label. |
| `data_scientist` | 10 | Data Scientist I (Winter 2027, two title variants); Data Science Intern/Co-op; Remote Data Science Intern—GIS & Remote Sensing; Velocity Data Scientist Internship/Co-op; AI & Automation Data Scientist Intern; Data Scientist (multiple postings); Data Scientist II. |
| `data_engineer` | 10 | Senior Data Engineer (routed for senior review); Remote Data Engineer, BI & Reporting; Data Engineer Co-op/Intern; Ingénieur Analytique / Analytics Engineer; Data Engineer (multiple postings); Data Engineering Developer I; Ingénieur Data / Data Engineer (French and English variants). |
| `product_analyst` | 0 | No archived title clearly identifies a product analyst. Candidate `Product Data Analyst` is retained as data analyst because the title explicitly names that role; `Remote Data Intern—AI-Driven Product Analytics` lacks an analyst title and remains `other`/review. |
| `operations_analyst` | 9 | Entry-Level Banking Operations Analyst—Mortgage & Loan Operations (1); Entry-Level Operations Analyst: Data Insights (Remote) (8 archived records with substantially repeated title/description). Both role descriptions support operations analysis. |
| `financial_risk_analyst` | 9 | Financial Analyst / Finance Analyst and Financial Reporting Analyst (3 records, `financial_analysis`); Climate Risk Analytics & Reporting, Analyst (4 records, `risk_analysis`); GRM Portfolio Risk Data & AI Analyst Intern (2 records, `risk_analysis`). The portfolio-risk descriptions explicitly analyze credit risk, prepare risk reporting, and support portfolio risk oversight. |

## Confusions found and rule changes

- The generic `data analytics` title token labeled `Product Owner Data &
  Analytics Practice` and `Remote Data Intern—AI-Driven Product Analytics` as
  data analysts despite neither title naming an analyst role. It was narrowed
  to `data analytics analyst`; these titles now go to `other`/review.
- The broad `business intelligence` token mislabeled non-analyst BI roles such
  as developers. It was removed. A financial reporting analyst also matched
  generic `reporting analyst`; a specific exclusion now preserves its
  `financial_analysis` label.
- Climate-risk reporting titles matched generic reporting analyst. A specific
  exclusion plus explicit risk-analytics keywords now labels these
  `risk_analysis`.
- `Data Science Risk Analyst` explicitly matches two core families and is
  `hybrid`/manual review, not forced into one class. Senior core-role titles
  retain their role label but have `role_status: manual_review`.
- `product` and `operations` analyst titles are core roles rather than
  adjacent roles. Marketing and insight analyst titles remain adjacent review
  labels. Investment-banking and credit-analyst titles remain outside scope;
  explicit credit-risk titles match the risk subtype.

Synthetic tests provide at least five positive and three confusable negative
examples per each of the eight families. This spot-check does not establish
classification precision, especially for product analyst, which has no real
positive example in the available archive.

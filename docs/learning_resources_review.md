# D5 learning resource selection and handoff

Reviewed on 2026-10-27, America/Toronto. Resource verification instants use UTC.
The catalog contains 33 entries: 11 campus or university-access entries and
22 external learning entries. Existing resource IDs are retained. The Excel
help portal and Power BI overview now point to specific guided exercises.

## Selection and access

`resources/learning_resources.json` is the maintained source. It uses the
existing resource schema; no schema or job-skill extraction changes are
needed. Entries are listed with university resources first for browsing.
Array order does not implement campus-first recommendation ranking: the
existing recommendation rule still governs sorting.

Public institutional pages were checked online. Quercus, SciNet, FMI,
Bloomberg, Coursera and Kaggle authenticated access was not exercised.
An institutional access statement is not proof that a particular account can
enrol today. Eligibility notes preserve these limits. MDL entries link to the
official roadmap, where students select the named self-paced course.

FMI Foundations is advertised free for UTSC Management and Economics students;
AFM is separate and paid. Coursera access applies only to the university's
selected catalogue. Bloomberg online learning access does not imply campus
terminal access. SciNet Git requires sign-in and may require an academic
access request. Google BigQuery Sandbox is limited; AWS and Azure labs may
incur charges, and Snowflake uses a time-limited trial.

FMI, Bloomberg, the Coursera portal and entrepreneurship retain empty
`skill_ids`: they are direction-exploration or access resources without an
evidenced mapping to the current tool catalog. Do not recommend them as
matches for a tool gap or automatically confirm any user skill. Choosing the
Google Sheets route in the foundational data course likewise does not prove
Excel experience; the Excel mapping is for the Excel route only.

Dated workshops, expired offers, unverified MOS/FactSet access and alumni-only
benefits were not added as evergreen resources. Live events belong in the
opportunity/review workflow, with their dates and availability checked.

## Suggested student deliverables

These are project-team suggestions, not provider assessments, certificates,
or requirements. Completion never automatically confirms proficiency.

| Resource ID | Suggested outcome |
| --- | --- |
| `utsc-bridge-excel-learning-series` | Complete selected practice spreadsheets and explain the formulas used. |
| `utsc-bridge-excel-intermediate` | A workbook with a table, validation rules and one ToolPak analysis; optional macro practice. |
| `utsc-library-foundational-data-literacy` | The official CCR route requires all 10 modules, quizzes and a one-page reflection; verify current CCR rules. |
| `uoft-mdl-r-self-paced` | An R script importing a sample dataset with summaries and notes, subject to actual course access. |
| `uoft-mdl-tableau-self-paced` | A workbook with two views and a short explanation, subject to actual course access. |
| `uoft-scinet-git-self-paced` | A small repository with meaningful commits, a branch and a README. |
| `uoft-scinet-python-recordings` | A short Python program and instructions for running it. |
| `tableau-superstore-guided-analysis` | A Superstore dashboard and three evidence-based observations. |
| `power-bi-desktop-getting-started` | A saved Financial Sample report with a model, measure, visuals and slicer; publishing is optional. |
| `kaggle-intro-sql-exercises` | Completed query exercises plus explanations of a grouping and join. |
| `kaggle-pandas-exercises` | Completed notebook exercises and an explanation of selection and grouping. |
| `excel-help-learning` | Two PivotTables from sample data, with summary findings. |
| `gcp-bigquery-sandbox-practice` | Saved SQL and a short public-data analysis within Sandbox limits. |
| `aws-athena-s3-query-lab` | Table/query SQL, results, and a record of resource cleanup and costs. |
| `azure-sql-database-lab` | Query results and a record of deleting the lab resource group. |
| `snowflake-getting-started-lab` | Loading/query SQL and a note on suspending compute and trial limits. |

The remaining external tutorials and references support the exercises or cover
tools such as NumPy, Matplotlib, R, Spark, TensorFlow and PyTorch. They remain
available for lookup and further practice. Learners should choose resources
appropriate to their prior knowledge, not treat every tool-intro label as
zero-programming-experience suitability.

## Validation and publishing

Run the existing D5 file/schema/catalog tests and package integration check in
`tests/test_contracts.py`. The exporter includes resources and the union of
their referenced skills. These checks validate structure and references, not
actual account access, learning quality or human approval. Rebuild a separate
versioned package when handing off; this catalog edit does not update an
already-generated historical package or publish anything online.

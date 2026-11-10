# Project: BigQuery Migration Agent

Goal: An AI-assisted tool that migrates a legacy PostgreSQL database to BigQuery
and PROVES the migration is correct with a validation report.

Stack: Python 3.11, psycopg2-binary, pandas, pyarrow, google-cloud-bigquery,
google-genai (AI Studio API key, NOT Vertex AI), streamlit, python-dotenv, pydantic, faker.
BigQuery runs in sandbox mode: use load jobs only (no streaming inserts, no DML).
OS: Windows, PowerShell. Use a .venv virtual environment.

Structure:
- seed/               -> creates the messy legacy database
- agent/profiler.py   -> reads source schema + stats
- agent/planner.py    -> Gemini proposes BigQuery schema (JSON) with reasons
- agent/migrator.py   -> moves data per plan (naive mode vs planned mode)
- agent/validator.py  -> compares source vs target, produces report
- agent/translator.py -> Gemini translates SQL queries, parity check
- app.py              -> Streamlit UI with human approval step

Rules:
- Config via .env. Never hardcode secrets.
- Validation logic must be deterministic Python/SQL, never LLM judgment.
- Gemini output must be structured JSON, validated before use.
- Keep functions small and testable. Short docstring per module.
- Work on ONE step at a time. After each step, tell me exactly how to run and test it.

## Progress
- Steps 0–8 done: seed DB (port 5433), profiler, planner (+ --amend-raw, propose/--approve), migrator (Parquet/NDJSON, sandbox mode, approval gate), validator (type fidelity, _raw JSON companion, known-limitation WARNs, keeps query_parity), translator (Gemini SQL translation + deterministic parity, response cache, quota fail-fast), Streamlit app (app.py, Load last results mode).
- Next: Step 9 (README).

## Headline results (agent vs naive; screenshot: docs/compare.png)
- Overall: agent WARN (0 failures · 2 known BigQuery limitations, both detected and explained); naive FAIL (5 failures).
- Tables: agent 3 passed · 2 warned · 0 failed; naive 0 passed · 0 warned · 5 failed.
- Money columns with SUM drift: agent 0 of 5; naive 5 of 5.
- Empty strings lost ('' → NULL): agent 0; naive 4,491.
- Type downgrades: agent 0; naive 14.
- Rows with changed values: agent 0 of 37,020, naive 7,541 of 37,020 (agent: 1,013 more rows differ only by the known limitations).
- Business queries identical: agent 3 of 3; naive 0 of 3.
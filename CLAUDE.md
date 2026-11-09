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
- Steps 0–6 done: seed DB (port 5433), profiler, planner (+ --amend-raw), migrator (Parquet/NDJSON, sandbox mode), validator (type fidelity, _raw JSON companion, known-limitation WARNs).
- Agent plan: 0 FAIL (2 explained WARNs). Naive plan: 5/5 FAIL.
- Next: Step 7 (query translation), Step 8 (Streamlit), Step 9 (README).
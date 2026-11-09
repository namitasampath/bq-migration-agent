"""Turn profile.json into BigQuery migration plans.

Agent plan (plan.json): Gemini (google-genai, AI Studio key) proposes a schema as
structured JSON. The response is validated with pydantic, then deterministically
against the profile and the migration rules; on failure Gemini gets one retry with
the error list. 429/5xx responses are retried with exponential backoff.

Naive plan (naive_plan.json): a rule-based careless mapping with no AI
(numeric -> FLOAT64, timestamps/json/arrays -> STRING, '' -> NULL).

Run from the project root:  python -m agent.planner
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError, field_validator

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "profile.json"
PLAN_PATH = ROOT / "plan.json"
NAIVE_PLAN_PATH = ROOT / "naive_plan.json"

Transform = Literal["none", "empty_string_to_null", "keep_as_is", "timestamp_to_datetime",
                    "timestamp_to_timestamp_utc", "json_to_json", "array_to_array"]

MAX_CLUSTER_COLUMNS = 4
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
BACKOFF_SECONDS = [2, 4, 8, 16, 32]
SOURCE_TIMEZONE = "Asia/Kolkata"

BQ_SCALAR = r"(STRING|BYTES|INT64|FLOAT64|NUMERIC|BIGNUMERIC|BOOL|DATE|DATETIME|TIMESTAMP|TIME|JSON)"
BQ_TYPE_RE = re.compile(rf"^(ARRAY<{BQ_SCALAR}>|{BQ_SCALAR}(\(\d+(,\s*\d+)?\))?)$")
PARTITIONABLE = ("DATE", "DATETIME", "TIMESTAMP", "INT64")
TEXT_UDTS = {"text", "varchar", "bpchar", "citext", "name"}
TIMESTAMP_UDTS = {"timestamp", "timestamptz"}
INT_UDTS = {"int2", "int4", "int8"}


# ---------------------------------------------------------------- plan models

class ColumnPlan(BaseModel):
    """Mapping decision for one source column."""
    source_column: str
    source_type: str
    bq_type: str
    transform: Transform
    reason: str


class TablePlan(BaseModel):
    """Mapping decisions for one source table."""
    source_table: str
    target_table: str
    columns: list[ColumnPlan]
    partition_column: Optional[str]
    partition_reason: str
    clustering_columns: list[str]
    clustering_reason: str

    @field_validator("clustering_columns")
    @classmethod
    def at_most_four(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_CLUSTER_COLUMNS:
            raise ValueError(f"at most {MAX_CLUSTER_COLUMNS} clustering columns, got {len(v)}")
        return v


class Plan(BaseModel):
    """A full migration plan: one entry per source table."""
    tables: list[TablePlan]


# ---------------------------------------------------------------- profile helpers

def load_profile(path: Path = PROFILE_PATH) -> dict:
    """Load profile.json written by agent.profiler."""
    if not path.exists():
        sys.exit(f"{path} not found. Run: python -m agent.profiler")
    return json.loads(path.read_text(encoding="utf-8"))


def compact_profile(profile: dict) -> dict:
    """Shrink the profile to what the planner needs (types, keys, key stats)."""
    tables = []
    for t in profile["tables"]:
        cols = []
        for c in t["columns"]:
            stats = {k: v for k, v in c["stats"].items() if k != "has_nonzero_microseconds"}
            cols.append({"name": c["name"], "type": c["type"],
                         "nullable": c["nullable"], **stats})
        tables.append({"name": t["name"], "row_count": t["row_count"],
                       "primary_key": t["primary_key"],
                       "foreign_keys": [f"{','.join(f['columns'])} -> "
                                        f"{f['ref_table']}({','.join(f['ref_columns'])})"
                                        for f in t["foreign_keys"]],
                       "columns": cols})
    return {"tables": tables}


def column_index(profile: dict) -> dict[str, dict[str, dict]]:
    """Map table -> column -> column profile."""
    return {t["name"]: {c["name"]: c for c in t["columns"]} for t in profile["tables"]}


# ---------------------------------------------------------------- deterministic validation

def check_column_rules(table: str, cp: ColumnPlan, src: dict) -> list[str]:
    """Check one column decision against the migration rules; return errors."""
    where = f"{table}.{cp.source_column}"
    udt, bq, tr = src["udt"], cp.bq_type.upper().replace(" ", ""), cp.transform
    errors = []
    if not BQ_TYPE_RE.match(bq):
        errors.append(f"{where}: '{cp.bq_type}' is not a valid BigQuery type")
    if cp.source_type != src["type"]:
        errors.append(f"{where}: source_type '{cp.source_type}' != profiled '{src['type']}'")
    if udt == "numeric" and not re.match(r"^(BIG)?NUMERIC\b", bq):
        errors.append(f"{where}: numeric money must be NUMERIC or BIGNUMERIC, got {cp.bq_type}")
    if udt in TIMESTAMP_UDTS:
        if tr == "timestamp_to_datetime" and bq != "DATETIME":
            errors.append(f"{where}: timestamp_to_datetime requires DATETIME")
        elif tr == "timestamp_to_timestamp_utc":
            if bq != "TIMESTAMP":
                errors.append(f"{where}: timestamp_to_timestamp_utc requires TIMESTAMP")
            if SOURCE_TIMEZONE not in cp.reason:
                errors.append(f"{where}: TIMESTAMP mapping must state the {SOURCE_TIMEZONE} assumption")
        elif tr not in ("timestamp_to_datetime", "timestamp_to_timestamp_utc"):
            errors.append(f"{where}: timestamp needs timestamp_to_datetime or "
                          f"timestamp_to_timestamp_utc, got {tr}")
    if udt in ("json", "jsonb") and (bq != "JSON" or tr != "json_to_json"):
        errors.append(f"{where}: jsonb must map to JSON with json_to_json")
    if src["is_array"] and (not bq.startswith("ARRAY<") or tr != "array_to_array"):
        errors.append(f"{where}: arrays must map to ARRAY<...> with array_to_array")
    if tr == "empty_string_to_null" and udt not in TEXT_UDTS:
        errors.append(f"{where}: empty_string_to_null only applies to text columns")
    if udt in INT_UDTS and bq not in ("INT64", "NUMERIC", "BIGNUMERIC"):
        errors.append(f"{where}: integer must map to INT64, got {cp.bq_type}")
    return errors


def check_table_plan(tp: TablePlan, src_cols: dict[str, dict], strict_rules: bool) -> list[str]:
    """Check coverage, partition/cluster columns and (optionally) the type rules."""
    errors = []
    names = [c.source_column for c in tp.columns]
    for name in sorted(set(names)):
        if names.count(name) > 1:
            errors.append(f"{tp.source_table}.{name}: appears {names.count(name)} times")
    for name in src_cols.keys() - set(names):
        errors.append(f"{tp.source_table}.{name}: missing from plan")
    for name in set(names) - src_cols.keys():
        errors.append(f"{tp.source_table}.{name}: not a source column")
    planned = {c.source_column: c for c in tp.columns}
    if tp.partition_column is not None:
        cp = planned.get(tp.partition_column)
        if cp is None:
            errors.append(f"{tp.source_table}: partition column '{tp.partition_column}' does not exist")
        elif not cp.bq_type.upper().startswith(PARTITIONABLE):
            errors.append(f"{tp.source_table}: partition column '{tp.partition_column}' "
                          f"has non-partitionable type {cp.bq_type}")
    for name in tp.clustering_columns:
        if name not in planned:
            errors.append(f"{tp.source_table}: clustering column '{name}' does not exist")
    if strict_rules:
        for cp in tp.columns:
            if cp.source_column in src_cols:
                errors.extend(check_column_rules(tp.source_table, cp, src_cols[cp.source_column]))
    return errors


def validate_plan(plan: Plan, profile: dict, strict_rules: bool = True) -> list[str]:
    """Validate a plan against the profile; return a list of human-readable errors."""
    index = column_index(profile)
    errors = []
    tables = [t.source_table for t in plan.tables]
    for name in index.keys() - set(tables):
        errors.append(f"table {name}: missing from plan")
    for name in sorted(set(tables)):
        if tables.count(name) > 1:
            errors.append(f"table {name}: appears {tables.count(name)} times")
    targets = [t.target_table for t in plan.tables]
    if len(set(targets)) != len(targets):
        errors.append("target_table names must be unique")
    for tp in plan.tables:
        if tp.source_table not in index:
            errors.append(f"table {tp.source_table}: not a source table")
            continue
        errors.extend(check_table_plan(tp, index[tp.source_table], strict_rules))
    return errors


# ---------------------------------------------------------------- Gemini

SYSTEM_PROMPT = f"""You are a senior data engineer planning a PostgreSQL -> BigQuery migration
for an Indian NBFC lending database. You receive a profile of the source schema with
column types and statistics. Return a JSON plan covering EVERY table and EVERY column,
each column exactly once, using the exact source column names and source types given.

Rules (mandatory):
1. Money / numeric(p,s) columns must be NUMERIC or BIGNUMERIC, never FLOAT64. Choose a
   type whose precision and scale hold the profiled max value and decimal places used.
2. "timestamp without time zone" must preserve microseconds. Map it explicitly either to
   DATETIME (transform timestamp_to_datetime) or to TIMESTAMP (transform
   timestamp_to_timestamp_utc) — if you choose TIMESTAMP, the reason must state the
   assumption that source values are {SOURCE_TIMEZONE} local time.
3. jsonb -> JSON (transform json_to_json).
4. text[] -> ARRAY<STRING> (transform array_to_array).
5. Do NOT silently convert empty strings to NULL. For text columns that contain empty
   strings use keep_as_is unless converting is clearly justified by the data; say why.
6. Integers -> INT64. Dates -> DATE. Other columns needing no change use transform none.
7. Per table: choose a partition column (DATE/DATETIME/TIMESTAMP/INT64, or null for small
   tables where partitioning does not help) and up to {MAX_CLUSTER_COLUMNS} clustering
   columns that match likely filters and joins. Give a reason for each choice.
8. Every reason is one concise sentence grounded in the profile statistics.
Target table names: lowercase snake_case, usually the same as the source table."""


def make_client():
    """Create a google-genai client from GEMINI_API_KEY in .env."""
    from google import genai
    load_dotenv(ROOT / ".env")
    if not os.getenv("GEMINI_API_KEY") or not os.getenv("GEMINI_MODEL"):
        sys.exit("Missing GEMINI_API_KEY or GEMINI_MODEL in .env")
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def call_gemini(client, model: str, contents: list) -> str:
    """Call Gemini in JSON mode with the Plan schema; retry 429/5xx with backoff."""
    from google.genai import errors, types
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT, temperature=0,
        response_mime_type="application/json", response_schema=Plan)
    for attempt, delay in enumerate([0, *BACKOFF_SECONDS]):
        if delay:
            print(f"  rate-limited/unavailable, retrying in {delay}s ...")
            time.sleep(delay)
        try:
            response = client.models.generate_content(model=model, contents=contents,
                                                      config=config)
            return response.text or ""
        except errors.APIError as e:
            if e.code not in RETRY_STATUS_CODES or attempt == len(BACKOFF_SECONDS):
                raise
    raise RuntimeError("unreachable")


def parse_plan(text: str, profile: dict) -> tuple[Optional[Plan], list[str]]:
    """Parse and validate Gemini's JSON; return (plan or None, errors)."""
    try:
        plan = Plan.model_validate_json(text)
    except ValidationError as e:
        return None, [f"schema: {'.'.join(map(str, err['loc']))}: {err['msg']}"
                      for err in e.errors()]
    return plan, validate_plan(plan, profile)


def user_message(text: str):
    from google.genai import types
    return types.Content(role="user", parts=[types.Part(text=text)])


def model_message(text: str):
    from google.genai import types
    return types.Content(role="model", parts=[types.Part(text=text)])


def build_agent_plan(profile: dict, client=None, model: Optional[str] = None) -> Plan:
    """Ask Gemini for a plan; validate it and retry once with the errors."""
    client = client or make_client()
    model = model or os.environ["GEMINI_MODEL"]
    prompt = ("Source profile (JSON):\n"
              + json.dumps(compact_profile(profile), ensure_ascii=False, separators=(",", ":")))
    contents = [user_message(prompt)]
    text = call_gemini(client, model, contents)
    plan, errors = parse_plan(text, profile)
    if not errors:
        return plan
    print(f"  Gemini plan failed validation ({len(errors)} errors); retrying once ...")
    for err in errors:
        print(f"    - {err}")
    contents += [model_message(text), user_message(
        "Your plan failed validation with these errors:\n- " + "\n- ".join(errors)
        + "\nReturn the complete corrected plan (all tables, all columns).")]
    text = call_gemini(client, model, contents)
    plan, errors = parse_plan(text, profile)
    if errors:
        raise ValueError("Gemini plan still invalid after retry:\n- " + "\n- ".join(errors))
    return plan


# ---------------------------------------------------------------- naive plan

def naive_column(col: dict) -> ColumnPlan:
    """Careless rule-based mapping for one column."""
    udt = col["udt"]
    if col["is_array"] or udt in ("json", "jsonb") or udt in TIMESTAMP_UDTS:
        bq, tr = "STRING", "none"
    elif udt in ("numeric", "float4", "float8", "money"):
        bq, tr = "FLOAT64", "none"
    elif udt in INT_UDTS:
        bq, tr = "INT64", "none"
    elif udt == "date":
        bq, tr = "DATE", "none"
    elif udt == "bool":
        bq, tr = "BOOL", "none"
    elif udt in TEXT_UDTS:
        bq, tr = "STRING", "empty_string_to_null"
    else:
        bq, tr = "STRING", "none"
    return ColumnPlan(source_column=col["name"], source_type=col["type"],
                      bq_type=bq, transform=tr, reason="naive default")


def build_naive_plan(profile: dict) -> Plan:
    """Deterministic careless plan: no partitioning, no clustering."""
    return Plan(tables=[TablePlan(
        source_table=t["name"], target_table=t["name"],
        columns=[naive_column(c) for c in t["columns"]],
        partition_column=None, partition_reason="naive default",
        clustering_columns=[], clustering_reason="naive default",
    ) for t in profile["tables"]])


# ---------------------------------------------------------------- output

def save_plan(plan: Plan, path: Path, mode: str, model: Optional[str]) -> None:
    """Write a plan with metadata; both plan files share this structure."""
    doc = {"mode": mode, "model": model,
           "generated_at": datetime.now().replace(microsecond=0).isoformat(),
           **plan.model_dump()}
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def load_plan(path: Path) -> Plan:
    """Load a saved plan file back into a validated Plan."""
    return Plan.model_validate(json.loads(path.read_text(encoding="utf-8")))


def format_plan(plan: Plan) -> str:
    """Readable table of decisions and reasons."""
    lines = []
    for tp in plan.tables:
        lines.append(f"\n{tp.source_table} -> {tp.target_table}")
        lines.append(f"  partition: {tp.partition_column or '-'}  ({tp.partition_reason})")
        lines.append(f"  cluster:   {', '.join(tp.clustering_columns) or '-'}  "
                     f"({tp.clustering_reason})")
        lines.append(f"  {'column':<18} {'source type':<28} {'bigquery':<16} "
                     f"{'transform':<26} reason")
        for c in tp.columns:
            lines.append(f"  {c.source_column:<18} {c.source_type:<28} {c.bq_type:<16} "
                         f"{c.transform:<26} {c.reason}")
    return "\n".join(lines)


def main() -> None:
    profile = load_profile()

    naive = build_naive_plan(profile)
    naive_errors = validate_plan(naive, profile, strict_rules=False)
    if naive_errors:
        sys.exit("Naive plan is structurally invalid:\n- " + "\n- ".join(naive_errors))
    save_plan(naive, NAIVE_PLAN_PATH, mode="naive", model=None)
    print(f"Saved naive plan to {NAIVE_PLAN_PATH}")

    client = make_client()
    model = os.environ["GEMINI_MODEL"]
    print(f"Asking {model} for the agent plan ...")
    plan = build_agent_plan(profile, client, model)
    save_plan(plan, PLAN_PATH, mode="agent", model=model)
    print(format_plan(plan))
    print(f"\nSaved agent plan to {PLAN_PATH}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

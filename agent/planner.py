"""Turn profile.json into BigQuery migration plans.

Agent plan (plan.json): Gemini (google-genai, AI Studio key) proposes a schema as
structured JSON. The response is validated with pydantic, then deterministically
against the profile and the migration rules; on failure Gemini gets one retry with
the error list. 429/5xx responses are retried with exponential backoff.

Naive plan (naive_plan.json): a rule-based careless mapping with no AI
(numeric -> FLOAT64, timestamps/json/arrays -> STRING, '' -> NULL).

Run from the project root:  python -m agent.planner
Add jsonb _raw companions to an existing plan.json (no Gemini):  python -m agent.planner --amend-raw
"""
import hashlib
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
MAX_RETRY_WAIT_SECONDS = 60  # a 429 asking us to wait longer than this fails fast
CACHE_DIR = ROOT / ".cache" / "gemini"
SOURCE_TIMEZONE = "Asia/Kolkata"
RAW_SUFFIX = "_raw"

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
    raw_companion: Optional[str] = None  # extra STRING column with the exact source text


class TargetColumn(BaseModel):
    """One physical BigQuery column: a planned column or a _raw companion."""
    name: str
    source_column: str
    source_type: str
    bq_type: str
    transform: Transform
    is_raw_companion: bool = False


def raw_companion_name(column: str) -> str:
    return f"{column}{RAW_SUFFIX}"


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


def target_columns(tp: TablePlan) -> list[TargetColumn]:
    """Physical target columns in order; a _raw companion follows its JSON column."""
    out = []
    for c in tp.columns:
        out.append(TargetColumn(name=c.source_column, source_column=c.source_column,
                                source_type=c.source_type, bq_type=c.bq_type,
                                transform=c.transform))
        if c.raw_companion:
            out.append(TargetColumn(name=c.raw_companion, source_column=c.source_column,
                                    source_type=c.source_type, bq_type="STRING",
                                    transform="keep_as_is", is_raw_companion=True))
    return out


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
    if udt in ("json", "jsonb"):
        if bq != "JSON" or tr != "json_to_json":
            errors.append(f"{where}: jsonb must map to JSON with json_to_json")
        if cp.raw_companion != raw_companion_name(cp.source_column):
            errors.append(f"{where}: jsonb needs raw_companion "
                          f"'{raw_companion_name(cp.source_column)}' (BigQuery JSON stores "
                          f"numbers as FLOAT64)")
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
    for cp in tp.columns:
        if cp.raw_companion is None:
            continue
        src = src_cols.get(cp.source_column)
        if src is not None and src["udt"] not in ("json", "jsonb"):
            errors.append(f"{tp.source_table}.{cp.source_column}: raw_companion is only "
                          f"allowed on json/jsonb columns")
        if cp.raw_companion in src_cols:
            errors.append(f"{tp.source_table}.{cp.source_column}: raw_companion "
                          f"'{cp.raw_companion}' collides with a source column")
    companions = [c.raw_companion for c in tp.columns if c.raw_companion]
    if len(set(companions)) != len(companions):
        errors.append(f"{tp.source_table}: duplicate raw_companion names")
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
3. jsonb -> JSON (transform json_to_json) AND set raw_companion to "<column>_raw".
   BigQuery JSON stores numbers as FLOAT64, so the companion STRING column keeps the
   exact original text. For every non-json column raw_companion must be null.
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


class GeminiQuotaExhausted(RuntimeError):
    """Daily (or long-window) Gemini quota is used up; retrying now is pointless."""


def quota_retry_delay(e) -> Optional[float]:
    """Seconds the API asks us to wait (RetryInfo.retryDelay), if present."""
    m = re.search(r"retryDelay['\"]?\s*:\s*['\"](\d+(?:\.\d+)?)s", str(e))
    return float(m.group(1)) if m else None


def is_quota_exhausted(e) -> bool:
    """429 caused by a per-day quota, or one that asks for a wait we won't do."""
    if getattr(e, "code", None) != 429:
        return False
    delay = quota_retry_delay(e)
    return "PerDay" in str(e) or (delay is not None and delay > MAX_RETRY_WAIT_SECONDS)


# ---------------------------------------------------------------- response cache

def cache_enabled() -> bool:
    load_dotenv(ROOT / ".env")
    return os.getenv("GEMINI_CACHE", "true").strip().lower() == "true"


def serialize_contents(contents: list) -> list:
    """JSON-safe form of str / types.Content / dict contents (for hashing)."""
    out = []
    for c in contents:
        if hasattr(c, "model_dump"):
            out.append(c.model_dump(mode="json", exclude_none=True))
        else:
            out.append(c)
    return out


def cache_key(model: str, system_prompt: str, contents: list, schema: type[BaseModel]) -> str:
    """sha256 of (model, system prompt, contents, schema name)."""
    payload = json.dumps({"model": model, "system_prompt": system_prompt,
                          "contents": serialize_contents(contents),
                          "schema": schema.__name__},
                         sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_read(key: str) -> Optional[str]:
    """Saved response text for a key, or None (missing or unreadable)."""
    path = CACHE_DIR / f"{key}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["text"]
    except (OSError, ValueError, KeyError):
        return None


def cache_write(key: str, text: str, model: str, schema: type[BaseModel]) -> None:
    """Save a response; written to a temp file first so a crash never leaves half a file."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"model": model, "schema": schema.__name__,
                               "created_at": datetime.now().replace(microsecond=0).isoformat(),
                               "text": text}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- Gemini call

def call_gemini(client, model: str, contents: list,
                system_prompt: str = SYSTEM_PROMPT, schema: type[BaseModel] = Plan) -> str:
    """Call Gemini in JSON mode with a response schema, via the response cache.

    Cache: .cache/gemini/<sha256>.json, keyed on (model, system prompt, contents, schema
    name); disabled with GEMINI_CACHE=false. Thinking is set to GEMINI_THINKING_LEVEL
    (default: minimal). 429/5xx are retried with backoff, except an exhausted daily
    quota, which raises GeminiQuotaExhausted immediately.
    """
    use_cache = cache_enabled()
    key = cache_key(model, system_prompt, contents, schema)
    if use_cache and (cached := cache_read(key)) is not None:
        print("  gemini call: cached", flush=True)
        return cached
    text = generate_with_retries(client, model, contents, system_prompt, schema)
    if use_cache:
        cache_write(key, text, model, schema)
    return text


def generate_with_retries(client, model: str, contents: list, system_prompt: str,
                          schema: type[BaseModel]) -> str:
    """The live API call: thinking level, timing print, backoff, fail-fast on quota."""
    from google.genai import errors, types
    level = os.getenv("GEMINI_THINKING_LEVEL", "minimal").strip().upper()
    thinking = (types.ThinkingConfig(thinking_level=level)
                if level and level != "DEFAULT" else None)
    for attempt, delay in enumerate([0, *BACKOFF_SECONDS]):
        if delay:
            print(f"  rate-limited/unavailable, retrying in {delay}s ...", flush=True)
            time.sleep(delay)
        config = types.GenerateContentConfig(
            system_instruction=system_prompt, temperature=0, thinking_config=thinking,
            response_mime_type="application/json", response_schema=schema)
        started = time.perf_counter()
        try:
            response = client.models.generate_content(model=model, contents=contents,
                                                      config=config)
        except errors.APIError as e:
            if is_quota_exhausted(e):
                delay_s = quota_retry_delay(e)
                wait = f" (API says retry in {delay_s / 60:.0f} min)" if delay_s else ""
                raise GeminiQuotaExhausted(f"Gemini quota exhausted for {model}{wait}") from e
            if thinking is not None and e.code == 400 and "think" in str(e).lower():
                print(f"  model rejected thinking_level={level}; using default thinking. "
                      f"API said: {getattr(e, 'message', None) or str(e)[:300]}", flush=True)
                thinking = None
                continue
            if e.code not in RETRY_STATUS_CODES or attempt == len(BACKOFF_SECONDS):
                raise
            continue
        usage = response.usage_metadata
        thoughts = getattr(usage, "thoughts_token_count", None) if usage else None
        output = getattr(usage, "candidates_token_count", None) if usage else None
        print(f"  gemini call: {time.perf_counter() - started:.1f}s "
              f"(thinking={level if thinking is not None else 'default'}, "
              f"thought tokens={thoughts}, "
              f"output tokens={output})", flush=True)
        return response.text or ""
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
            if c.raw_companion:
                lines.append(f"  {'+ ' + c.raw_companion:<18} {c.source_type:<28} "
                             f"{'STRING':<16} {'keep_as_is':<26} exact original JSON text")
    return "\n".join(lines)


# ---------------------------------------------------------------- deterministic amendments

def add_raw_companions(plan: Plan, profile: dict) -> list[str]:
    """Set raw_companion on every json/jsonb column; return what changed."""
    index = column_index(profile)
    changed = []
    for tp in plan.tables:
        for cp in tp.columns:
            src = index.get(tp.source_table, {}).get(cp.source_column)
            if src and src["udt"] in ("json", "jsonb") and not cp.raw_companion:
                cp.raw_companion = raw_companion_name(cp.source_column)
                changed.append(f"{tp.source_table}.{cp.source_column} -> +{cp.raw_companion}")
    return changed


def amend_saved_plan(path: Path, profile: dict) -> list[str]:
    """Add raw companions to a saved agent plan without calling Gemini."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    plan = Plan.model_validate(doc)
    changed = add_raw_companions(plan, profile)
    errors = validate_plan(plan, profile)
    if errors:
        sys.exit("Amended plan is invalid:\n- " + "\n- ".join(errors))
    if changed:
        doc["tables"] = plan.model_dump()["tables"]
        doc.setdefault("amendments", []).append({
            "at": datetime.now().replace(microsecond=0).isoformat(),
            "rule": "jsonb raw companion (deterministic, no Gemini call)",
            "changes": changed})
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed


def main() -> None:
    profile = load_profile()
    if "--amend-raw" in sys.argv:
        changed = amend_saved_plan(PLAN_PATH, profile)
        print("Amended plan.json:" if changed else "plan.json already has raw companions.")
        for c in changed:
            print(f"  {c}")
        return

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

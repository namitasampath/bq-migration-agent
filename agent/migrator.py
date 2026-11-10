"""Move Postgres data to BigQuery according to a saved plan (never calls Gemini).

  python -m agent.migrator --plan agent   -> plan.json        -> tables agent_<name>
  python -m agent.migrator --plan naive   -> naive_plan.json  -> tables naive_<name>
  optional: --tables loans payments

Each table is streamed out of Postgres with a server-side cursor (read-only, never
port 5432), converted per the plan, written to tmp/<target>.parquet with an explicit
pyarrow schema, then loaded with a BigQuery load job using an explicit schema. BigQuery cannot load
Parquet into JSON columns, so tables with a JSON column also get tmp/<target>.ndjson
(same converted values) and load from that instead. A jsonb column with a raw_companion
is loaded twice: as JSON and as <col>_raw STRING holding the exact Postgres text.
No streaming inserts, no DML. BQ_SANDBOX=true defers partitioning (sandbox
partitions expire after 60 days). Results are recorded in migration_log.json.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv
from psycopg2 import sql
from psycopg2.extras import register_default_json, register_default_jsonb

from agent.planner import (NAIVE_PLAN_PATH, PLAN_PATH, Plan, TablePlan, TargetColumn,
                           approval_info, load_plan, target_columns)
from agent.profiler import connect

ROOT = Path(__file__).resolve().parents[1]
TMP_DIR = ROOT / "tmp"
LOG_PATH = ROOT / "migration_log.json"
CHUNK_ROWS = 5000
IST = timezone(timedelta(hours=5, minutes=30))  # Asia/Kolkata has no DST
PLANS = {"agent": PLAN_PATH, "naive": NAIVE_PLAN_PATH}
DEFAULT_NUMERIC = (38, 9)
DEFAULT_BIGNUMERIC = (76, 38)

Converter = Callable[[object], object]


# ---------------------------------------------------------------- settings

def read_bq_settings() -> dict:
    """Read GCP_PROJECT, BQ_DATASET and BQ_SANDBOX from .env."""
    load_dotenv(ROOT / ".env")
    missing = [k for k in ("GCP_PROJECT", "BQ_DATASET") if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing settings in .env: {', '.join(missing)}")
    return {"project": os.environ["GCP_PROJECT"], "dataset": os.environ["BQ_DATASET"],
            "sandbox": os.getenv("BQ_SANDBOX", "false").strip().lower() == "true"}


# ---------------------------------------------------------------- type mapping

def parse_bq_type(bq_type: str) -> tuple[str, bool, tuple[int, int] | None]:
    """Split 'ARRAY<STRING>' / 'NUMERIC(38,9)' into (base, is_array, (precision, scale))."""
    t = bq_type.upper().replace(" ", "")
    is_array = t.startswith("ARRAY<") and t.endswith(">")
    if is_array:
        t = t[6:-1]
    m = re.match(r"^([A-Z0-9]+)(?:\((\d+)(?:,(\d+))?\))?$", t)
    if not m:
        raise ValueError(f"Unsupported BigQuery type: {bq_type}")
    params = (int(m[2]), int(m[3] or 0)) if m[2] else None
    return m[1], is_array, params


def arrow_scalar(base: str, params: tuple[int, int] | None) -> pa.DataType:
    """Arrow type for one BigQuery scalar type."""
    if base == "NUMERIC":
        return pa.decimal128(*(params or DEFAULT_NUMERIC))
    if base == "BIGNUMERIC":
        return pa.decimal256(*(params or DEFAULT_BIGNUMERIC))
    simple = {"STRING": pa.string(), "JSON": pa.string(), "BYTES": pa.binary(),
              "INT64": pa.int64(), "FLOAT64": pa.float64(), "BOOL": pa.bool_(),
              "DATE": pa.date32(), "DATETIME": pa.timestamp("us"),
              "TIMESTAMP": pa.timestamp("us", tz="UTC"), "TIME": pa.time64("us")}
    if base not in simple:
        raise ValueError(f"Unsupported BigQuery type: {base}")
    return simple[base]


def arrow_field(col: TargetColumn) -> pa.Field:
    """Arrow field matching the planned BigQuery column."""
    base, is_array, params = parse_bq_type(col.bq_type)
    t = arrow_scalar(base, params)
    if is_array:
        return pa.field(col.name, pa.list_(pa.field("element", t)), nullable=False)
    return pa.field(col.name, t)


def arrow_schema(tp: TablePlan) -> pa.Schema:
    return pa.schema([arrow_field(c) for c in target_columns(tp)])


def bq_field(col: TargetColumn):
    """BigQuery SchemaField matching the planned column."""
    from google.cloud import bigquery
    base, is_array, params = parse_bq_type(col.bq_type)
    kwargs = {}
    if params and base in ("NUMERIC", "BIGNUMERIC"):
        kwargs = {"precision": params[0], "scale": params[1]}
    return bigquery.SchemaField(col.name, base,
                                mode="REPEATED" if is_array else "NULLABLE", **kwargs)


# ---------------------------------------------------------------- value conversion

def is_array_source(src_type: str) -> bool:
    return src_type.endswith("[]")


def to_decimal(v):
    """Exact Decimal; refuse floats so money never passes through binary floating point."""
    if isinstance(v, float):
        raise TypeError(f"float {v!r} reached a NUMERIC column")
    return v if isinstance(v, Decimal) else Decimal(v)


def to_naive_string(v, src_type: str) -> str:
    """Careless stringification used by the naive plan."""
    if is_array_source(src_type):
        return ",".join("" if x is None else str(x) for x in v)
    return v if isinstance(v, str) else str(v)


def ist_to_utc(v: datetime) -> datetime:
    """Interpret a naive timestamp as Asia/Kolkata local time and convert to UTC."""
    return v.replace(tzinfo=IST).astimezone(timezone.utc)


def base_converter(col: TargetColumn) -> Converter:
    """Converter for a non-NULL value, chosen from bq_type, source type and transform."""
    base, is_array, _ = parse_bq_type(col.bq_type)
    src = col.source_type
    if is_array:
        return list
    if col.transform == "timestamp_to_timestamp_utc":
        return ist_to_utc
    if base in ("NUMERIC", "BIGNUMERIC"):
        return to_decimal
    if base == "FLOAT64":
        return float
    if base == "STRING":
        return lambda v: to_naive_string(v, src)
    return lambda v: v  # INT64, DATE, DATETIME, JSON (raw text), BOOL, ...


def make_converter(col: TargetColumn) -> Converter:
    """Full converter for a column, including NULL and empty-string handling."""
    convert = base_converter(col)
    _, is_array, _ = parse_bq_type(col.bq_type)
    empty_to_null = col.transform == "empty_string_to_null"

    def run(v):
        if v is None:
            return [] if is_array else None   # BigQuery arrays cannot be NULL
        if empty_to_null and v == "":
            return None
        return convert(v)
    return run


# ---------------------------------------------------------------- extract -> parquet

def select_query(tp: TablePlan) -> sql.Composed:
    """SELECT the source column of every target column (a JSON column twice when it has
    a _raw companion), ordered by the first column."""
    cols = [sql.Identifier(c.source_column) for c in target_columns(tp)]
    return sql.SQL("SELECT {} FROM {} ORDER BY 1").format(
        sql.SQL(", ").join(cols), sql.Identifier("public", tp.source_table))


def convert_chunk(rows: list[tuple], converters: list[Converter]) -> list[list]:
    """Apply per-column converters to a chunk; return column-major value lists."""
    return [[conv(r[i]) for r in rows] for i, conv in enumerate(converters)]


def columns_to_arrow(columns: list[list], schema: pa.Schema) -> pa.Table:
    """Build an Arrow table with the explicit schema from converted columns."""
    arrays = [pa.array(col, type=field.type) for col, field in zip(columns, schema)]
    return pa.Table.from_arrays(arrays, schema=schema)


def has_json_column(tp: TablePlan) -> bool:
    """BigQuery cannot load Parquet into JSON columns, so such tables load as NDJSON."""
    return any(parse_bq_type(c.bq_type)[0] == "JSON" for c in target_columns(tp))


def ndjson_value(v, base: str) -> str:
    """Serialize one non-NULL converted value for an NDJSON load, exactly (no float money)."""
    if base == "JSON":
        return v  # raw JSON text from Postgres, embedded as-is
    if isinstance(v, Decimal):
        return f'"{format(v, "f")}"'
    if isinstance(v, datetime):
        return json.dumps(v.isoformat(sep=" ", timespec="microseconds"))
    if hasattr(v, "isoformat"):
        return json.dumps(v.isoformat())
    return json.dumps(v, ensure_ascii=False)


def columns_to_ndjson(columns: list[list], tp: TablePlan) -> str:
    """Render converted columns as newline-delimited JSON rows.

    NULLs are omitted, not written as null: a JSON column would store a JSON 'null'
    value instead of SQL NULL. A missing field always loads as SQL NULL.
    """
    cols = target_columns(tp)
    keys = [json.dumps(c.name) for c in cols]
    bases = [parse_bq_type(c.bq_type)[0] for c in cols]
    lines = []
    for row in zip(*columns):
        parts = [f"{k}: {ndjson_value(v, b)}"
                 for k, v, b in zip(keys, row, bases) if v is not None]
        lines.append("{" + ", ".join(parts) + "}\n")
    return "".join(lines)


def count_null_arrays(rows: list[tuple], tp: TablePlan) -> int:
    """Count NULL values in ARRAY columns (they load as empty arrays)."""
    idx = [i for i, c in enumerate(target_columns(tp)) if parse_bq_type(c.bq_type)[1]]
    return sum(1 for r in rows for i in idx if r[i] is None)


def extract(conn, tp: TablePlan, parquet_path: Path, ndjson_path: Path | None) -> dict:
    """Stream a table into Parquet (and NDJSON when needed) in chunks; return stats."""
    schema = arrow_schema(tp)
    converters = [make_converter(c) for c in target_columns(tp)]
    rows_extracted, null_arrays = 0, 0
    ndjson = ndjson_path.open("w", encoding="utf-8", newline="\n") if ndjson_path else None
    try:
        with conn.cursor(name=f"extract_{tp.source_table}") as cur, \
                pq.ParquetWriter(parquet_path, schema, use_compliant_nested_type=True) as writer:
            cur.itersize = CHUNK_ROWS
            cur.execute(select_query(tp))
            while rows := cur.fetchmany(CHUNK_ROWS):
                columns = convert_chunk(rows, converters)
                writer.write_table(columns_to_arrow(columns, schema))
                if ndjson:
                    ndjson.write(columns_to_ndjson(columns, tp))
                rows_extracted += len(rows)
                null_arrays += count_null_arrays(rows, tp)
    finally:
        if ndjson:
            ndjson.close()
    return {"rows_extracted": rows_extracted, "null_arrays_loaded_as_empty": null_arrays}


def open_source():
    """Read-only Postgres connection that returns json/jsonb as raw text, not floats."""
    conn = connect()
    register_default_json(conn, loads=lambda s: s)
    register_default_jsonb(conn, loads=lambda s: s)
    return conn


# ---------------------------------------------------------------- BigQuery load

def ensure_dataset(client, project: str, dataset: str):
    """Create the dataset in US if missing; return it."""
    from google.cloud import bigquery
    ds = bigquery.Dataset(f"{project}.{dataset}")
    ds.location = "US"
    return client.create_dataset(ds, exists_ok=True)


def load_job_config(tp: TablePlan, sandbox: bool, source_format: str):
    """Load config with explicit schema, clustering and (outside sandbox) partitioning."""
    from google.cloud import bigquery
    config = bigquery.LoadJobConfig(
        source_format=source_format,
        schema=[bq_field(c) for c in target_columns(tp)],
        autodetect=False,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
    )
    if source_format == bigquery.SourceFormat.PARQUET:
        parquet_options = bigquery.ParquetOptions()
        parquet_options.enable_list_inference = True
        config.parquet_options = parquet_options
    if tp.clustering_columns:
        config.clustering_fields = tp.clustering_columns
    if tp.partition_column and not sandbox:
        config.time_partitioning = bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field=tp.partition_column,
            expiration_ms=None)
    return config


def load_file(client, path: Path, source_format: str, table_id: str,
              tp: TablePlan, sandbox: bool) -> dict:
    """Replace the target table and load the file with a load job; return load stats."""
    client.delete_table(table_id, not_found_ok=True)  # fresh table spec every run
    config = load_job_config(tp, sandbox, source_format)
    with path.open("rb") as f:
        job = client.load_table_from_file(f, table_id, job_config=config)
    job.result()
    table = client.get_table(table_id)
    return {"rows_loaded": job.output_rows, "table_num_rows": table.num_rows,
            "job_id": job.job_id, "load_format": source_format,
            "load_file": path.relative_to(ROOT).as_posix(),
            "table_expires": table.expires.isoformat() if table.expires else None}


# ---------------------------------------------------------------- orchestration

def partition_status(tp: TablePlan, sandbox: bool) -> str:
    if not tp.partition_column:
        return "none"
    return "deferred" if sandbox else "applied"


def migrate_table(conn, client, tp: TablePlan, prefix: str, settings: dict) -> dict:
    """Extract, convert, load and verify one table; return its log entry."""
    started = time.perf_counter()
    target = f"{prefix}{tp.target_table}"
    table_id = f"{settings['project']}.{settings['dataset']}.{target}"
    path = TMP_DIR / f"{target}.parquet"
    status = partition_status(tp, settings["sandbox"])
    if status == "deferred":
        print(f"  {tp.source_table}: partitioning deferred: sandbox partition expiry "
              f"(planned on {tp.partition_column})")
    ndjson_path = TMP_DIR / f"{target}.ndjson" if has_json_column(tp) else None
    extracted = extract(conn, tp, path, ndjson_path)
    if ndjson_path:
        print(f"  {tp.source_table}: JSON column present -> loading NDJSON "
              f"(BigQuery cannot load Parquet into JSON)")
        loaded = load_file(client, ndjson_path, "NEWLINE_DELIMITED_JSON", table_id, tp,
                           settings["sandbox"])
    else:
        loaded = load_file(client, path, "PARQUET", table_id, tp, settings["sandbox"])
    entry = {
        "source_table": tp.source_table, "target_table": target, "table_id": table_id,
        **extracted, **loaded,
        "seconds": round(time.perf_counter() - started, 2),
        "parquet_file": path.relative_to(ROOT).as_posix(),
        "partition_column": tp.partition_column, "partitioning": status,
        "clustering_columns": tp.clustering_columns,
        "migrated_at": datetime.now().replace(microsecond=0).isoformat(),
    }
    entry["ok"] = entry["rows_loaded"] == entry["rows_extracted"] == entry["table_num_rows"]
    return entry


def update_log(plan_name: str, settings: dict, entries: list[dict]) -> None:
    """Merge table entries into migration_log.json under the plan name."""
    log = json.loads(LOG_PATH.read_text(encoding="utf-8")) if LOG_PATH.exists() else {}
    section = log.setdefault(plan_name, {"tables": {}})
    section.update({"sandbox": settings["sandbox"], "dataset": settings["dataset"],
                    "project": settings["project"]})
    for e in entries:
        section["tables"][e["source_table"]] = e
    LOG_PATH.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")


def select_tables(plan: Plan, names: list[str] | None) -> list[TablePlan]:
    """Restrict the plan to --tables (all tables when omitted)."""
    if not names:
        return plan.tables
    by_name = {t.source_table: t for t in plan.tables}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        sys.exit(f"Unknown tables: {', '.join(unknown)}. Plan has: {', '.join(by_name)}")
    return [by_name[n] for n in names]


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Migrate Postgres tables to BigQuery per a saved plan.")
    p.add_argument("--plan", choices=sorted(PLANS), required=True)
    p.add_argument("--tables", nargs="+", help="only migrate these source tables")
    return p.parse_args(argv)


class PlanNotApproved(RuntimeError):
    """plan.json has no approved_at, so the agent plan may not be migrated."""


def run_migration(plan_name: str, tables: list[str] | None = None,
                  on_event: Callable[[str, dict], None] | None = None) -> list[dict]:
    """Migrate a plan; report progress through on_event(kind, data).

    Events: "start" {target, sandbox}, "table_start" {source_table, partitioning},
    "table_done" <log entry>. The agent plan must be approved (approved_at in plan.json).
    """
    from google.cloud import bigquery
    emit = on_event or (lambda kind, data: None)
    if plan_name == "agent" and not approval_info(PLAN_PATH):
        raise PlanNotApproved("plan.json is not approved; approve it in the app or run "
                              "'python -m agent.planner --approve' first")
    settings = read_bq_settings()
    selected = select_tables(load_plan(PLANS[plan_name]), tables)
    TMP_DIR.mkdir(exist_ok=True)
    client = bigquery.Client(project=settings["project"])
    ensure_dataset(client, settings["project"], settings["dataset"])
    emit("start", {"target": f"{settings['project']}.{settings['dataset']}",
                   "sandbox": settings["sandbox"], "tables": [t.source_table for t in selected]})
    entries = []
    conn = open_source()
    try:
        for tp in selected:
            emit("table_start", {"source_table": tp.source_table,
                                 "partitioning": partition_status(tp, settings["sandbox"]),
                                 "partition_column": tp.partition_column})
            entry = migrate_table(conn, client, tp, f"{plan_name}_", settings)
            entries.append(entry)
            emit("table_done", entry)
            if not entry["ok"]:
                update_log(plan_name, settings, entries)
                raise RuntimeError(
                    f"Row count mismatch for {entry['target_table']}: extracted "
                    f"{entry['rows_extracted']}, job loaded {entry['rows_loaded']}, "
                    f"table has {entry['table_num_rows']}")
    finally:
        conn.close()
    update_log(plan_name, settings, entries)
    return entries


def print_event(kind: str, data: dict) -> None:
    """CLI progress output for run_migration."""
    if kind == "start":
        print(f"Migrating -> {data['target']} (sandbox={data['sandbox']})")
    elif kind == "table_done":
        print(f"  {data['target_table']:<20} extracted={data['rows_extracted']:>6} "
              f"loaded={data['rows_loaded']:>6}  {data['seconds']:>6.2f}s  "
              f"partitioning={data['partitioning']}")
        if data["null_arrays_loaded_as_empty"]:
            print(f"    note: {data['null_arrays_loaded_as_empty']} NULL arrays "
                  f"loaded as [] (BigQuery arrays cannot be NULL)")


def main(argv=None) -> None:
    args = parse_args(argv)
    try:
        run_migration(args.plan, args.tables, print_event)
    except PlanNotApproved as e:
        sys.exit(str(e))
    print(f"Done. Log written to {LOG_PATH}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

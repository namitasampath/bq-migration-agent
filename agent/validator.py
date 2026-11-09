"""Prove (or disprove) a migration: compare each Postgres table with its BigQuery copy.

  python -m agent.validator --plan agent   -> agent_* tables -> report_agent.json
  python -m agent.validator --plan naive   -> naive_* tables -> report_naive.json
  optional: --tables loans payments   --no-ai

Every status comes from deterministic Python/SQL checks: schema vs plan, type fidelity
(target type vs what the source type requires; FLOAT64/STRING downgrades FAIL), row
count, per-column NULL / empty-string / distinct counts, exact SUM/MIN/MAX for numeric
columns, and MD5 row fingerprints joined on the primary key, with values canonicalized
by SOURCE type. A jsonb <col>_raw companion must equal Postgres' text exactly.
Known BigQuery limitations become WARN only when provably that limitation: NULL arrays
loaded as [] (count must equal migration_log.json), and JSON numbers stored as FLOAT64
(only if the _raw companion matches exactly). Deferred partitioning is INFO. Afterwards ONE Gemini call adds plain-English explanations for
WARN/FAIL findings under ai_explanations; it never changes a status.
Exit code: 0 if no FAIL, 1 otherwise.
"""
import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple, Optional

from psycopg2 import sql
from pydantic import BaseModel, ValidationError

from agent.migrator import (IST, LOG_PATH, PLANS, open_source, parse_bq_type,
                            read_bq_settings)
from agent.planner import (TablePlan, TargetColumn, call_gemini, load_plan, make_client,
                           target_columns)
from agent.profiler import (NO_EQUALITY_TYPES, TEXT_TYPES, get_columns, get_primary_key,
                            to_json)

ROOT = Path(__file__).resolve().parents[1]
PASS, INFO, WARN, FAIL = "PASS", "INFO", "WARN", "FAIL"
SEVERITY = {PASS: 0, INFO: 0, WARN: 1, FAIL: 2}
NUMBER_UDTS = {"int2", "int4", "int8", "numeric"}
INT_UDTS = {"int2", "int4", "int8"}
BQ_NUMERIC_BASES = {"INT64", "NUMERIC", "BIGNUMERIC", "FLOAT64"}
LEGACY_BQ_TYPES = {"INTEGER": "INT64", "FLOAT": "FLOAT64", "BOOLEAN": "BOOL"}
KNOWN_ARRAY_LIMITATION = "known BigQuery limitation: NULL arrays loaded as empty arrays"
KNOWN_JSON_LIMITATION = "known BigQuery limitation: JSON numbers stored as FLOAT64"
# A _raw companion is compared as plain text: exactly Postgres' <col>::text.
RAW_SRC = {"udt": "text", "type": "text", "is_array": False, "numeric_scale": None,
           "element_type": None}
# source udts -> BigQuery base types that preserve them (anything else is a downgrade)
FIDELITY_RULES = [
    ({"numeric"}, {"NUMERIC", "BIGNUMERIC"}),
    ({"int2", "int4", "int8"}, {"INT64", "NUMERIC", "BIGNUMERIC"}),
    ({"timestamp", "timestamptz"}, {"DATETIME", "TIMESTAMP"}),
    ({"json", "jsonb"}, {"JSON"}),
    ({"date"}, {"DATE"}),
    ({"bool"}, {"BOOL"}),
    ({"text", "varchar", "bpchar", "citext", "name"}, {"STRING"}),
]
MAX_EXAMPLES = 5


def check(name: str, status: str, column: Optional[str] = None, source=None,
          target=None, detail: str = "") -> dict:
    """One check result record."""
    return {"check": name, "column": column, "status": status,
            "source": source, "target": target, "detail": detail}


def worst(statuses) -> str:
    """Worst status by severity (INFO counts as PASS)."""
    result = PASS
    for s in statuses:
        if SEVERITY[s] > SEVERITY[result]:
            result = s
    return result


# ---------------------------------------------------------------- canonical values

def canonical_number(v, scale: Optional[int]) -> Optional[str]:
    """Exact decimal string at the source scale. Pads, never rounds away extra digits."""
    if v is None:
        return None
    try:
        d = Decimal(repr(v)) if isinstance(v, float) else Decimal(str(v))
    except InvalidOperation:
        return str(v)
    if not d.is_finite():
        return str(d)
    norm = d.normalize()
    if scale is None or norm.as_tuple().exponent < -scale:
        return format(norm, "f")
    return format(d.quantize(Decimal(1).scaleb(-scale)), "f")


def canonical_datetime(v, transform: str) -> Optional[str]:
    """ISO datetime with 6-digit microseconds, in the source's (naive) clock."""
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    if not isinstance(v, datetime):
        return str(v)
    if v.tzinfo is not None:
        v = (v.astimezone(IST) if transform == "timestamp_to_timestamp_utc"
             else v.astimezone(timezone.utc)).replace(tzinfo=None)
    return v.isoformat(timespec="microseconds")


def canonical_json(v) -> Optional[str]:
    """Parse JSON and re-serialize with sorted keys; numbers kept exact."""
    if isinstance(v, str):
        try:
            v = json.loads(v, parse_float=Decimal)
        except json.JSONDecodeError:
            return v
    return dump_canonical_json(v)


def dump_canonical_json(o) -> str:
    """Sorted keys, no spaces, every number as a bare normalized decimal (590.0 == 590).

    Numbers stay unquoted, so the JSON string "590" never equals the number 590.
    """
    if isinstance(o, dict):
        items = sorted(o.items())
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{dump_canonical_json(v)}"
                              for k, v in items) + "}"
    if isinstance(o, list):
        return "[" + ",".join(dump_canonical_json(v) for v in o) + "]"
    if isinstance(o, (int, float, Decimal)) and not isinstance(o, bool):
        d = Decimal(repr(o)) if isinstance(o, float) else Decimal(o)
        return format(d.normalize(), "f") if d.is_finite() else json.dumps(str(d))
    return json.dumps(o, ensure_ascii=False)


def canonical(v, src: dict, transform: str):
    """Canonical form of a value according to its SOURCE column type."""
    if v is None:
        return None
    udt = src["udt"]
    if src["is_array"]:
        if not isinstance(v, list):
            return json.dumps(v, ensure_ascii=False)  # e.g. naive comma-joined string
        elem = {"udt": src["element_type"], "is_array": False, "numeric_scale": None}
        return json.dumps([canonical(e, elem, transform) for e in v], ensure_ascii=False)
    if udt in INT_UDTS:
        return canonical_number(v, 0)
    if udt == "numeric":
        return canonical_number(v, src["numeric_scale"])
    if udt in ("timestamp", "timestamptz"):
        return canonical_datetime(v, transform)
    if udt == "date":
        return v.isoformat() if isinstance(v, date) else str(v)
    if udt in ("json", "jsonb"):
        return canonical_json(v)
    if udt == "bool":
        return str(v).lower()
    return v if isinstance(v, str) else str(v)


def row_hash(values: list) -> str:
    """MD5 of a canonical row ('' and NULL stay distinct: "" vs null)."""
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- column specs

class Spec(NamedTuple):
    """One target column with the source info used to canonicalize and aggregate it."""
    tc: TargetColumn
    src: dict                 # source column profile (RAW_SRC for a _raw companion)
    pg_expr: sql.Composable   # Postgres expression producing the expected value


def column_specs(tp: TablePlan, src_cols: dict) -> list[Spec]:
    """Specs for every physical target column, companions included."""
    specs = []
    for tc in target_columns(tp):
        ident = sql.Identifier(tc.source_column)
        if tc.is_raw_companion:
            src = {**RAW_SRC, "type": f"{src_cols[tc.source_column]['type']} (raw text)"}
            specs.append(Spec(tc, src, sql.SQL("{}::text").format(ident)))
        else:
            specs.append(Spec(tc, src_cols[tc.source_column], ident))
    return specs


# ---------------------------------------------------------------- aggregate stats

def pg_stat_exprs(spec: Spec) -> list[tuple[str, sql.Composable]]:
    """Postgres aggregates for one target column."""
    c, src = spec.pg_expr, spec.src
    distinct = sql.SQL("{}::text").format(c) if src["udt"] in NO_EQUALITY_TYPES else c
    exprs = [("null_count", sql.SQL("count(*) - count({})").format(c)),
             ("distinct_count", sql.SQL("count(DISTINCT {})").format(distinct))]
    if src["udt"] in TEXT_TYPES:
        exprs.append(("empty_string_count", sql.SQL("count(*) FILTER (WHERE {} = '')").format(c)))
    if src["udt"] in NUMBER_UDTS:
        exprs += [(f, sql.SQL(f"{f}({{}})").format(c)) for f in ("sum", "min", "max")]
    if src["is_array"]:
        exprs.append(("empty_array_count",
                      sql.SQL("count(*) FILTER (WHERE cardinality({}) = 0)").format(c)))
    return exprs


def bq_stat_exprs(spec: Spec) -> list[tuple[str, str]]:
    """BigQuery aggregates for the same column, adapted to the target type."""
    c = f"`{spec.tc.name}`"
    base, is_array, _ = parse_bq_type(spec.tc.bq_type)
    distinct_arg = (f"IF({c} IS NULL, NULL, TO_JSON_STRING({c}))"
                    if is_array or base == "JSON" else c)
    exprs = [("null_count", f"COUNTIF({c} IS NULL)"),
             ("distinct_count", f"COUNT(DISTINCT {distinct_arg})")]
    if spec.src["udt"] in TEXT_TYPES and base == "STRING" and not is_array:
        exprs.append(("empty_string_count", f"COUNTIF({c} = '')"))
    if spec.src["udt"] in NUMBER_UDTS and base in BQ_NUMERIC_BASES and not is_array:
        exprs += [(f, f"{f.upper()}({c})") for f in ("sum", "min", "max")]
    if is_array:
        exprs.append(("empty_array_count", f"COUNTIF(ARRAY_LENGTH({c}) = 0)"))
    return exprs


def pg_aggregates(cur, tp: TablePlan, specs: list[Spec]) -> dict:
    """Run one aggregate scan in Postgres: {'row_count': n, col: {stat: v}}."""
    keys, selects = [("", "row_count")], [sql.SQL("count(*)")]
    for spec in specs:
        for stat, expr in pg_stat_exprs(spec):
            keys.append((spec.tc.name, stat))
            selects.append(expr)
    cur.execute(sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(selects), sql.Identifier("public", tp.source_table)))
    return unpack(keys, cur.fetchone())


def bq_aggregates(client, table_id: str, specs: list[Spec]) -> dict:
    """Run one aggregate query in BigQuery with the same shape as pg_aggregates."""
    keys, selects = [("", "row_count")], ["COUNT(*)"]
    for spec in specs:
        for stat, expr in bq_stat_exprs(spec):
            keys.append((spec.tc.name, stat))
            selects.append(expr)
    aliased = ", ".join(f"{e} AS a{i}" for i, e in enumerate(selects))
    row = list(client.query(f"SELECT {aliased} FROM `{table_id}`").result())[0]
    return unpack(keys, list(row.values()))


def unpack(keys: list[tuple[str, str]], values) -> dict:
    out: dict = {}
    for (col, stat), v in zip(keys, values):
        if col == "":
            out[stat] = v
        else:
            out.setdefault(col, {})[stat] = v
    return out


# ---------------------------------------------------------------- aggregate checks

def array_columns(specs: list[Spec]) -> list[str]:
    return [s.tc.name for s in specs if parse_bq_type(s.tc.bq_type)[1]]


def limitation_applies(specs: list[Spec], pg: dict, log_entry: dict) -> bool:
    """True when source NULL arrays exactly equal the count the migrator logged."""
    logged = log_entry.get("null_arrays_loaded_as_empty", 0)
    total = sum(pg[c]["null_count"] for c in array_columns(specs))
    return logged > 0 and total == logged


def compare_count(name: str, col: str, s, t) -> dict:
    return check(name, PASS if s == t else FAIL, col, s, t,
                 "" if s == t else f"source {s} != target {t}")


def null_check(col: str, s: int, t: int, is_array: bool, known: bool) -> dict:
    if s == t:
        return check("null_count", PASS, col, s, t)
    if is_array and known and t == 0:
        return check("null_count", WARN, col, s, t, KNOWN_ARRAY_LIMITATION)
    return check("null_count", FAIL, col, s, t, f"source {s} != target {t}")


def distinct_check(col: str, pg: dict, bq: dict, is_array: bool, known: bool) -> dict:
    s, t = pg["distinct_count"], bq["distinct_count"]
    if s == t:
        return check("distinct_count", PASS, col, s, t)
    if (is_array and known and pg["null_count"] > 0
            and pg["empty_array_count"] == 0 and t == s + 1):
        return check("distinct_count", WARN, col, s, t,
                     f"{KNOWN_ARRAY_LIMITATION} ([] became a new distinct value)")
    return check("distinct_count", FAIL, col, s, t, f"source {s} != target {t}")


def numeric_checks(col: str, pg: dict, bq: dict) -> list[dict]:
    """Exact SUM/MIN/MAX as Decimal strings, no tolerance."""
    out = []
    for stat in ("sum", "min", "max"):
        if stat not in bq:
            out.append(check(stat, FAIL, col, canonical_number(pg[stat], None), None,
                             "target column is not numeric"))
            continue
        s, t = canonical_number(pg[stat], None), canonical_number(bq[stat], None)
        out.append(check(stat, PASS if s == t else FAIL, col, s, t,
                         "" if s == t else "values differ (exact comparison)"))
    return out


def aggregate_checks(specs: list[Spec], pg: dict, bq: dict, log_entry: dict) -> list[dict]:
    """Checks 1-5: row count, NULLs, empty strings, distinct, numeric SUM/MIN/MAX."""
    known = limitation_applies(specs, pg, log_entry)
    results = [compare_count("row_count", None, pg["row_count"], bq["row_count"])]
    logged = log_entry.get("rows_loaded")
    if logged is not None and logged != bq["row_count"]:
        results.append(check("row_count_vs_log", FAIL, None, logged, bq["row_count"],
                             "migration_log rows_loaded differs from target table"))
    for spec in specs:
        name = spec.tc.name
        is_array = parse_bq_type(spec.tc.bq_type)[1]
        p, b = pg[name], bq[name]
        results.append(null_check(name, p["null_count"], b["null_count"], is_array, known))
        if "empty_string_count" in p:
            if "empty_string_count" in b:
                results.append(compare_count("empty_string_count", name,
                                             p["empty_string_count"], b["empty_string_count"]))
            else:
                results.append(check("empty_string_count", FAIL, name,
                                     p["empty_string_count"], None, "target is not STRING"))
        results.append(distinct_check(name, p, b, is_array, known))
        if spec.src["udt"] in NUMBER_UDTS:
            results.extend(numeric_checks(name, p, b))
    return results


# ---------------------------------------------------------------- metadata checks

def actual_type(field) -> tuple[str, bool]:
    """(standard SQL base type, is_repeated) of a BigQuery SchemaField."""
    return LEGACY_BQ_TYPES.get(field.field_type, field.field_type), field.mode == "REPEATED"


def schema_check(specs: list[Spec], table) -> dict:
    """Target columns, types and modes match the plan (companions included)."""
    fields = {f.name: f for f in table.schema}
    problems = []
    for spec in specs:
        base, is_array, _ = parse_bq_type(spec.tc.bq_type)
        f = fields.get(spec.tc.name)
        if f is None:
            problems.append(f"{spec.tc.name}: missing")
            continue
        actual, repeated = actual_type(f)
        if actual != base or repeated != is_array:
            problems.append(f"{spec.tc.name}: expected {spec.tc.bq_type}, got {actual} {f.mode}")
    extra = fields.keys() - {s.tc.name for s in specs}
    problems += [f"{name}: not in plan" for name in sorted(extra)]
    return check("schema", FAIL if problems else PASS, None, None, None, "; ".join(problems))


def required_types(src: dict) -> Optional[set[str]]:
    """BigQuery base types that preserve a source type (None = no rule)."""
    udt = src["element_type"] if src["is_array"] else src["udt"]
    for udts, allowed in FIDELITY_RULES:
        if udt in udts:
            return allowed
    return None


def type_fidelity_checks(specs: list[Spec], table) -> list[dict]:
    """Each target column's ACTUAL BigQuery type must be one the source type requires."""
    fields = {f.name: f for f in table.schema}
    out = []
    for spec in specs:
        f = fields.get(spec.tc.name)
        if f is None:
            continue  # reported by schema_check
        actual, repeated = actual_type(f)
        shown = f"ARRAY<{actual}>" if repeated else actual
        allowed = required_types(spec.src)
        if allowed is None:
            out.append(check("type_fidelity", PASS, spec.tc.name, spec.src["type"], shown,
                             "no fidelity rule for this source type"))
            continue
        need = " or ".join(sorted(allowed))
        need = f"ARRAY<{need}>" if spec.src["is_array"] else need
        ok = actual in allowed and repeated == spec.src["is_array"]
        out.append(check("type_fidelity", PASS if ok else FAIL, spec.tc.name,
                         spec.src["type"], shown,
                         "" if ok else f"downgrade: {spec.src['type']} requires {need}"))
    return out


def layout_checks(tp: TablePlan, table, log_entry: dict) -> list[dict]:
    """Partitioning (deferred in sandbox = INFO) and clustering vs the plan."""
    out = []
    actual_part = table.time_partitioning.field if table.time_partitioning else None
    if log_entry.get("partitioning") == "deferred":
        out.append(check("partitioning", INFO, tp.partition_column, tp.partition_column,
                         actual_part, "partitioning deferred: sandbox partition expiry"))
    else:
        ok = actual_part == tp.partition_column
        out.append(check("partitioning", PASS if ok else FAIL, tp.partition_column,
                         tp.partition_column, actual_part))
    actual_cluster = list(table.clustering_fields or [])
    ok = actual_cluster == tp.clustering_columns
    out.append(check("clustering", PASS if ok else FAIL, None, tp.clustering_columns,
                     actual_cluster))
    return out


# ---------------------------------------------------------------- row fingerprints

def fetch_pg_rows(cur, tp: TablePlan, specs: list[Spec], pk: list[str]) -> list[tuple]:
    cols = sql.SQL(", ").join(s.pg_expr for s in specs)
    order = sql.SQL(", ").join(sql.Identifier(c) for c in pk) if pk else sql.SQL("1")
    cur.execute(sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        cols, sql.Identifier("public", tp.source_table), order))
    return cur.fetchall()


def fetch_bq_rows(client, table_id: str, specs: list[Spec], pk: list[str]) -> list[tuple]:
    """Download the target table via Arrow (REST, no Storage API needed)."""
    cols = ", ".join(f"`{s.tc.name}`" for s in specs)
    order = ", ".join(f"`{c}`" for c in pk) if pk else "1"
    arrow = client.query(f"SELECT {cols} FROM `{table_id}` ORDER BY {order}").to_arrow(
        create_bqstorage_client=False)
    return [tuple(r.values()) for r in arrow.to_pylist()]


def canonical_rows(rows: list[tuple], specs: list[Spec]) -> list[list]:
    return [[canonical(v, s.src, s.tc.transform) for v, s in zip(row, specs)] for row in rows]


def json_equal_as_float64(a, b) -> bool:
    """Same JSON structure and strings; numbers equal once both are read as FLOAT64."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal_as_float64(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal_as_float64(x, y) for x, y in zip(a, b))
    if is_json_number(a) and is_json_number(b):
        return float(a) == float(b)
    return type(a) is type(b) and a == b


def is_json_number(v) -> bool:
    return isinstance(v, (int, Decimal)) and not isinstance(v, bool)


def json_float_only_diff(s: str, t: str) -> bool:
    """True when two canonical JSON texts differ only by FLOAT64 rounding of numbers."""
    try:
        a, b = (json.loads(x, parse_float=Decimal) for x in (s, t))
    except (TypeError, json.JSONDecodeError):
        return False
    return json_equal_as_float64(a, b)


def raw_companions_ok(specs: list[Spec], cell_counts: dict, failing_cols: set) -> dict:
    """source column -> True if its _raw companion matched exactly (rows and aggregates)."""
    return {s.tc.source_column: cell_counts.get(s.tc.name, 0) == 0
            and s.tc.name not in failing_cols
            for s in specs if s.tc.is_raw_companion}


def classify_cell(name: str, s, t, ctx: dict) -> Optional[str]:
    """Name of the known limitation explaining this cell difference, or None."""
    if ctx["array_known"] and name in ctx["arrays"] and s is None and t == "[]":
        return "null_array"
    src_col = ctx["json_cols"].get(name)
    if (src_col and ctx["raw_ok"].get(src_col) and s is not None and t is not None
            and json_float_only_diff(s, t)):
        return "json_float"
    return None


def fingerprint_check(specs: list[Spec], pk: list[str], pg_rows, bq_rows, pg_stats: dict,
                      log_entry: dict, agg_results: list[dict]) -> tuple[dict, dict]:
    """Check 6: MD5 per canonical row, joined on the primary key."""
    names = [s.tc.name for s in specs]
    pk_idx = [names.index(c) for c in pk] if pk else list(range(len(names)))
    src = {tuple(r[i] for i in pk_idx): r for r in canonical_rows(pg_rows, specs)}
    tgt = {tuple(r[i] for i in pk_idx): r for r in canonical_rows(bq_rows, specs)}
    stats = {"source_rows": len(src), "target_rows": len(tgt), "matching": 0,
             "mismatched": 0, "known_limitation_rows": 0,
             "missing_in_target": len(src.keys() - tgt.keys()),
             "extra_in_target": len(tgt.keys() - src.keys()),
             "column_mismatch_counts": {}, "known_limitation_cells": {}, "examples": []}
    diffs = {}
    for key in [k for k in src if k in tgt]:  # source PK order
        if row_hash(src[key]) == row_hash(tgt[key]):
            stats["matching"] += 1
        else:
            diffs[key] = [(n, s, t) for n, s, t in zip(names, src[key], tgt[key]) if s != t]
    cell_counts: dict = {}
    for cells in diffs.values():
        for n, _, _ in cells:
            cell_counts[n] = cell_counts.get(n, 0) + 1
    failing = {c["column"] for c in agg_results if c["status"] == FAIL and c["column"]}
    ctx = {"arrays": array_columns(specs),
           "array_known": limitation_applies(specs, pg_stats, log_entry),
           "json_cols": {s.tc.name: s.tc.source_column for s in specs
                         if parse_bq_type(s.tc.bq_type)[0] == "JSON"},
           "raw_ok": raw_companions_ok(specs, cell_counts, failing)}
    stats["raw_companions_exact"] = ctx["raw_ok"]
    known_examples = []
    for key, cells in diffs.items():
        real = []
        for name, s, t in cells:
            kind = classify_cell(name, s, t, ctx)
            if kind is None:
                real.append((name, s, t))
                continue
            kc = stats["known_limitation_cells"]
            kc[kind] = kc.get(kind, 0) + 1
            if len(known_examples) < MAX_EXAMPLES:
                known_examples.append(example(pk, key, name, s, t, kind))
        if not real:
            stats["known_limitation_rows"] += 1
            continue
        stats["mismatched"] += 1
        for name, s, t in real:
            counts = stats["column_mismatch_counts"]
            counts[name] = counts.get(name, 0) + 1
            if len(stats["examples"]) < MAX_EXAMPLES:
                stats["examples"].append(example(pk, key, name, s, t, None))
    if not stats["examples"]:
        stats["examples"] = known_examples
    return fingerprint_status(stats, log_entry, len(src), len(tgt)), stats


def fingerprint_status(stats: dict, log_entry: dict, n_src: int, n_tgt: int) -> dict:
    """PASS / WARN (only known limitations) / FAIL for the fingerprint check."""
    known = stats["known_limitation_cells"]
    logged = log_entry.get("null_arrays_loaded_as_empty", 0)
    if stats["mismatched"] or stats["missing_in_target"] or stats["extra_in_target"]:
        status, detail = FAIL, (f"{stats['mismatched']} mismatched, {stats['missing_in_target']} "
                                f"missing, {stats['extra_in_target']} extra rows")
    elif known.get("null_array", 0) not in (0, logged):
        status, detail = FAIL, (f"{known['null_array']} NULL->[] cells but migration log "
                                f"recorded {logged}")
    elif known:
        parts = []
        if known.get("null_array"):
            parts.append(f"{known['null_array']} cells: {KNOWN_ARRAY_LIMITATION}")
        if known.get("json_float"):
            parts.append(f"{known['json_float']} cells: {KNOWN_JSON_LIMITATION} "
                         f"(_raw companion matches exactly)")
        status = WARN
        detail = (f"{stats['known_limitation_rows']} rows differ only by known limitations: "
                  + "; ".join(parts))
    else:
        status, detail = PASS, f"all {stats['matching']} rows match"
    return check("row_fingerprint", status, None, n_src, n_tgt, detail)


def example(pk: list[str], key: tuple, column: str, s, t, known: Optional[str]) -> dict:
    return {"primary_key": dict(zip(pk, key)) if pk else list(key), "column": column,
            "source_value": s, "target_value": t, "known_limitation": known}


# ---------------------------------------------------------------- per table

def validate_table(cur, client, tp: TablePlan, prefix: str, settings: dict,
                   log_entry: Optional[dict]) -> dict:
    """Run every check for one table and return its report section."""
    from google.api_core.exceptions import NotFound
    target = f"{prefix}{tp.target_table}"
    table_id = f"{settings['project']}.{settings['dataset']}.{target}"
    result = {"source_table": tp.source_table, "target_table": target, "table_id": table_id}
    if log_entry is None:
        return finish(result, [check("migration_log", FAIL,
                                     detail="table not in migration_log.json")])
    try:
        table = client.get_table(table_id)
    except NotFound:
        return finish(result, [check("exists", FAIL, detail=f"{table_id} not found")])
    src_cols = {c["name"]: c for c in get_columns(cur, tp.source_table)}
    missing = [c.source_column for c in tp.columns if c.source_column not in src_cols]
    if missing:
        return finish(result, [check("source_columns", FAIL,
                                     detail=f"missing in source: {missing}")])
    specs = column_specs(tp, src_cols)
    checks = [schema_check(specs, table)] + type_fidelity_checks(specs, table)
    if checks[0]["status"] == FAIL:
        return finish(result, checks + layout_checks(tp, table, log_entry))
    pg = pg_aggregates(cur, tp, specs)
    bq = bq_aggregates(client, table_id, specs)
    agg = aggregate_checks(specs, pg, bq, log_entry)
    checks += agg
    pk = get_primary_key(cur, tp.source_table)
    fp_check, fp_stats = fingerprint_check(
        specs, pk, fetch_pg_rows(cur, tp, specs, pk),
        fetch_bq_rows(client, table_id, specs, pk), pg, log_entry, agg)
    checks.append(fp_check)
    checks += layout_checks(tp, table, log_entry)
    result["primary_key"] = pk
    result["fingerprint"] = fp_stats
    return finish(result, checks)


def finish(result: dict, checks: list[dict]) -> dict:
    result["checks"] = checks
    result["status"] = worst(c["status"] for c in checks)
    result["check_counts"] = {s: sum(c["status"] == s for c in checks)
                              for s in (PASS, INFO, WARN, FAIL)}
    return result


# ---------------------------------------------------------------- AI explanations

class Explanation(BaseModel):
    finding_id: str
    explanation: str
    suggested_fix: str


class Explanations(BaseModel):
    items: list[Explanation]


EXPLAIN_PROMPT = """You explain data-migration validation findings (PostgreSQL -> BigQuery)
to engineers. Each finding's status was computed by deterministic checks and is FINAL:
never dispute or re-grade it. For every finding, return its finding_id, a plain-English
explanation (1-2 sentences: what the numbers mean and the likely cause) and a concrete
suggested_fix (1 sentence). Return exactly one item per finding."""


def collect_findings(tables: list[dict]) -> list[dict]:
    """WARN/FAIL checks with ids, plus fingerprint examples for context."""
    findings = []
    for t in tables:
        for c in t["checks"]:
            if c["status"] in (WARN, FAIL):
                f = {"finding_id": f"F{len(findings) + 1}", "table": t["source_table"],
                     **{k: c[k] for k in ("check", "column", "status", "source", "target", "detail")}}
                if c["check"] == "row_fingerprint":
                    f["examples"] = t.get("fingerprint", {}).get("examples", [])
                findings.append(f)
    return findings


def explain_findings(findings: list[dict], plan_name: str) -> dict:
    """One Gemini call; advisory only. Errors are recorded, never raised."""
    label = "AI-generated by Gemini - advisory only; does not affect any status"
    if not findings:
        return {"label": label, "items": [], "note": "no WARN/FAIL findings"}
    try:
        import os
        client = make_client()
        model = os.environ["GEMINI_MODEL"]
        prompt = (f"Plan: {plan_name}\nFindings (JSON):\n"
                  + json.dumps(findings, ensure_ascii=False, default=str))
        text = call_gemini(client, model, [prompt], EXPLAIN_PROMPT, Explanations)
        items = Explanations.model_validate_json(text).items
    except (ValidationError, Exception) as e:  # never let AI break the report
        return {"label": label, "items": [], "error": f"{type(e).__name__}: {e}"}
    by_id = {f["finding_id"]: f for f in findings}
    merged = [{**{k: by_id[i.finding_id][k] for k in ("finding_id", "table", "check", "column", "status")},
               "explanation": i.explanation, "suggested_fix": i.suggested_fix}
              for i in items if i.finding_id in by_id]
    result = {"label": label, "model": model, "items": merged}
    unexplained = sorted(by_id.keys() - {i.finding_id for i in items})
    if unexplained:
        result["unexplained_findings"] = unexplained
    return result


# ---------------------------------------------------------------- report + CLI

def summarize(tables: list[dict]) -> dict:
    statuses = [t["status"] for t in tables]
    return {"tables": len(tables), "passed": statuses.count(PASS),
            "warned": statuses.count(WARN), "failed": statuses.count(FAIL),
            "overall": worst(statuses) if statuses else PASS}


def format_table(t: dict) -> str:
    cc = t["check_counts"]
    lines = [f"\n{t['target_table']:<20} {t['status']:<5} ({sum(cc.values())} checks: "
             f"{cc[PASS]} PASS, {cc[WARN]} WARN, {cc[FAIL]} FAIL, {cc[INFO]} INFO)"]
    for c in t["checks"]:
        if c["status"] != PASS:
            where = f" {c['column']}" if c["column"] else ""
            vals = (f"source={c['source']} target={c['target']}"
                    if c["source"] is not None or c["target"] is not None else "")
            lines.append(f"    {c['status']:<4} {c['check']}{where}: {vals} {c['detail']}".rstrip())
    fp = t.get("fingerprint")
    if fp:
        lines.append(f"    rows: {fp['matching']} match, {fp['mismatched']} mismatched, "
                     f"{fp['missing_in_target']} missing, {fp['extra_in_target']} extra, "
                     f"{fp['known_limitation_rows']} known-limitation")
        for ex in fp["examples"][:3] if fp["mismatched"] else []:
            lines.append(f"      e.g. {ex['primary_key']} {ex['column']}: "
                         f"{ex['source_value']!r} -> {ex['target_value']!r}")
    return "\n".join(lines)


def format_ai(ai: dict) -> str:
    lines = [f"\n--- {ai['label']} ---"]
    if ai.get("error"):
        lines.append(f"  (unavailable: {ai['error'][:200]})")
    for i in ai["items"]:
        where = f".{i['column']}" if i["column"] else ""
        lines.append(f"  [{i['status']}] {i['table']}{where} {i['check']}: {i['explanation']}")
        lines.append(f"      fix: {i['suggested_fix']}")
    return "\n".join(lines)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate a BigQuery migration against Postgres.")
    p.add_argument("--plan", choices=sorted(PLANS), required=True)
    p.add_argument("--tables", nargs="+", help="only validate these source tables")
    p.add_argument("--no-ai", action="store_true", help="skip the Gemini explanations")
    return p.parse_args(argv)


def combined_status(statuses) -> str:
    """Worst of table/query statuses; NOT_RUN (unproven) ranks between WARN and FAIL."""
    rank = {PASS: 0, INFO: 0, WARN: 1, "NOT_RUN": 2, FAIL: 3}
    statuses = [s for s in statuses if s]
    return max(statuses, key=lambda s: rank.get(s, 0)) if statuses else PASS


def carry_over_query_parity(path: Path, report: dict) -> None:
    """Keep the translator's query_parity section when the validator rewrites the report.

    The section is copied unchanged (it was produced by the translator's own
    deterministic comparison) and marked as carried over until the translator re-runs.
    """
    if not path.exists():
        return
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    section = old.get("query_parity")
    if not section:
        return
    report["query_parity"] = {**section, "carried_over_from_report_at": old.get("generated_at")}
    report["overall_status"] = combined_status(
        [report["summary"]["overall"], section.get("summary", {}).get("overall")])


def main(argv=None) -> int:
    from google.cloud import bigquery
    from agent.migrator import select_tables
    args = parse_args(argv)
    settings = read_bq_settings()
    plan = load_plan(PLANS[args.plan])
    tables = select_tables(plan, args.tables)
    log = json.loads(LOG_PATH.read_text(encoding="utf-8")) if LOG_PATH.exists() else {}
    log_tables = log.get(args.plan, {}).get("tables", {})
    client = bigquery.Client(project=settings["project"])
    conn = open_source()
    try:
        with conn.cursor() as cur:
            results = [validate_table(cur, client, tp, f"{args.plan}_", settings,
                                      log_tables.get(tp.source_table)) for tp in tables]
    finally:
        conn.close()
    summary = summarize(results)
    ai = ({"label": "skipped (--no-ai)", "items": []} if args.no_ai
          else explain_findings(collect_findings(results), args.plan))
    report = {"plan": args.plan, "generated_at": datetime.now().replace(microsecond=0),
              "project": settings["project"], "dataset": settings["dataset"],
              "sandbox": log.get(args.plan, {}).get("sandbox"),
              "summary": summary, "tables": results, "ai_explanations": ai}
    out = ROOT / f"report_{args.plan}.json"
    carry_over_query_parity(out, report)
    out.write_text(to_json(report), encoding="utf-8")
    print(f"Validation of plan '{args.plan}' ({settings['dataset']})")
    for t in results:
        print(format_table(t))
    if ai["items"] or ai.get("error"):
        print(format_ai(ai))
    print(f"\nSUMMARY: {summary['tables']} tables: {summary['passed']} passed, "
          f"{summary['warned']} warned, {summary['failed']} failed -> {summary['overall']}")
    print(f"Report written to {out}")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

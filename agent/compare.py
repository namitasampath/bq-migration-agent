"""Read-only views over the saved JSON artifacts, for the UI and the agent-vs-naive demo.

Nothing here re-validates or calls Gemini: every number is taken from profile.json,
plan files, migration_log.json, report_<plan>.json and translations_<plan>.json, which
the deterministic modules produced. Functions return plain lists/dicts/DataFrames.
In Load mode, missing root artifacts fall back to the bundled copies in samples/.
"""
import json
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "profile": ROOT / "profile.json",
    "plan_proposed": ROOT / "plan_proposed.json",
    "plan": ROOT / "plan.json",
    "naive_plan": ROOT / "naive_plan.json",
    "migration_log": ROOT / "migration_log.json",
    "report_agent": ROOT / "report_agent.json",
    "report_naive": ROOT / "report_naive.json",
    "translations_agent": ROOT / "translations_agent.json",
    "translations_naive": ROOT / "translations_naive.json",
}
SAMPLES_DIR = ROOT / "samples"   # bundled results so "Load last results" works on a fresh clone
SAMPLE_NAMES = [n for n in FILES if n != "plan_proposed"]
_use_samples: ContextVar[bool] = ContextVar("use_samples", default=False)
CHECK_ORDER = ["exists", "schema", "type_fidelity", "row_count", "row_count_vs_log",
               "null_count", "empty_string_count", "distinct_count", "sum", "min", "max",
               "row_fingerprint", "partitioning", "clustering"]
LIMITATION_EXPLANATIONS = {
    "NULL arrays loaded as empty arrays":
        "BigQuery ARRAY columns cannot hold NULL, so a NULL Postgres array is stored as []. "
        "The migrator counts these rows and the validator accepts the difference only when "
        "the count matches the migration log exactly.",
    "JSON numbers stored as FLOAT64":
        "BigQuery's JSON type stores numbers as 64-bit floats, so a decimal such as "
        "606872.2549 can read back as 606872.2549000001. The exact original text is kept "
        "in the <column>_raw STRING companion, which must match Postgres byte-for-byte.",
    "partitioning deferred: sandbox partition expiry":
        "BigQuery sandbox expires partitions after 60 days, which would silently delete "
        "historical rows. Partitioning is recorded in the plan and applied after leaving "
        "sandbox; clustering is applied now.",
}


# ---------------------------------------------------------------- loading

def use_samples(enabled: bool) -> None:
    """Let path()/load() fall back to samples/ when a root artifact is missing (Load mode)."""
    _use_samples.set(enabled)


def path(name: str) -> Path:
    """Root artifact if present; else the bundled sample copy when the fallback is on."""
    root = FILES[name]
    sample = SAMPLES_DIR / root.name
    if not root.exists() and _use_samples.get() and name in SAMPLE_NAMES and sample.exists():
        return sample
    return root


def is_sample(name: str) -> bool:
    return path(name).parent == SAMPLES_DIR


def load(name: str) -> Optional[dict]:
    """Parsed JSON for a known artifact, or None if it does not exist / is unreadable."""
    p = path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def modified_at(name: str) -> Optional[float]:
    p = path(name)
    return p.stat().st_mtime if p.exists() else None


def approved_at() -> Optional[str]:
    """approved_at of the plan shown (root plan.json, or the sample in Load mode)."""
    return (load("plan") or {}).get("approved_at")


# ---------------------------------------------------------------- profile

def _text(v) -> Optional[str]:
    """Display value as text (min/max mix ints, exact decimals and dates)."""
    return None if v is None else str(v)


def column_traps(col: dict) -> list[str]:
    """Migration traps visible in one profiled column."""
    s, traps = col["stats"], []
    if col["udt"] == "numeric":
        traps.append(f"money precision {col['type']}, {s.get('max_decimal_places_used')} dp used")
    if s.get("empty_string_count"):
        traps.append(f"'' x{s['empty_string_count']} vs NULL x{s['null_count']}")
    if s.get("has_nonzero_microseconds"):
        traps.append(f"microseconds in {s['rows_with_microseconds']} rows")
    if col.get("is_json"):
        traps.append("JSON")
    if col.get("is_array"):
        traps.append(f"array (NULL x{s['null_count']})")
    return traps


def profile_table_frame(table: dict) -> pd.DataFrame:
    """One row per column: type, key stats and traps."""
    rows = []
    for c in table["columns"]:
        s = c["stats"]
        rows.append({"column": c["name"], "type": c["type"], "nulls": s["null_count"],
                     "empty ''": s.get("empty_string_count"),
                     "distinct": s["distinct_count"],
                     "min": _text(s.get("min")), "max": _text(s.get("max")),
                     "traps": ", ".join(column_traps(c))})
    return pd.DataFrame(rows)


def profile_trap_counts(table: dict) -> dict:
    """Counts of trap kinds in a table, for headline metrics."""
    cols = table["columns"]
    return {
        "money columns": sum(c["udt"] == "numeric" for c in cols),
        "'' vs NULL columns": sum(bool(c["stats"].get("empty_string_count")) for c in cols),
        "microsecond columns": sum(bool(c["stats"].get("has_nonzero_microseconds")) for c in cols),
        "JSON columns": sum(bool(c.get("is_json")) for c in cols),
        "array columns": sum(bool(c.get("is_array")) for c in cols),
    }


# ---------------------------------------------------------------- plan

def plan_table_frame(table: dict) -> pd.DataFrame:
    """Column decisions of one planned table, raw companions shown as extra rows."""
    rows = []
    for c in table["columns"]:
        rows.append({"column": c["source_column"], "source type": c["source_type"],
                     "BigQuery type": c["bq_type"], "transform": c["transform"],
                     "reason": c["reason"]})
        if c.get("raw_companion"):
            rows.append({"column": f"+ {c['raw_companion']}", "source type": c["source_type"],
                         "BigQuery type": "STRING", "transform": "keep_as_is",
                         "reason": "Exact original JSON text (BigQuery JSON stores numbers "
                                   "as FLOAT64)."})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- reports

def check_grid(table_report: dict) -> pd.DataFrame:
    """Status matrix: rows = columns ('(table)' for table-level), cols = check names."""
    checks = table_report.get("checks", [])
    if not checks:
        return pd.DataFrame()
    df = pd.DataFrame(checks)
    df["row"] = df["column"].where(df["check"].isin(["null_count", "empty_string_count",
                                                     "distinct_count", "sum", "min", "max",
                                                     "type_fidelity"]), "(table)")
    df["row"] = df["row"].fillna("(table)")
    grid = df.pivot_table(index="row", columns="check", values="status",
                          aggfunc=lambda s: worst_status(list(s)))
    cols = [c for c in CHECK_ORDER if c in grid.columns]
    order = ["(table)"] + [r for r in dict.fromkeys(df["row"]) if r != "(table)"]
    return grid.reindex(index=order, columns=cols).fillna("")


def worst_status(statuses: list[str]) -> str:
    rank = {"PASS": 0, "INFO": 1, "WARN": 2, "NOT_RUN": 3, "FAIL": 4}
    return max(statuses, key=lambda s: rank.get(s, 0)) if statuses else ""


def non_pass_checks(table_report: dict) -> list[dict]:
    return [c for c in table_report.get("checks", []) if c["status"] != "PASS"]


def known_limitations(report: dict) -> list[dict]:
    """WARN/INFO findings with their deterministic explanation."""
    rows = []
    for t in report.get("tables", []):
        for c in t.get("checks", []):
            if c["status"] not in ("WARN", "INFO"):
                continue
            key = next((k for k in LIMITATION_EXPLANATIONS if k in (c.get("detail") or "")), None)
            rows.append({"table": t["source_table"], "check": c["check"],
                         "column": c.get("column"), "status": c["status"],
                         "detail": c.get("detail"),
                         "explanation": LIMITATION_EXPLANATIONS.get(key, "")})
    return rows


def overall_status(report: Optional[dict]) -> str:
    if not report:
        return "NOT_RUN"
    return report.get("overall_status") or report.get("summary", {}).get("overall", "NOT_RUN")


# ---------------------------------------------------------------- agent vs naive

def _numeric_columns(table_report: dict) -> set[str]:
    """Columns whose SOURCE type is numeric(p,s), from the type_fidelity checks."""
    return {c["column"] for c in table_report.get("checks", [])
            if c["check"] == "type_fidelity" and str(c.get("source", "")).startswith("numeric")}


def _dec(v) -> Optional[Decimal]:
    try:
        return Decimal(str(v)) if v is not None else None
    except InvalidOperation:
        return None


def money_drift(report: dict) -> list[dict]:
    """Exact SUM of every money column: source vs target and the drift."""
    rows = []
    for t in report.get("tables", []):
        money = _numeric_columns(t)
        for c in t.get("checks", []):
            if c["check"] == "sum" and c.get("column") in money:
                s, g = _dec(c.get("source")), _dec(c.get("target"))
                rows.append({"table": t["source_table"], "column": c["column"],
                             "source sum": c.get("source"), "target sum": c.get("target"),
                             "drift": format(g - s, "f") if s is not None and g is not None
                             else None, "status": c["status"]})
    return rows


def empty_strings_lost(report: dict) -> list[dict]:
    """Columns where '' values in Postgres did not arrive as '' in BigQuery."""
    rows = []
    for t in report.get("tables", []):
        for c in t.get("checks", []):
            if c["check"] != "empty_string_count":
                continue
            src, tgt = c.get("source") or 0, c.get("target") or 0
            rows.append({"table": t["source_table"], "column": c["column"],
                         "source ''": src, "target ''": tgt, "lost": max(src - tgt, 0),
                         "status": c["status"]})
    return rows


def type_downgrades(report: dict) -> list[dict]:
    return [{"table": t["source_table"], "column": c["column"], "source type": c.get("source"),
             "BigQuery type": c.get("target"), "detail": c.get("detail")}
            for t in report.get("tables", []) for c in t.get("checks", [])
            if c["check"] == "type_fidelity" and c["status"] == "FAIL"]


def query_results(report: Optional[dict], plan_name: Optional[str] = None) -> list[dict]:
    """Per-query parity results: report's query_parity, else translations_<plan>.json."""
    section = (report or {}).get("query_parity") or {}
    if section.get("queries"):
        return section["queries"]
    plan_name = plan_name or (report or {}).get("plan")
    if plan_name not in ("agent", "naive"):
        return []
    doc = load(f"translations_{plan_name}") or {}
    return [{"name": q["name"], "status": q.get("status"), "detail": q.get("detail"),
             "confidence": q.get("confidence"), "dialect_changes": q.get("dialect_changes"),
             "translation_source": q.get("translation_source"),
             "examples": (q.get("parity") or {}).get("examples", [])}
            for q in doc.get("queries", [])]


def known_limitation_kinds(report: Optional[dict]) -> list[str]:
    """Distinct documented BigQuery limitations behind WARN checks (INFO excluded)."""
    kinds = []
    for t in (report or {}).get("tables", []):
        for c in t.get("checks", []):
            if c["status"] != "WARN":
                continue
            for key in LIMITATION_EXPLANATIONS:
                if key in (c.get("detail") or "") and key not in kinds:
                    kinds.append(key)
    return kinds


def fingerprint_totals(report: dict) -> dict:
    """Row fingerprint counts summed over tables.

    rows_changed counts only rows with a real value mismatch; rows that differ solely by
    a proven known limitation are counted separately (rows_known_limitation).
    """
    fps = [t.get("fingerprint") or {} for t in report.get("tables", [])]
    return {"rows_compared": sum(f.get("source_rows", 0) for f in fps),
            "rows_changed": sum(f.get("mismatched", 0) for f in fps),
            "rows_known_limitation": sum(f.get("known_limitation_rows", 0) for f in fps)}


def demo_summary(report: Optional[dict], plan_name: Optional[str] = None) -> dict:
    """Headline numbers for one plan's side of the comparison."""
    if not report:
        return {}
    s = report.get("summary", {})
    drift = money_drift(report)
    lost = empty_strings_lost(report)
    queries = query_results(report, plan_name)
    statuses = [q.get("status") for q in queries]
    return {
        "overall": overall_status(report),
        "tables": s.get("tables", 0), "passed": s.get("passed", 0),
        "warned": s.get("warned", 0), "failed": s.get("failed", 0),
        "known_limitations": known_limitation_kinds(report),
        "money_columns": len(drift),
        "money_columns_drifting": sum(r["status"] == "FAIL" for r in drift),
        "money_abs_drift": format(sum((abs(_dec(r["drift"])) for r in drift
                                       if r["drift"] is not None), Decimal(0)), "f"),
        "empty_strings_lost": sum(r["lost"] for r in lost),
        "type_downgrades": len(type_downgrades(report)),
        **fingerprint_totals(report),
        "queries": len(queries), "queries_passed": statuses.count("PASS"),
        "queries_failed": statuses.count("FAIL"), "queries_not_run": statuses.count("NOT_RUN"),
    }


def comparison_rows(agent: dict, naive: dict) -> list[dict]:
    """Rows of the agent-vs-naive table: metric, display values and good/bad flags."""
    def tables(s):
        return f"{s['passed']} passed · {s['warned']} warned · {s['failed']} failed"

    def money(s):
        n = s["money_columns_drifting"]
        return f"{n} of {s['money_columns']} columns" if n == 0 else \
            f"{n} of {s['money_columns']} columns (total |drift| {s['money_abs_drift']})"

    def changed(s):
        text = f"{s['rows_changed']:,} of {s['rows_compared']:,} rows"
        if s["rows_known_limitation"]:
            text += f" ({s['rows_known_limitation']:,} more differ only by known limitations)"
        return text

    def queries(s):
        return f"{s['queries_passed']} of {s['queries']} identical"

    def q_ok(s):
        return s["queries"] > 0 and s["queries_passed"] == s["queries"]

    specs = [
        ("Tables passed · warned · failed", tables, lambda s: s["failed"] == 0),
        ("Money columns with SUM drift", money, lambda s: s["money_columns_drifting"] == 0),
        ("Empty strings lost ('' → NULL)", lambda s: f"{s['empty_strings_lost']:,}",
         lambda s: s["empty_strings_lost"] == 0),
        ("Type downgrades", lambda s: f"{s['type_downgrades']}",
         lambda s: s["type_downgrades"] == 0),
        ("Rows with changed values", changed, lambda s: s["rows_changed"] == 0),
        ("Business queries identical", queries, q_ok),
    ]
    rows = []
    for label, fmt, ok in specs:
        rows.append({"metric": label,
                     "agent": fmt(agent) if agent else "-", "agent_ok": ok(agent) if agent else None,
                     "naive": fmt(naive) if naive else "-", "naive_ok": ok(naive) if naive else None})
    return rows


def headline(s: dict) -> str:
    """One-line verdict under a plan's badge."""
    if not s:
        return "no report yet"
    n = len(s["known_limitations"])
    fails = f"{s['failed']} failure{'s' if s['failed'] != 1 else ''}"
    if n == 0:
        return fails
    which = "both" if n == 2 else "all" if n > 2 else "it"
    noun = "limitation" if n == 1 else "limitations"
    return f"{fails} · {n} known BigQuery {noun}, {which} detected and explained"


# ---------------------------------------------------------------- migration log

def migration_frame(log: Optional[dict], plan_name: str) -> pd.DataFrame:
    """Per-table migration results from migration_log.json."""
    tables = (log or {}).get(plan_name, {}).get("tables", {})
    rows = [{"table": e["target_table"], "rows extracted": e["rows_extracted"],
             "rows loaded": e["rows_loaded"], "seconds": e["seconds"],
             "format": e.get("load_format"), "partitioning": e["partitioning"],
             "clustering": ", ".join(e.get("clustering_columns") or []) or "-",
             "NULL arrays → []": e.get("null_arrays_loaded_as_empty", 0),
             "ok": e.get("ok"), "migrated at": e.get("migrated_at")}
            for e in tables.values()]
    return pd.DataFrame(rows)

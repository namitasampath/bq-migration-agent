"""Translate legacy Postgres reporting queries to BigQuery and prove result parity.

  python -m agent.translator --plan agent   -> agent_* tables -> translations_agent.json
  python -m agent.translator --plan naive   -> naive_* tables -> translations_naive.json
  optional: --queries currently_overdue_loans

Queries come from seed/business_queries.sql ("-- query: <name>" separators). Gemini
(temperature 0, JSON mode) translates each one, given the plan's column types. The JSON
is validated (pydantic + SELECT-only safety checks), then dry-run in BigQuery; a failing
translation gets ONE fix attempt with the error. The original runs on Postgres
(read-only), the translation on BigQuery, and results are compared deterministically:
same columns, same row count, sorted rows, exact Decimals, normalized dates/datetimes.
If Gemini is unavailable (quota exhausted stops all further Gemini calls), the last
translation that passed its dry run is reused and parity is re-checked; with nothing
to reuse the query is NOT_RUN. Saved translations are never replaced by an error.
A query_parity section is added to report_<plan>.json.
Exit code: 1 if any FAIL (tables or queries), 2 if a query is NOT_RUN, else 0.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError, field_validator

from agent.migrator import PLANS, open_source, read_bq_settings
from agent.planner import (GeminiQuotaExhausted, Plan, call_gemini, load_plan,
                           make_client, model_message,
                           target_columns, user_message)
from agent.profiler import to_json
from agent.validator import (FAIL, INFO, PASS, WARN, canonical_number,
                             dump_canonical_json)

ROOT = Path(__file__).resolve().parents[1]
QUERIES_PATH = ROOT / "seed" / "business_queries.sql"
QUERY_MARKER = re.compile(r"^--\s*query:\s*(\S+)\s*$", re.MULTILINE)
FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|EXPORT|CALL)\b",
    re.IGNORECASE)
MAX_EXAMPLES = 5
NOT_RUN = "NOT_RUN"
STATUS_RANK = {PASS: 0, INFO: 0, WARN: 1, NOT_RUN: 2, FAIL: 3}
EXIT_CODES = {FAIL: 1, NOT_RUN: 2}


# ---------------------------------------------------------------- inputs

def parse_queries(path: Path = QUERIES_PATH) -> list[tuple[str, str]]:
    """Split the SQL file into (name, sql) pairs on '-- query: <name>' lines."""
    text = path.read_text(encoding="utf-8")
    marks = list(QUERY_MARKER.finditer(text))
    queries = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip().rstrip(";").strip()
        queries.append((m.group(1), body))
    return queries


def schema_context(plan: Plan, project: str, dataset: str, prefix: str) -> str:
    """Describe every target table and column (source type -> BigQuery type, transform)."""
    lines = []
    for tp in plan.tables:
        lines.append(f"Postgres table {tp.source_table} -> "
                     f"`{project}.{dataset}.{prefix}{tp.target_table}`")
        for tc in target_columns(tp):
            note = " (exact original JSON text)" if tc.is_raw_companion else ""
            lines.append(f"  {tc.name}: {tc.source_type} -> {tc.bq_type}, "
                         f"transform={tc.transform}{note}")
    return "\n".join(lines)


# ---------------------------------------------------------------- Gemini translation

class Translation(BaseModel):
    """Gemini's structured translation of one query."""
    translated_sql: str
    dialect_changes: list[str]
    confidence: Literal["high", "medium", "low"]

    @field_validator("translated_sql")
    @classmethod
    def strip_sql(cls, v: str) -> str:
        return v.strip().rstrip(";").strip()


SYSTEM_PROMPT = """You translate PostgreSQL reporting queries into BigQuery GoogleSQL
(Standard SQL) that runs against migrated tables. Requirements:
- Return ONE read-only SELECT (or WITH ... SELECT) statement, no trailing semicolon.
- Use ONLY the fully-qualified backticked target tables given in the schema.
- Produce the SAME result as the original: same output column names in the same order,
  same rows, same ORDER BY. Keep values exact: NUMERIC stays NUMERIC (never FLOAT64)
  and cast numeric results to NUMERIC where the original casts to numeric; dates stay
  DATE, timestamps stay DATETIME/TIMESTAMP.
- Respect each column's BigQuery type and transform. If a column was stored as STRING or
  FLOAT64 or had empty strings converted to NULL, write the SQL that best reproduces the
  original semantics on that data (e.g. parse strings back to DATETIME, split strings).
- Postgres and BigQuery both use UTC for CURRENT_DATE.
- List every dialect change you made as short phrases (e.g. "ILIKE -> LOWER() LIKE").
- confidence: high / medium / low that results are identical."""


def validate_translation(t: Translation, allowed_prefix: str) -> list[str]:
    """Deterministic safety/shape checks before the SQL is ever sent to BigQuery."""
    errors = []
    sql_text = t.translated_sql
    if not re.match(r"^\s*(SELECT|WITH)\b", sql_text, re.IGNORECASE):
        errors.append("translated_sql must start with SELECT or WITH")
    if ";" in sql_text:
        errors.append("translated_sql must be a single statement (no ';')")
    if FORBIDDEN_SQL.search(re.sub(r"'[^']*'", "''", sql_text)):
        errors.append("translated_sql must be read-only (DML/DDL keyword found)")
    tables = re.findall(r"`([^`]+)`", sql_text)
    bad = [x for x in tables if x.count(".") == 2 and not x.startswith(allowed_prefix)]
    if bad:
        errors.append(f"only tables under {allowed_prefix}* are allowed, found {bad}")
    if not any(x.startswith(allowed_prefix) for x in tables):
        errors.append(f"must reference tables as `{allowed_prefix}<table>`")
    return errors


def ask_gemini(client, model: str, contents: list, allowed_prefix: str
               ) -> tuple[Optional[Translation], str, list[str]]:
    """One Gemini call; return (translation or None, raw text, validation errors)."""
    text = call_gemini(client, model, contents, SYSTEM_PROMPT, Translation)
    try:
        t = Translation.model_validate_json(text)
    except ValidationError as e:
        return None, text, [f"invalid JSON response: {err['msg']}" for err in e.errors()]
    return t, text, validate_translation(t, allowed_prefix)


def dry_run(bq, sql_text: str) -> tuple[bool, Optional[str], Optional[int]]:
    """BigQuery dry run: (ok, error message, bytes that would be processed)."""
    from google.cloud import bigquery
    try:
        job = bq.query(sql_text, job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False))
        return True, None, job.total_bytes_processed
    except Exception as e:  # BadRequest etc. -> feed the message back to Gemini
        return False, getattr(e, "message", None) or str(e), None


def translate(client, model: str, bq, name: str, original: str, context: str,
              allowed_prefix: str) -> dict:
    """Translate + validate + dry-run, with one fix round on any failure."""
    prompt = (f"Target schema:\n{context}\n\nTranslate this PostgreSQL query "
              f"'{name}':\n{original}")
    contents = [user_message(prompt)]
    attempts = []
    for attempt in (1, 2):
        t, raw, errors = ask_gemini(client, model, contents, allowed_prefix)
        ok, dry_error, nbytes = (False, None, None)
        if t and not errors:
            ok, dry_error, nbytes = dry_run(bq, t.translated_sql)
            if not ok:
                errors = [f"BigQuery dry run failed: {dry_error}"]
        attempts.append({"attempt": attempt, "errors": errors, "dry_run_ok": ok,
                         "bytes_processed": nbytes})
        if ok:
            return {"translation": t, "attempts": attempts}
        contents = contents + [model_message(raw), user_message(
            "Your translation failed:\n- " + "\n- ".join(errors)
            + "\nReturn the corrected translation.")]
    return {"translation": t, "attempts": attempts}


# ---------------------------------------------------------------- execution + parity

def run_postgres(cur, sql_text: str) -> tuple[list[str], list[tuple]]:
    cur.execute(sql_text)
    return [d.name for d in cur.description], cur.fetchall()


def run_bigquery(bq, sql_text: str) -> tuple[list[str], list[tuple]]:
    result = bq.query(sql_text).result()
    return [f.name for f in result.schema], [tuple(r.values()) for r in result]


def normalize(v):
    """Comparable form: exact Decimal strings, ISO dates/datetimes, canonical JSON."""
    if v is None or isinstance(v, (str, bool)):
        return v
    if isinstance(v, (int, float, Decimal)):
        return canonical_number(v, None)
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.isoformat(timespec="microseconds")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, (list, dict)):
        return dump_canonical_json(v)
    return str(v)


def sort_key(row: tuple) -> tuple:
    return tuple((v is None, "" if v is None else str(v)) for v in row)


def compare_results(src_cols, src_rows, tgt_cols, tgt_rows) -> dict:
    """Deterministic parity: columns, row count, sorted normalized rows."""
    out = {"source_columns": src_cols, "target_columns": tgt_cols,
           "source_rows": len(src_rows), "target_rows": len(tgt_rows),
           "matching_rows": 0, "examples": []}
    problems = []
    if [c.lower() for c in src_cols] != [c.lower() for c in tgt_cols]:
        problems.append("column names/order differ")
    if len(src_rows) != len(tgt_rows):
        problems.append(f"row count {len(src_rows)} != {len(tgt_rows)}")
    src = sorted((tuple(normalize(v) for v in r) for r in src_rows), key=sort_key)
    tgt = sorted((tuple(normalize(v) for v in r) for r in tgt_rows), key=sort_key)
    s_count, t_count = Counter(src), Counter(tgt)
    only_src, only_tgt = s_count - t_count, t_count - s_count
    out["matching_rows"] = sum((s_count & t_count).values())
    out["source_only_rows"] = sum(only_src.values())
    out["target_only_rows"] = sum(only_tgt.values())
    if only_src or only_tgt:
        problems.append(f"{out['source_only_rows']} source rows and "
                        f"{out['target_only_rows']} target rows have no exact match")
    out["examples"] = diff_examples(src_cols, tgt_cols, sorted(only_src.elements(), key=sort_key),
                                    sorted(only_tgt.elements(), key=sort_key))
    out["status"] = FAIL if problems else PASS
    out["detail"] = "; ".join(problems) or f"all {len(src_rows)} rows identical"
    return out


def diff_examples(src_cols, tgt_cols, only_src: list, only_tgt: list) -> list[dict]:
    """Pair up unmatched rows (sorted) and show which columns differ."""
    examples = []
    for i in range(min(MAX_EXAMPLES, max(len(only_src), len(only_tgt)))):
        s = dict(zip(src_cols, only_src[i])) if i < len(only_src) else None
        t = dict(zip(tgt_cols, only_tgt[i])) if i < len(only_tgt) else None
        differing = ([c for c in src_cols if s.get(c) != t.get(c)] if s and t else None)
        examples.append({"source_row": s, "target_row": t, "differing_columns": differing})
    return examples


# ---------------------------------------------------------------- per query

def load_previous(plan_name: str) -> dict[str, dict]:
    """Entries from the last translations_<plan>.json, by query name."""
    path = ROOT / f"translations_{plan_name}.json"
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {q["name"]: q for q in doc.get("queries", []) if "name" in q}


def reusable_sql(previous: Optional[dict]) -> Optional[str]:
    """A previous translation that passed its BigQuery dry run, if any."""
    if not previous or not previous.get("translated_sql"):
        return None
    attempts = previous.get("attempts") or []
    return previous["translated_sql"] if attempts and attempts[-1].get("dry_run_ok") else None


def get_translation(client, model, bq, name, original, context, prefix, gemini: dict
                    ) -> tuple[Optional[dict], Optional[str]]:
    """Fresh Gemini translation, or (None, reason) when Gemini is unavailable.

    gemini["blocked"] is set after a quota error so later queries skip Gemini entirely.
    """
    if gemini.get("blocked"):
        return None, f"{gemini['blocked']} (Gemini skipped for remaining queries)"
    try:
        return translate(client, model, bq, name, original, context, prefix), None
    except GeminiQuotaExhausted as e:
        gemini["blocked"] = str(e)
        return None, str(e)
    except Exception as e:
        return None, f"Gemini error: {type(e).__name__}: {e}"


def process_query(client, model, bq, cur, name, original, context, prefix,
                  previous: Optional[dict], gemini: dict) -> dict:
    """Translate (or reuse the last good translation), run both sides, compare."""
    entry = {"name": name, "original_sql": original}
    tr, gemini_error = get_translation(client, model, bq, name, original, context, prefix,
                                       gemini)
    if tr is None:
        sql_text = reusable_sql(previous)
        if sql_text is None:
            return {**entry, "status": NOT_RUN, "gemini_error": gemini_error,
                    "detail": f"no translation available: {gemini_error}"}
        entry.update({k: previous.get(k) for k in ("translated_sql", "dialect_changes",
                                                    "confidence", "attempts", "translated_at")})
        entry.update({"translation_source": "previous_run", "gemini_error": gemini_error})
        return run_and_compare(entry, cur, bq, original, sql_text)
    t = tr["translation"]
    entry.update({"translated_sql": t.translated_sql if t else None,
                  "dialect_changes": t.dialect_changes if t else [],
                  "confidence": t.confidence if t else None,
                  "attempts": tr["attempts"], "translation_source": "gemini",
                  "translated_at": datetime.now().replace(microsecond=0).isoformat()})
    if not tr["attempts"][-1]["dry_run_ok"]:
        return {**entry, "status": FAIL,
                "detail": "translation invalid after one fix: "
                          + "; ".join(tr["attempts"][-1]["errors"])}
    return run_and_compare(entry, cur, bq, original, t.translated_sql)


def run_and_compare(entry: dict, cur, bq, original: str, sql_text: str) -> dict:
    """Run the original on Postgres and the translation on BigQuery; compare results."""
    try:
        src_cols, src_rows = run_postgres(cur, original)
    except Exception as e:
        cur.connection.rollback()
        return {**entry, "status": FAIL, "detail": f"Postgres error: {e}"}
    try:
        tgt_cols, tgt_rows = run_bigquery(bq, sql_text)
    except Exception as e:
        return {**entry, "status": FAIL, "detail": f"BigQuery error: {e}"}
    parity = compare_results(src_cols, src_rows, tgt_cols, tgt_rows)
    return {**entry, "status": parity["status"], "detail": parity["detail"], "parity": parity}


# ---------------------------------------------------------------- outputs

def overall(statuses) -> str:
    """Worst status; NOT_RUN ranks above WARN (unproven) and below FAIL."""
    statuses = [s for s in statuses if s]
    return max(statuses, key=lambda s: STATUS_RANK[s]) if statuses else PASS


def parity_section(results: list[dict], model: str) -> dict:
    """query_parity section for report_<plan>.json (statuses are deterministic)."""
    statuses = [r["status"] for r in results]
    return {"model_used_for_translation": model,
            "note": "SQL was translated by Gemini; PASS/FAIL comes only from deterministic "
                    "result comparison. NOT_RUN = no translation available (Gemini "
                    "unavailable and no previous translation to reuse).",
            "summary": {"queries": len(results), "passed": statuses.count(PASS),
                        "failed": statuses.count(FAIL), "not_run": statuses.count(NOT_RUN),
                        "overall": overall(statuses)},
            "queries": [{k: r.get(k) for k in ("name", "status", "detail", "confidence",
                                               "dialect_changes", "translation_source",
                                               "gemini_error")}
                        | {"examples": r.get("parity", {}).get("examples", [])}
                        for r in results]}


def update_report(plan_name: str, section: dict) -> dict:
    """Add/replace query_parity in report_<plan>.json, keeping everything else."""
    path = ROOT / f"report_{plan_name}.json"
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"plan": plan_name}
    report["query_parity"] = section
    tables_overall = report.get("summary", {}).get("overall")
    report["overall_status"] = overall([tables_overall, section["summary"]["overall"]])
    path.write_text(to_json(report), encoding="utf-8")
    return report


def format_result(r: dict) -> str:
    source = ""
    if r.get("translation_source") == "previous_run":
        source = ", reused previous translation (Gemini unavailable)"
    lines = [f"\n{r['name']:<34} {r['status']}  (confidence: {r.get('confidence') or '-'}, "
             f"attempts: {len(r.get('attempts') or [])}{source})"]
    for change in r.get("dialect_changes") or []:
        lines.append(f"    - {change}")
    lines.append(f"    result: {r['detail']}")
    for ex in r.get("parity", {}).get("examples", [])[:3]:
        cols = ex["differing_columns"]
        if cols:
            pairs = ", ".join(f"{c}: {ex['source_row'][c]!r} -> {ex['target_row'].get(c)!r}"
                              for c in cols[:3])
            lines.append(f"      e.g. {pairs}")
        else:
            lines.append(f"      e.g. source {ex['source_row']} / target {ex['target_row']}")
    return "\n".join(lines)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Translate business queries and check parity.")
    p.add_argument("--plan", choices=sorted(PLANS), default="agent")
    p.add_argument("--queries", nargs="+", help="only these query names")
    return p.parse_args(argv)


def merge_translations(plan_name: str, results: list[dict], model: str
                       ) -> tuple[Path, list[dict]]:
    """Write translations_<plan>.json; queries not in this run keep their old entries.

    Returns the file path and the merged entries (latest result for every query).
    """
    merged = load_previous(plan_name)
    merged.update({r["name"]: r for r in results})
    out = ROOT / f"translations_{plan_name}.json"
    out.write_text(to_json({"plan": plan_name, "model": model,
                            "generated_at": datetime.now().replace(microsecond=0),
                            "queries": list(merged.values())}), encoding="utf-8")
    return out, list(merged.values())


def main(argv=None) -> int:
    from google.cloud import bigquery
    args = parse_args(argv)
    settings = read_bq_settings()
    plan = load_plan(PLANS[args.plan])
    prefix = f"{args.plan}_"
    allowed = f"{settings['project']}.{settings['dataset']}.{prefix}"
    queries = parse_queries()
    if args.queries:
        queries = [q for q in queries if q[0] in args.queries]
    context = schema_context(plan, settings["project"], settings["dataset"], prefix)
    client, model = make_client(), os.environ["GEMINI_MODEL"]
    bq = bigquery.Client(project=settings["project"])
    previous, gemini = load_previous(args.plan), {}
    conn = open_source()
    try:
        with conn.cursor() as cur:
            results = [process_query(client, model, bq, cur, name, sql_text, context, allowed,
                                     previous.get(name), gemini)
                       for name, sql_text in queries]
    finally:
        conn.close()
    out, merged = merge_translations(args.plan, results, model)
    report = update_report(args.plan, parity_section(merged, model))  # all queries
    qp = report["query_parity"]["summary"]
    print(f"Query translation for plan '{args.plan}' ({settings['dataset']}, {model})")
    if gemini.get("blocked"):
        print(f"  NOTE: {gemini['blocked']}; stopped calling Gemini for this run")
    for r in results:
        print(format_result(r))
    print(f"\nQUERY PARITY: {qp['queries']} queries: {qp['passed']} passed, "
          f"{qp['failed']} failed, {qp['not_run']} not run -> {qp['overall']}")
    print(f"OVERALL (tables + queries): {report['overall_status']}")
    print(f"Saved {out.name}; added query_parity to report_{args.plan}.json")
    return EXIT_CODES.get(report["overall_status"], 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())

"""Profile the legacy Postgres database (PG_* settings from .env) into profile.json.

For every base table in the public schema: exact column types, primary key,
foreign keys, row count, and per-column stats (nulls, empty strings, distinct,
min/max, decimal places actually used, sub-second timestamps). All stats are
computed in SQL with one aggregate scan per table; nothing is loaded into pandas.

Run from the project root:  python -m agent.profiler [output_path]
"""
import json
import os
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "profile.json"
FORBIDDEN_PORT = 5432  # unrelated Postgres on this machine; never touch it

TEXT_TYPES = {"text", "varchar", "bpchar", "citext", "name"}
NUMBER_TYPES = {"int2", "int4", "int8", "numeric", "float4", "float8", "money"}
TEMPORAL_TYPES = {"date", "timestamp", "timestamptz"}
TIMESTAMP_TYPES = {"timestamp", "timestamptz"}
NO_EQUALITY_TYPES = {"json", "xml", "point", "polygon", "box", "path", "circle", "line", "lseg"}


# ---------------------------------------------------------------- settings / db

def read_pg_settings() -> dict:
    """Read PG_* settings from .env and refuse to target the forbidden port."""
    load_dotenv(ROOT / ".env")
    keys = ["PG_HOST", "PG_PORT", "PG_USER", "PG_PASSWORD", "PG_DB"]
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing settings in .env: {', '.join(missing)}")
    port = int(os.environ["PG_PORT"])
    if port == FORBIDDEN_PORT:
        sys.exit(f"PG_PORT={port} is the unrelated Postgres. Refusing to connect.")
    return {"host": os.environ["PG_HOST"], "port": port, "user": os.environ["PG_USER"],
            "password": os.environ["PG_PASSWORD"], "dbname": os.environ["PG_DB"]}


def connect():
    """Open a read-only psycopg2 connection to the legacy database."""
    conn = psycopg2.connect(**read_pg_settings())
    conn.set_session(readonly=True)
    return conn


# ---------------------------------------------------------------- JSON encoding

class ProfileEncoder(json.JSONEncoder):
    """Encode Decimal and date/time values as exact strings, never floats."""

    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, (datetime, date, time)):
            return o.isoformat()
        return super().default(o)


def to_json(profile: dict) -> str:
    """Serialize a profile dict with the exact-value encoder."""
    return json.dumps(profile, cls=ProfileEncoder, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- schema metadata

def list_tables(cur) -> list[str]:
    """Return the base tables of the public schema, sorted by name."""
    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    return [r[0] for r in cur.fetchall()]


def get_columns(cur, table: str) -> list[dict]:
    """Return columns with exact Postgres types (precision/scale, arrays, json)."""
    cur.execute("""
        SELECT a.attname,
               format_type(a.atttypid, a.atttypmod),
               t.typname,
               t.typcategory = 'A',
               et.typname,
               NOT a.attnotnull,
               c.numeric_precision,
               c.numeric_scale,
               c.character_maximum_length,
               c.column_default
        FROM pg_attribute a
        JOIN pg_class r       ON r.oid = a.attrelid
        JOIN pg_namespace n   ON n.oid = r.relnamespace
        JOIN pg_type t        ON t.oid = a.atttypid
        LEFT JOIN pg_type et  ON et.oid = t.typelem AND t.typcategory = 'A'
        LEFT JOIN information_schema.columns c
               ON c.table_schema = n.nspname AND c.table_name = r.relname
              AND c.column_name = a.attname
        WHERE n.nspname = 'public' AND r.relname = %s
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
    """, (table,))
    columns = []
    for (name, full_type, udt, is_array, elem, nullable,
         precision, scale, max_len, default) in cur.fetchall():
        columns.append({
            "name": name,
            "type": full_type,          # e.g. numeric(14,4), text[], jsonb
            "udt": udt,                 # e.g. numeric, _text, jsonb
            "is_array": is_array,
            "element_type": elem,       # e.g. text for text[], else None
            "is_json": udt in ("json", "jsonb"),
            "nullable": nullable,
            "numeric_precision": precision if udt == "numeric" else None,
            "numeric_scale": scale if udt == "numeric" else None,
            "char_max_length": max_len,
            "default": default,
        })
    return columns


def get_primary_key(cur, table: str) -> list[str]:
    """Return primary key column names in key order (empty list if none)."""
    cur.execute("""
        SELECT a.attname
        FROM pg_index i
        CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
        WHERE i.indisprimary AND i.indrelid = %s::regclass
        ORDER BY k.ord
    """, (f'public."{table}"',))
    return [r[0] for r in cur.fetchall()]


def get_foreign_keys(cur, table: str) -> list[dict]:
    """Return foreign keys as {name, columns, ref_table, ref_columns}."""
    cur.execute("""
        SELECT con.conname, a.attname, rt.relname, ra.attname
        FROM pg_constraint con
        JOIN pg_class t      ON t.oid = con.conrelid
        JOIN pg_namespace n  ON n.oid = t.relnamespace
        JOIN pg_class rt     ON rt.oid = con.confrelid
        CROSS JOIN LATERAL unnest(con.conkey, con.confkey)
             WITH ORDINALITY AS k(attnum, ref_attnum, ord)
        JOIN pg_attribute a  ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = k.ref_attnum
        WHERE con.contype = 'f' AND n.nspname = 'public' AND t.relname = %s
        ORDER BY con.conname, k.ord
    """, (table,))
    fks: dict[str, dict] = {}
    for name, col, ref_table, ref_col in cur.fetchall():
        fk = fks.setdefault(name, {"name": name, "columns": [],
                                   "ref_table": ref_table, "ref_columns": []})
        fk["columns"].append(col)
        fk["ref_columns"].append(ref_col)
    return list(fks.values())


# ---------------------------------------------------------------- column stats

def stat_expressions(col: dict) -> list[tuple[str, sql.Composable]]:
    """Return (stat_name, SQL aggregate) pairs that apply to this column's type."""
    c = sql.Identifier(col["name"])
    udt = col["udt"]
    distinct_target = sql.SQL("{}::text").format(c) if udt in NO_EQUALITY_TYPES else c
    exprs = [
        ("null_count", sql.SQL("count(*) - count({})").format(c)),
        ("distinct_count", sql.SQL("count(DISTINCT {})").format(distinct_target)),
    ]
    if udt in TEXT_TYPES:
        exprs.append(("empty_string_count",
                      sql.SQL("count(*) FILTER (WHERE {} = '')").format(c)))
    if udt in NUMBER_TYPES or udt in TEMPORAL_TYPES:
        exprs.append(("min", sql.SQL("min({})").format(c)))
        exprs.append(("max", sql.SQL("max({})").format(c)))
    if udt == "numeric":
        # Digits after the point once trailing zeros are stripped: 12.3400 -> 2.
        exprs.append(("max_decimal_places_used", sql.SQL(
            "max(length(rtrim(split_part({}::text, '.', 2), '0')))").format(c)))
    if udt in TIMESTAMP_TYPES:
        exprs.append(("rows_with_microseconds", sql.SQL(
            "count(*) FILTER (WHERE {c} <> date_trunc('second', {c}))").format(c=c)))
    return exprs


def build_stats_query(table: str, columns: list[dict]) -> tuple[sql.Composed, list]:
    """Build one aggregate query for the whole table; also return result keys."""
    selects = [sql.SQL("count(*)")]
    keys: list = [("__table__", "row_count")]
    for col in columns:
        for stat, expr in stat_expressions(col):
            selects.append(expr)
            keys.append((col["name"], stat))
    query = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(selects), sql.Identifier("public", table))
    return query, keys


def profile_stats(cur, table: str, columns: list[dict]) -> tuple[int, dict]:
    """Run the aggregate scan; return (row_count, {column: {stat: value}})."""
    query, keys = build_stats_query(table, columns)
    cur.execute(query)
    values = cur.fetchone()
    row_count = 0
    stats: dict[str, dict] = {col["name"]: {} for col in columns}
    for (col_name, stat), value in zip(keys, values):
        if col_name == "__table__":
            row_count = value
        else:
            stats[col_name][stat] = value
    for col_stats in stats.values():
        if "rows_with_microseconds" in col_stats:
            col_stats["has_nonzero_microseconds"] = col_stats["rows_with_microseconds"] > 0
    return row_count, stats


# ---------------------------------------------------------------- orchestration

def profile_table(cur, table: str) -> dict:
    """Collect schema, keys and stats for one table."""
    columns = get_columns(cur, table)
    row_count, stats = profile_stats(cur, table, columns)
    for col in columns:
        col["stats"] = stats[col["name"]]
    return {
        "name": table,
        "row_count": row_count,
        "primary_key": get_primary_key(cur, table),
        "foreign_keys": get_foreign_keys(cur, table),
        "columns": columns,
    }


def profile_database(conn) -> dict:
    """Profile every base table in the public schema."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), current_setting('server_version')")
        db_name, version = cur.fetchone()
        tables = [profile_table(cur, t) for t in list_tables(cur)]
    return {
        "database": db_name,
        "server_version": version,
        "schema": "public",
        "profiled_at": datetime.now().replace(microsecond=0),
        "tables": tables,
    }


def save_profile(profile: dict, path: Path) -> None:
    """Write the profile to disk as UTF-8 JSON."""
    path.write_text(to_json(profile), encoding="utf-8")


# ---------------------------------------------------------------- summary

def summarize_column(col: dict) -> str:
    """One readable line of notable facts about a column."""
    s = col["stats"]
    parts = [f"nulls={s['null_count']}", f"distinct={s['distinct_count']}"]
    if s.get("empty_string_count"):
        parts.append(f"empty=''x{s['empty_string_count']}")
    if "min" in s:
        parts.append(f"range=[{s['min']} .. {s['max']}]")
    if s.get("max_decimal_places_used") is not None:
        parts.append(f"dp_used={s['max_decimal_places_used']}/{col['numeric_scale']}")
    if s.get("has_nonzero_microseconds"):
        parts.append(f"micros_rows={s['rows_with_microseconds']}")
    return f"    {col['name']:<18} {col['type']:<28} " + "  ".join(parts)


def summarize_table(table: dict) -> str:
    """Readable multi-line summary of one table profile."""
    lines = [f"\n{table['name']}  ({table['row_count']} rows)  "
             f"PK: {', '.join(table['primary_key']) or '-'}"]
    for fk in table["foreign_keys"]:
        lines.append(f"  FK {', '.join(fk['columns'])} -> "
                     f"{fk['ref_table']}({', '.join(fk['ref_columns'])})")
    lines.extend(summarize_column(c) for c in table["columns"])
    return "\n".join(lines)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    conn = connect()
    try:
        profile = profile_database(conn)
    finally:
        conn.close()
    print(f"Database {profile['database']} (Postgres {profile['server_version']})")
    for table in profile["tables"]:
        print(summarize_table(table))
    save_profile(profile, out)
    print(f"\nSaved {len(profile['tables'])} table profiles to {out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

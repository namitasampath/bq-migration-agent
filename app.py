"""Streamlit UI for the BigQuery Migration Agent: profile -> plan & approve -> migrate -> prove.

Run from the project root:  streamlit run app.py

The app only wraps the agent modules (profiler, planner, migrator, validator,
translator) and reads their JSON artifacts through agent.compare; it contains no
migration or validation logic of its own. "Load last results" mode never touches
Postgres, BigQuery or Gemini; it only reads the saved files.
"""
import contextlib
import io
import os
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from agent import compare
from agent.compare import ROOT

load_dotenv(ROOT / ".env")

STATUS_COLORS = {"PASS": "#1a7f37", "WARN": "#b26b00", "FAIL": "#cf222e",
                 "INFO": "#57606a", "NOT_RUN": "#6e40c9", "": "transparent"}
PAGES = ["Compare agent vs naive", "1 · Profile source", "2 · Plan & approve",
         "3 · Migrate", "4 · Validate & prove"]
LIVE, LOAD = "Live (run steps)", "Load last results"

CSS = """
<style>
.block-container {padding-top: 2.2rem; max-width: 1500px;}
.badge {display:inline-block; padding:2px 10px; border-radius:999px; color:#fff;
        font-weight:600; font-size:0.8rem; letter-spacing:.03em;}
.big-badge {display:inline-block; padding:10px 28px; border-radius:12px; color:#fff;
            font-weight:700; font-size:2.1rem; letter-spacing:.06em;}
.stepper {display:flex; gap:10px; margin:0 0 1.2rem 0;}
.step {flex:1; border:1px solid rgba(128,128,128,.25); border-radius:10px;
       padding:10px 14px; background:rgba(128,128,128,.04);}
.step.current {border:2px solid #0969da;}
.step .num {font-weight:700; margin-right:6px;}
.step .state {font-size:.78rem; opacity:.8; margin-top:2px;}
.verdict {margin:.5rem 0 1rem 0; font-size:.95rem; opacity:.85;}
table.cmp {width:100%; border-collapse:collapse; margin:.5rem 0 1rem 0; font-size:1rem;}
table.cmp th, table.cmp td {padding:12px 16px; border-bottom:1px solid rgba(128,128,128,.2);
                            text-align:left;}
table.cmp thead th {font-size:.8rem; text-transform:uppercase; letter-spacing:.05em;
                    opacity:.7; border-bottom:2px solid rgba(128,128,128,.35);}
table.cmp tbody th {font-weight:500; width:30%;}
table.cmp td {width:35%;}
</style>
"""


# ---------------------------------------------------------------- small UI helpers

def badge(status: str, big: bool = False) -> str:
    cls = "big-badge" if big else "badge"
    return f'<span class="{cls}" style="background:{STATUS_COLORS.get(status, "#57606a")}">' \
           f'{status}</span>'


def status_style(value) -> str:
    color = STATUS_COLORS.get(str(value))
    if not color or color == "transparent":
        return ""
    return f"background-color:{color}26; color:{color}; font-weight:600"


def styled(df: pd.DataFrame, columns=None):
    """Colour PASS/WARN/FAIL cells (whole frame, or only the given columns)."""
    if df.empty:
        return df
    return df.style.map(status_style, subset=columns) if columns else df.style.map(status_style)


def capture(fn, *args):
    """Run a module entry point, capturing its console output for a log expander."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args)
    return result, buf.getvalue()


def live() -> bool:
    return st.session_state.get("mode") == LIVE


def action_button(label: str, key: str, disabled: bool = False, help_text: str = "",
                  primary: bool = False) -> bool:
    """Button that is disabled in Load mode (with an explanation)."""
    if not live():
        help_text = "Switch to Live mode in the sidebar to run this step."
    return st.button(label, key=key, disabled=disabled or not live(), help=help_text or None,
                     type="primary" if primary else "secondary")


# ---------------------------------------------------------------- stepper + sidebar

def stage_states() -> list[tuple[str, str]]:
    """(label, state) for the four stages, derived from the saved artifacts."""
    from agent.planner import approval_info
    log = compare.load("migration_log") or {}
    approved = approval_info()
    proposed = compare.FILES["plan_proposed"].exists()
    report = compare.load("report_agent")
    return [
        ("Profile source", "done" if compare.FILES["profile"].exists() else "to do"),
        ("Plan & approve", f"approved {approved}" if approved
         else "proposal awaiting approval" if proposed else "no approved plan"),
        ("Migrate", "done: " + ", ".join(p for p in ("agent", "naive") if log.get(p))
         if log else "to do"),
        ("Validate & prove", f"agent: {compare.overall_status(report)}" if report else "to do"),
    ]


def render_stepper(current: int) -> None:
    items = []
    for i, (label, state) in enumerate(stage_states(), start=1):
        cls = "step current" if i == current else "step"
        items.append(f'<div class="{cls}"><span class="num">{i}</span>{label}'
                     f'<div class="state">{state}</div></div>')
    st.markdown(f'<div class="stepper">{"".join(items)}</div>', unsafe_allow_html=True)


def render_sidebar() -> str:
    with st.sidebar:
        st.markdown("### BigQuery Migration Agent")
        st.caption("Postgres → BigQuery, with proof.")
        st.radio("Mode", [LOAD, LIVE], key="mode",
                 help="Load last results only reads the saved JSON files: instant, no "
                      "database, BigQuery or Gemini calls.")
        page = st.radio("View", PAGES, key="page", label_visibility="collapsed")
        st.divider()
        info = {
            "GCP project": os.getenv("GCP_PROJECT", "-"),
            "BigQuery dataset": os.getenv("BQ_DATASET", "-"),
            "Sandbox mode": os.getenv("BQ_SANDBOX", "false"),
            "Gemini model": os.getenv("GEMINI_MODEL", "-"),
            "Gemini cache": os.getenv("GEMINI_CACHE", "true"),
            "Source": f"{os.getenv('PG_HOST', '-')}:{os.getenv('PG_PORT', '-')}/"
                      f"{os.getenv('PG_DB', '-')}",
        }
        for k, v in info.items():
            st.markdown(f"<small>{k}</small><br><code>{v}</code>", unsafe_allow_html=True)
        st.divider()
        st.caption("Artifacts (last modified)")
        for name in ("profile", "plan", "migration_log", "report_agent", "report_naive"):
            ts = compare.modified_at(name)
            when = datetime.fromtimestamp(ts).strftime("%d %b %H:%M") if ts else "missing"
            st.markdown(f"<small>{compare.FILES[name].name}: {when}</small>",
                        unsafe_allow_html=True)
    return page


# ---------------------------------------------------------------- compare view

def plan_header(name: str, s: dict) -> None:
    """Plan name, big status badge and a one-line verdict."""
    st.markdown(f"#### {name} plan", unsafe_allow_html=True)
    st.markdown(badge(s.get("overall", "NOT_RUN") if s else "NOT_RUN", big=True),
                unsafe_allow_html=True)
    st.markdown(f'<div class="verdict">{compare.headline(s)}</div>', unsafe_allow_html=True)


def comparison_table_html(rows: list[dict]) -> str:
    """Full-width metric table, Agent | Naive cells coloured green (good) / red (bad)."""
    def cell(value: str, ok) -> str:
        if ok is None:
            return f"<td>{value}</td>"
        color = STATUS_COLORS["PASS" if ok else "FAIL"]
        return (f'<td style="color:{color}; background:{color}14; font-weight:600">'
                f'{"✓" if ok else "✗"}&nbsp; {value}</td>')
    body = "".join(f"<tr><th>{r['metric']}</th>{cell(r['agent'], r['agent_ok'])}"
                   f"{cell(r['naive'], r['naive_ok'])}</tr>" for r in rows)
    return ('<table class="cmp"><thead><tr><th>Metric</th><th>Agent plan</th>'
            f'<th>Naive plan</th></tr></thead><tbody>{body}</tbody></table>')


def merged(agent_rows: list[dict], naive_rows: list[dict], keys: list[str],
           value: str) -> pd.DataFrame:
    a = pd.DataFrame(agent_rows)
    n = pd.DataFrame(naive_rows)
    if a.empty or n.empty:
        return pd.DataFrame()
    return a[keys + [value, "status"]].merge(
        n[keys + [value, "status"]], on=keys, how="outer", suffixes=(" (agent)", " (naive)"))


def page_compare() -> None:
    st.title("Agent vs naive migration")
    st.caption("Same source database, same BigQuery dataset. One plan was designed by the "
               "agent and approved by a human; the other is a typical careless migration. "
               "Every number below comes from deterministic checks.")
    ra, rn = compare.load("report_agent"), compare.load("report_naive")
    sa, sn = compare.demo_summary(ra, "agent"), compare.demo_summary(rn, "naive")
    left, right = st.columns(2, gap="large")
    with left:
        plan_header("Agent", sa)
    with right:
        plan_header("Naive", sn)
    st.markdown(comparison_table_html(compare.comparison_rows(sa, sn)), unsafe_allow_html=True)
    if not (ra and rn):
        return
    st.divider()
    tabs = st.tabs(["Money totals", "Empty strings", "Type downgrades", "Business queries",
                    "Known limitations (agent)"])
    with tabs[0]:
        st.caption("Exact SUM per money column, compared as decimals with no tolerance.")
        df = merged(compare.money_drift(ra), compare.money_drift(rn), ["table", "column"],
                    "target sum")
        src = pd.DataFrame(compare.money_drift(ra))[["table", "column", "source sum"]]
        df = src.merge(df, on=["table", "column"])
        naive_drift = pd.DataFrame(compare.money_drift(rn))[["table", "column", "drift"]]
        df = df.merge(naive_drift.rename(columns={"drift": "naive drift"}), on=["table", "column"])
        st.dataframe(styled(df, ["status (agent)", "status (naive)"]), hide_index=True,
                     width="stretch")
    with tabs[1]:
        st.caption("Rows where Postgres stored an empty string '' (distinct from NULL).")
        df = merged(compare.empty_strings_lost(ra), compare.empty_strings_lost(rn),
                    ["table", "column"], "lost")
        src = pd.DataFrame(compare.empty_strings_lost(ra))[["table", "column", "source ''"]]
        st.dataframe(styled(src.merge(df, on=["table", "column"]),
                            ["status (agent)", "status (naive)"]),
                     hide_index=True, width="stretch")
    with tabs[2]:
        down = compare.type_downgrades(rn)
        st.caption(f"Agent: {len(compare.type_downgrades(ra))} downgrades. "
                   f"Naive: {len(down)} columns stored in a type that cannot represent the "
                   f"source exactly.")
        st.dataframe(pd.DataFrame(down), hide_index=True, width="stretch")
    with tabs[3]:
        render_query_comparison(ra, rn)
    with tabs[4]:
        render_known_limitations(ra)


def render_query_comparison(ra: dict, rn: dict) -> None:
    rows = {}
    for plan, report in (("agent", ra), ("naive", rn)):
        for q in compare.query_results(report, plan):
            rows.setdefault(q["name"], {"query": q["name"]})[plan] = q["status"]
            rows[q["name"]][f"{plan} detail"] = q.get("detail")
    if not rows:
        st.info("No query parity results yet. Run step 4 (translator).")
        return
    df = pd.DataFrame(rows.values())
    st.dataframe(styled(df, [c for c in ("agent", "naive") if c in df]), hide_index=True,
                 width="stretch")


def render_known_limitations(report: dict) -> None:
    lims = compare.known_limitations(report)
    if not lims:
        st.success("No known limitations recorded.")
        return
    st.caption("Accepted differences, each proven to be exactly the documented limitation "
               "(deterministic rules, not AI).")
    for text in dict.fromkeys(r["explanation"] for r in lims if r["explanation"]):
        st.info(text)
    st.dataframe(styled(pd.DataFrame(lims).drop(columns=["explanation"]), ["status"]),
                 hide_index=True, width="stretch")


# ---------------------------------------------------------------- stage 1: profile

def page_profile() -> None:
    render_stepper(1)
    st.title("Profile source")
    if action_button("Run profiler", "run_profiler", primary=True):
        from agent.profiler import connect, profile_database, save_profile
        with st.status("Profiling Postgres ...", expanded=False) as s:
            conn = connect()
            try:
                save_profile(profile_database(conn), compare.FILES["profile"])
            finally:
                conn.close()
            s.update(label="Profile saved to profile.json", state="complete")
    profile = compare.load("profile")
    if not profile:
        st.info("No profile.json yet. Run the profiler (Live mode).")
        return
    st.caption(f"{profile['database']} · Postgres {profile['server_version']} · "
               f"profiled {profile['profiled_at']}")
    tabs = st.tabs([f"{t['name']} ({t['row_count']:,})" for t in profile["tables"]])
    for tab, table in zip(tabs, profile["tables"]):
        with tab:
            counts = compare.profile_trap_counts(table)
            cols = st.columns(2 + len(counts))
            cols[0].metric("rows", f"{table['row_count']:,}")
            cols[1].metric("columns", len(table["columns"]))
            for col, (label, value) in zip(cols[2:], counts.items()):
                col.metric(label, value)
            fks = ", ".join(f"{','.join(f['columns'])} → {f['ref_table']}"
                            for f in table["foreign_keys"]) or "none"
            st.caption(f"Primary key: {', '.join(table['primary_key']) or '-'} · "
                       f"Foreign keys: {fks}")
            st.dataframe(compare.profile_table_frame(table), hide_index=True, width="stretch")


# ---------------------------------------------------------------- stage 2: plan

def page_plan() -> None:
    from agent import planner
    render_stepper(2)
    st.title("Plan and approve")
    approved_at = planner.approval_info()
    if approved_at:
        st.success(f"plan.json approved at {approved_at}. Migration is unlocked.")
    else:
        st.warning("No approved plan. Migration is locked until a plan is approved.")
    proposed_exists = compare.FILES["plan_proposed"].exists()
    c1, c2, c3 = st.columns(3)
    if c1.button("Generate plan (Gemini)", key="gen", disabled=not live(), type="primary",
                 help="Writes plan_proposed.json and naive_plan.json; never plan.json."):
        run_generate_plan(planner)
        proposed_exists = True
    if c2.button("Review current plan.json", key="review",
                 disabled=not live() or not compare.FILES["plan"].exists(),
                 help="Copy plan.json to plan_proposed.json for (re-)approval, no Gemini call."):
        planner.propose_from_approved()
        proposed_exists = True
    errors = proposal_errors(planner) if proposed_exists else ["no proposal"]
    if c3.button("Approve plan", key="approve", disabled=not live() or bool(errors),
                 help="Copies plan_proposed.json to plan.json and records approved_at."):
        stamp = planner.approve_plan(planner.load_profile())
        st.success(f"Approved at {stamp}.")
        st.rerun()
    if proposed_exists and errors:
        st.error("Proposal fails deterministic validation:\n\n- " + "\n- ".join(errors))
    shown = "plan_proposed" if proposed_exists else "plan"
    doc = compare.load(shown)
    if not doc:
        st.info("No plan yet. Generate one (Live mode).")
        return
    if proposed_exists and approved_at:
        render_plan_diff(planner)
    if shown == "plan_proposed":
        label = "Proposed plan (awaiting approval)"
    elif approved_at:
        label = f"Approved plan (approved {approved_at})"
    else:
        label = "Current plan (not approved)"
    st.subheader(label)
    st.caption(f"model {doc.get('model')} · generated {doc.get('generated_at')}")
    render_plan_tables(doc)


def run_generate_plan(planner) -> None:
    with st.status("Asking Gemini for a plan ...", expanded=True) as s:
        try:
            _, out = capture(planner.generate_proposals, planner.load_profile())
            s.update(label="Proposal written to plan_proposed.json", state="complete")
        except Exception as e:  # show the error; nothing is written to plan.json
            s.update(label=f"Plan generation failed: {e}", state="error")
            return
    with st.expander("Planner log"):
        st.code(out or "(no output)")


def proposal_errors(planner) -> list[str]:
    try:
        plan = planner.load_plan(compare.FILES["plan_proposed"])
        return planner.validate_plan(plan, planner.load_profile())
    except Exception as e:
        return [f"cannot read proposal: {e}"]


def render_plan_diff(planner) -> None:
    diff = planner.plan_diff(planner.load_plan(compare.FILES["plan"]),
                             planner.load_plan(compare.FILES["plan_proposed"]))
    with st.expander(f"Diff vs approved plan.json ({len(diff)} changes)", expanded=bool(diff)):
        if diff:
            st.dataframe(pd.DataFrame(diff), hide_index=True, width="stretch")
        else:
            st.write("Identical decisions.")


def render_plan_tables(doc: dict) -> None:
    tabs = st.tabs([t["source_table"] for t in doc["tables"]])
    for tab, t in zip(tabs, doc["tables"]):
        with tab:
            a, b = st.columns(2)
            a.markdown(f"**Partition:** `{t['partition_column'] or 'none'}`  \n"
                       f"<small>{t['partition_reason']}</small>", unsafe_allow_html=True)
            b.markdown(f"**Cluster:** `{', '.join(t['clustering_columns']) or 'none'}`  \n"
                       f"<small>{t['clustering_reason']}</small>", unsafe_allow_html=True)
            st.dataframe(compare.plan_table_frame(t), hide_index=True, width="stretch",
                         column_config={"reason": st.column_config.TextColumn(width="large")})


# ---------------------------------------------------------------- stage 3: migrate

def page_migrate() -> None:
    from agent.planner import approval_info
    render_stepper(3)
    st.title("Migrate")
    approved = approval_info()
    if not approved:
        st.warning("Locked: approve a plan in step 2 first.")
    sandbox = os.getenv("BQ_SANDBOX", "false").lower() == "true"
    if sandbox:
        st.info("Sandbox mode: partitioning is deferred (BigQuery sandbox expires partitions "
                "after 60 days, which would delete historical rows). Clustering is applied.")
    c1, c2 = st.columns(2)
    run = None
    if c1.button("Migrate agent plan", key="mig_agent", type="primary",
                 disabled=not live() or not approved):
        run = "agent"
    if c2.button("Migrate naive plan", key="mig_naive", disabled=not live() or not approved):
        run = "naive"
    if run:
        run_migration_live(run)
    log = compare.load("migration_log")
    for plan in ("agent", "naive"):
        df = compare.migration_frame(log, plan)
        st.subheader(f"Last {plan} migration")
        if df.empty:
            st.caption("Not migrated yet.")
        else:
            st.dataframe(df, hide_index=True, width="stretch")


def run_migration_live(plan_name: str) -> None:
    from agent.migrator import run_migration
    rows: list[dict] = []
    bar = st.progress(0.0, text=f"Starting {plan_name} migration ...")
    table_slot = st.empty()
    total = {"n": 1}

    def on_event(kind: str, data: dict) -> None:
        if kind == "start":
            total["n"] = max(len(data["tables"]), 1)
        elif kind == "table_start":
            note = " (partitioning deferred: sandbox)" if data["partitioning"] == "deferred" else ""
            bar.progress(len(rows) / total["n"], text=f"Migrating {data['source_table']}{note} ...")
        elif kind == "table_done":
            rows.append({"table": data["target_table"], "rows extracted": data["rows_extracted"],
                         "rows loaded": data["rows_loaded"], "seconds": data["seconds"],
                         "format": data["load_format"], "partitioning": data["partitioning"],
                         "ok": data["ok"]})
            table_slot.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            bar.progress(len(rows) / total["n"], text=f"{len(rows)}/{total['n']} tables")

    try:
        run_migration(plan_name, on_event=on_event)
        bar.progress(1.0, text=f"{plan_name} migration complete")
        st.success(f"{plan_name}: all row counts match. Results saved to migration_log.json.")
    except Exception as e:
        st.error(f"Migration stopped: {e}")


# ---------------------------------------------------------------- stage 4: validate

def page_validate() -> None:
    render_stepper(4)
    st.title("Validate and prove")
    plan = st.segmented_control("Plan", ["agent", "naive"], default="agent", key="vplan")
    plan = plan or "agent"
    c1, c2 = st.columns(2)
    if c1.button("Run validator", key="run_val", type="primary", disabled=not live()):
        run_module("agent.validator", plan, "Validating (deterministic checks) ...")
    if c2.button("Run query translation", key="run_tr", disabled=not live(),
                 help="Uses Gemini for translation (cached responses are reused)."):
        run_module("agent.translator", plan, "Translating queries and checking parity ...")
    report = compare.load(f"report_{plan}")
    if not report:
        st.info(f"No report_{plan}.json yet.")
        return
    render_report_header(report)
    render_known_limitations(report)
    st.subheader("Checks per table")
    for t in report["tables"]:
        render_table_report(t)
    st.subheader("Business query parity")
    render_query_parity(plan, report)
    render_ai_box(report)


def run_module(module: str, plan: str, label: str) -> None:
    import importlib
    mod = importlib.import_module(module)
    with st.status(label, expanded=False) as s:
        try:
            code, out = capture(mod.main, ["--plan", plan])
        except Exception as e:
            s.update(label=f"{module} failed: {e}", state="error")
            return
        s.update(label=f"{module} finished (exit code {code})", state="complete")
    with st.expander(f"{module} log"):
        st.code(out or "(no output)")


def render_report_header(report: dict) -> None:
    s = compare.demo_summary(report)
    a, b = st.columns([1, 3])
    a.markdown(badge(compare.overall_status(report), big=True), unsafe_allow_html=True)
    a.markdown(f'<div class="verdict">{compare.headline(s)}</div>', unsafe_allow_html=True)
    a.caption(f"report generated {report.get('generated_at')}")
    cols = b.columns(4)
    cols[0].metric("tables passed", s.get("passed", 0))
    cols[1].metric("tables warned", s.get("warned", 0))
    cols[2].metric("tables failed", s.get("failed", 0))
    cols[3].metric("business queries identical",
                   f"{s.get('queries_passed', 0)} of {s.get('queries', 0)}")


def render_table_report(t: dict) -> None:
    fp = t.get("fingerprint", {})
    title = (f"{t['target_table']} · {t['status']} · "
             f"{fp.get('matching', '-')} of {fp.get('source_rows', '-')} rows identical")
    with st.expander(title, expanded=t["status"] == "FAIL"):
        st.markdown(badge(t["status"]), unsafe_allow_html=True)
        st.dataframe(styled(compare.check_grid(t)), width="stretch")
        issues = compare.non_pass_checks(t)
        if issues:
            df = pd.DataFrame(issues)
            df[["source", "target"]] = df[["source", "target"]].astype(str)
            st.dataframe(styled(df, ["status"]), hide_index=True, width="stretch")
        if fp.get("examples"):
            st.caption("Example differing rows (canonical values, joined on primary key)")
            st.dataframe(pd.DataFrame(fp["examples"]).astype(str), hide_index=True,
                         width="stretch")


def render_query_parity(plan: str, report: dict) -> None:
    translations = {q["name"]: q for q in (compare.load(f"translations_{plan}") or {})
                    .get("queries", [])}
    queries = compare.query_results(report, plan)
    if not queries:
        st.info("No query parity results yet. Run query translation.")
        return
    for q in queries:
        tr = translations.get(q["name"], {})
        with st.expander(f"{q['name']} · {q['status']} · {q.get('detail')}"):
            st.markdown(badge(q["status"]), unsafe_allow_html=True)
            if q.get("dialect_changes"):
                st.markdown("**Dialect changes:** " + "; ".join(q["dialect_changes"]))
            a, b = st.columns(2)
            a.caption("Original (PostgreSQL)")
            a.code(tr.get("original_sql", ""), language="sql")
            b.caption(f"Translated (BigQuery) · confidence {q.get('confidence')} · "
                      f"source {tr.get('translation_source', 'gemini')}")
            b.code(tr.get("translated_sql") or "(no translation)", language="sql")
            if q.get("examples"):
                st.caption("Example differing rows")
                st.dataframe(pd.DataFrame(q["examples"]).astype(str), hide_index=True,
                             width="stretch")


def render_ai_box(report: dict) -> None:
    ai = report.get("ai_explanations") or {}
    with st.container(border=True):
        st.markdown("#### AI explanation (advisory)")
        st.caption("Written by Gemini from the WARN/FAIL findings only. Advisory: every "
                   "status on this page comes from deterministic checks and is never "
                   "changed by AI.")
        if ai.get("error"):
            st.write(f"Unavailable: {ai['error'][:300]}")
        for item in ai.get("items", []):
            where = f".{item['column']}" if item.get("column") else ""
            st.markdown(f"{badge(item['status'])} **{item['table']}{where}** · "
                        f"{item['check']}", unsafe_allow_html=True)
            st.markdown(f"{item['explanation']}  \n*Suggested fix:* {item['suggested_fix']}")
        if not ai.get("items") and not ai.get("error"):
            st.write(ai.get("note") or ai.get("label") or "No explanations.")


# ---------------------------------------------------------------- main

def main() -> None:
    st.set_page_config(page_title="BigQuery Migration Agent", page_icon="🧭", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    page = render_sidebar()
    {
        PAGES[0]: page_compare,
        PAGES[1]: page_profile,
        PAGES[2]: page_plan,
        PAGES[3]: page_migrate,
        PAGES[4]: page_validate,
    }[page]()


main()

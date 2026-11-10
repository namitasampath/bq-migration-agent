"""Streamlit UI for the BigQuery Migration Agent: profile -> plan & approve -> migrate -> prove.

Run from the project root:  streamlit run app.py

The app only wraps the agent modules (profiler, planner, migrator, validator,
translator) and reads their JSON artifacts through agent.compare; it contains no
migration or validation logic of its own. "Load last results" mode never touches
Postgres, BigQuery or Gemini; it only reads the saved files. The look (Material,
console-style; theme in .streamlit/config.toml) is presentation only.
"""
import contextlib
import io
import math
import numbers
import os
from datetime import datetime
from html import escape

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from agent import compare
from agent.compare import ROOT
from ui import charts

load_dotenv(ROOT / ".env")

PAGES = ["Compare agent vs naive", "1 · Profile source", "2 · Plan & approve",
         "3 · Migrate", "4 · Validate & prove"]
LIVE, LOAD = "Live (run steps)", "Load last results"
MODE_LABELS = {LOAD: "Last results", LIVE: "Live run"}
NAV = {PAGES[0]: ("compare_arrows", "Agent vs naive"),
       PAGES[1]: ("database", "Profile source"),
       PAGES[2]: ("auto_awesome", "Plan & approve"),
       PAGES[3]: ("sync_alt", "Migrate"),
       PAGES[4]: ("verified", "Validate & prove")}
STATUS_ICONS = {"PASS": "check_circle", "WARN": "warning", "FAIL": "error",
                "INFO": "info", "NOT_RUN": "do_not_disturb_on"}
MD_COLORS = {"PASS": "green", "WARN": "orange", "FAIL": "red", "INFO": "blue",
             "NOT_RUN": "gray"}
BANNER_ICONS = {"info": "info", "warn": "warning", "success": "check_circle",
                "error": "error"}
PROSE_COLUMNS = {"reason", "detail", "explanation", "traps", "agent detail", "naive detail"}

# colour layer: one colour per pipeline stage, used in stepper, nav, page header and tiles
STAGES = {  # stage: (colour, icon, icon colour on the tile)
    "Profile": ("#1A73E8", "database", "#FFFFFF"),
    "Plan": ("#9334E6", "auto_awesome", "#FFFFFF"),
    "Approve": ("#F9AB00", "how_to_reg", "#202124"),
    "Migrate": ("#12B5CB", "sync_alt", "#202124"),
    "Validate": ("#1E8E3E", "verified", "#FFFFFF"),
}
COMPARE_TILE = ("#5F6368", "compare_arrows", "#FFFFFF")
COMPARE_ACCENT = "linear-gradient(90deg, #1A73E8, #9334E6, #12B5CB, #1E8E3E)"
PAGE_STAGE = {PAGES[1]: "Profile", PAGES[2]: "Plan", PAGES[3]: "Migrate",
              PAGES[4]: "Validate"}
TONES = {  # tone: (number/icon colour, accent border, tint) - numbers meet WCAG AA on white
    "good": ("#188038", "#1E8E3E", "#E6F4EA"),
    "bad": ("#D93025", "#EA4335", "#FCE8E6"),
    "warn": ("#B06000", "#F9AB00", "#FEF7E0"),
    "info": ("#1967D2", "#1A73E8", "#E8F0FE"),
    "neutral": ("#3C4043", "#BDC1C6", "#F1F3F4"),
}
ROW_BAR = {"PASS": "#1E8E3E", "WARN": "#F9AB00", "FAIL": "#EA4335", "INFO": "#1A73E8",
           "NOT_RUN": "#9AA0A6"}
STATUS_RANK = {"NOT_RUN": 0, "PASS": 1, "INFO": 2, "WARN": 3, "FAIL": 4}
TYPE_COLUMNS = {"BigQuery type"}
TYPE_STYLES = [  # prefix, dot colour, text colour (AA), tint
    ("ARRAY", "#E8710A", "#A84800", "#FEEFE3"),
    ("JSON", "#12B5CB", "#00727A", "#E4F7FB"),
    ("BIGNUMERIC", "#1A73E8", "#185ABC", "#E8F0FE"),
    ("NUMERIC", "#1A73E8", "#185ABC", "#E8F0FE"),
    ("DATETIME", "#9334E6", "#7627BB", "#F3E8FD"),
    ("TIMESTAMP", "#9334E6", "#7627BB", "#F3E8FD"),
    ("DATE", "#9334E6", "#7627BB", "#F3E8FD"),
    ("STRING", "#9AA0A6", "#3C4043", "#F1F3F4"),
]

# styles live in ui/style.css (presentation only); injected once per run in main()
CSS = f"\n<style>\n{(ROOT / 'ui' / 'style.css').read_text(encoding='utf-8')}</style>\n"


# ---------------------------------------------------------------- HTML building blocks

def esc(text) -> str:
    """HTML-escape; '' is wrapped in <code> so markdown cannot curl it into a quote mark."""
    return escape(str(text)).replace("&#x27;&#x27;", "<code class=q>''</code>")


def msi(name: str, cls: str = "") -> str:
    """Material Symbols Outlined icon."""
    return f'<span class="msi {cls}">{name}</span>'


def chip(status: str, text: str = None) -> str:
    """Small rounded status chip with a Material icon."""
    s = status if status in STATUS_ICONS else "NOT_RUN"
    return (f'<span class="chip s-{s.lower()}">{msi(STATUS_ICONS[s])}'
            f'{esc(text or status)}</span>')


def status_icon(status: str) -> str:
    s = status if status in STATUS_ICONS else "NOT_RUN"
    return msi(STATUS_ICONS[s], f"ic-{s.lower()}")


def md_status(status: str) -> str:
    """Status icon + word for markdown labels (expanders), coloured by the theme."""
    s = status if status in STATUS_ICONS else "NOT_RUN"
    return f":{MD_COLORS[s]}[:material/{STATUS_ICONS[s]}: {s}]"


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def banner(kind: str, body: str, raw: bool = False) -> None:
    """Material banner: tinted background, icon on the left."""
    text = body if raw else esc(body)
    html(f'<div class="banner {kind}">{msi(BANNER_ICONS[kind])}<div>{text}</div></div>')


def section_title(text: str, icon: str = "") -> None:
    html(f'<div class="section-title">{msi(icon) if icon else ""}{esc(text)}</div>')


def kpi_card(label: str, value, sub: str = "", tone: str = "neutral", icon: str = "analytics",
             status: str = None, note: str = "") -> str:
    """KPI card: coloured top border, tinted icon circle, number in the tone's colour."""
    fg, accent, tint = TONES[tone]
    value = f"{value:,}" if isinstance(value, numbers.Integral) else esc(value)
    badge = status_icon(status) if status else ""
    return (f'<div class="kpi" style="--fg:{fg};--accent:{accent};--tint:{tint}">'
            f'<div class="top"><span class="kicon">{msi(icon)}</span>{badge}</div>'
            f'<div class="label">{esc(label)}</div>'
            f'<div class="value">{value}</div><div class="sub">{esc(sub)}</div>'
            + (f'<div class="note">{esc(note)}</div>' if note else "") + "</div>")


def kpi_row(cards: list[str], cls: str = "") -> None:
    html(f'<div class="kpi-row {cls}">{"".join(cards)}</div>')


def _cell(value, whole: bool = False) -> tuple[str, bool]:
    """(inner HTML, is_numeric) for one table cell; whole=True shows 5.0 as 5."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return '<span class="muted">—</span>', False
    if whole and isinstance(value, float):
        return f"{int(value):,}", True
    if isinstance(value, bool):
        return chip("PASS" if value else "FAIL", str(value).lower()), False
    if isinstance(value, numbers.Integral):
        return f"{value:,}", True
    text = str(value)
    if text in STATUS_ICONS:
        return chip(text), False
    return esc(text).replace("\n", "<br>"), isinstance(value, numbers.Number)


def table_html(df: pd.DataFrame, max_height: int = None) -> str:
    """Console-style data table: uppercase grey header, 48px rows, status chips,
    a 3px status colour bar on each row and coloured BigQuery type chips."""
    cols = [str(c) for c in df.columns]
    numeric = {c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])
               and not pd.api.types.is_bool_dtype(df[c])}
    # count columns that pandas stored as float only because of missing values
    whole = {c for c in numeric if pd.api.types.is_float_dtype(df[c])
             and df[c].dropna().apply(float.is_integer).all()}
    head = "".join(f'<th class="{"num" if c in numeric else ""}">{esc(n)}</th>'
                   for c, n in zip(df.columns, cols))
    rows = []
    for record in df.itertuples(index=False, name=None):
        status = row_status(record)
        bar = f' style="box-shadow:inset 3px 0 0 {ROW_BAR[status]}"' if status else ""
        tds = []
        for i, (c, n, v) in enumerate(zip(df.columns, cols, record)):
            if n in TYPE_COLUMNS and isinstance(v, str) and v:
                inner = type_chip(v)
            else:
                inner, _ = _cell(v, c in whole)
            cls = "prose" if n in PROSE_COLUMNS else "mono"
            if c in numeric:
                cls += " num"
            tds.append(f'<td class="{cls}"{bar if i == 0 else ""}>{inner}</td>')
        rows.append(f"<tr>{''.join(tds)}</tr>")
    style = f' style="max-height:{max_height}px"' if max_height else ""
    return (f'<div class="dt-wrap"{style}><table class="dt"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def data_table(df: pd.DataFrame, max_height: int = 520) -> None:
    if df is None or df.empty:
        st.caption("No rows.")
        return
    html(table_html(df, max_height))


def tile(spec: tuple, size: str = "sm", extra: str = "") -> str:
    """Rounded coloured icon tile: spec = (colour, icon, icon colour)."""
    colour, icon, fg = spec
    return (f'<span class="tile {size}" style="background:{colour};color:{fg}">'
            f'{msi(icon)}{extra}</span>')


def type_chip(bq_type: str) -> str:
    """BigQuery type as a coloured chip (NUMERIC blue, DATETIME purple, JSON teal, ...)."""
    text = str(bq_type)
    for prefix, dot, fg, bg in TYPE_STYLES:
        if text.upper().startswith(prefix):
            return (f'<span class="tchip" style="background:{bg};color:{fg}">'
                    f'<i style="background:{dot}"></i>{esc(text)}</span>')
    return f'<span class="tchip plain">{esc(text)}</span>'


def row_status(values) -> str:
    """Worst status among a row's cells (bools count as PASS/FAIL), or '' if none."""
    found = [("PASS" if v else "FAIL") if isinstance(v, bool) else str(v)
             for v in values if isinstance(v, bool) or str(v) in STATUS_RANK]
    return max(found, key=STATUS_RANK.get) if found else ""


def nav_tile_css() -> str:
    """Colour each sidebar nav icon as a tile in its stage colour."""
    rules = []
    for i, page in enumerate(PAGES, start=1):
        colour, _, fg = page_spec(page)
        rules.append(f'section[data-testid="stSidebar"] [data-testid="stRadioGroup"] > '
                     f'div:nth-child({i}) [data-testid="stRadioOption"] p span[role="img"] '
                     f'{{background:{colour}; color:{fg} !important;}}')
    return "<style>" + "\n".join(rules) + "</style>"


def page_spec(page: str) -> tuple:
    stage = PAGE_STAGE.get(page)
    return STAGES[stage] if stage else COMPARE_TILE


def ok_tone(ok: bool) -> str:
    return "good" if ok else "bad"


def chart(fig, title: str = "", caption: str = "") -> None:
    """Altair chart in a bordered card (our own theme, not Streamlit's)."""
    if fig is None:
        return
    with st.container(border=True):
        if title:
            section_title(title)
        if caption:
            st.caption(caption)
        st.altair_chart(fig, theme=None, width="stretch")


def render_hero(sa: dict, sn: dict) -> None:
    """Full-width headline card with soft tints and the two verdict chips."""
    if not (sa and sn):
        return
    n = sn.get("rows_compared", 0)
    head = (f'Naive migration silently changed <span class="mono bad">'
            f'{sn.get("rows_changed", 0):,}</span> of <span class="mono">{n:,}</span> rows. '
            f'The agent changed <span class="mono good">{sa.get("rows_changed", 0):,}</span>, '
            f'and proves it.')
    sub = f"Agent: {compare.headline(sa)}. Naive: {compare.headline(sn)}."
    chips = "".join(
        f'<span class="chip xl s-{st_.lower()}">{msi(STATUS_ICONS.get(st_, "info"))}'
        f'<span class="who">{who}</span>{st_}</span>'
        for who, st_ in (("Agent", sa.get("overall", "NOT_RUN")),
                         ("Naive", sn.get("overall", "NOT_RUN"))))
    html(f'<div class="hero"><div class="hero-text"><div class="eyebrow">Same source · same '
         f'dataset · deterministic proof</div><div class="head">{head}</div>'
         f'<div class="sub">{esc(sub)}</div></div><div class="chips">{chips}</div></div>')


def profile_icon(label: str) -> str:
    for word, icon in (("money", "payments"), ("NULL", "format_quote"),
                       ("microsecond", "schedule"), ("JSON", "data_object"),
                       ("array", "data_array")):
        if word in label:
            return icon
    return "analytics"


def capture(fn, *args):
    """Run a module entry point, capturing its console output for a log expander."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args)
    return result, buf.getvalue()


def live() -> bool:
    return st.session_state.get("mode") == LIVE


def action_button(label: str, key: str, disabled: bool = False, help_text: str = "",
                  primary: bool = False, icon: str = None) -> bool:
    """Button that is disabled in Load mode (with an explanation)."""
    if not live():
        help_text = "Switch to Live run in the sidebar to run this step."
    return st.button(label, key=key, disabled=disabled or not live(), help=help_text or None,
                     type="primary" if primary else "secondary", icon=icon)


def short_time(iso) -> str:
    """'2026-11-09T13:04:46' -> '9 Nov 13:04' (display only)."""
    try:
        d = datetime.fromisoformat(str(iso))
        return f"{d.day} {d:%b %H:%M}"
    except ValueError:
        return str(iso)


# ---------------------------------------------------------------- app bar, sidebar, header

def render_app_bar() -> None:
    project, dataset = os.getenv("GCP_PROJECT"), os.getenv("BQ_DATASET")
    chips = []
    if any(compare.is_sample(n) for n in ("report_agent", "report_naive", "plan")):
        chips.append(f'<span class="pchip sample">{msi("inventory_2")}Sample results</span>')
    if os.getenv("BQ_SANDBOX", "false").lower() == "true":
        chips.append(f'<span class="pchip sandbox">{msi("science")}Sandbox</span>')
    if project or dataset:
        where = " · ".join(esc(v) for v in (project, dataset) if v)
        chips.append(f'<span class="pchip">{msi("folder")}<span class="mono">{where}</span>'
                     "</span>")
    html(f'<div class="appbar"><div class="brand">{msi("database")}BigQuery Migration Agent'
         f'</div><div class="spacer"></div>{"".join(chips)}</div>')


def render_sidebar() -> str:
    st.session_state.setdefault("mode", LOAD)
    with st.sidebar:
        html('<div class="side-section">Mode</div>')
        st.segmented_control("Mode", [LOAD, LIVE], key="mode", required=True,
                             format_func=MODE_LABELS.get, label_visibility="collapsed",
                             width="stretch",
                             help="Last results only reads the saved JSON files: instant, no "
                                  "database, BigQuery or Gemini calls. Missing files fall "
                                  "back to the bundled samples/.")
        compare.use_samples(not live())
        html('<div class="side-section" style="margin-top:16px">Navigation</div>')
        page = st.radio("View", PAGES, key="page", label_visibility="collapsed",
                        format_func=lambda p: f":material/{NAV[p][0]}: {NAV[p][1]}")
        html('<div class="side-section" style="margin-top:24px">Environment</div>')
        env = {
            "model": os.getenv("GEMINI_MODEL", "-"),
            "cache": os.getenv("GEMINI_CACHE", "true"),
            "source": f"{os.getenv('PG_HOST', '-')}:{os.getenv('PG_PORT', '-')}/"
                      f"{os.getenv('PG_DB', '-')}",
            "sandbox": os.getenv("BQ_SANDBOX", "false"),
        }
        html("".join(f'<div class="kv stack"><span class="k">{k}</span>'
                     f'<span class="v" title="{esc(v)}">{esc(v)}</span></div>'
                     for k, v in env.items()))
        html('<div class="side-section" style="margin-top:24px">Artifacts</div>')
        rows = []
        for name in ("profile", "plan", "migration_log", "report_agent", "report_naive"):
            ts = compare.modified_at(name)
            when = datetime.fromtimestamp(ts).strftime("%d %b %H:%M") if ts else "missing"
            where = "samples/" if compare.is_sample(name) else ""
            rows.append(f'<div class="kv wide"><span class="k">{where}{compare.FILES[name].name}'
                        f'</span><span class="v">{when}</span></div>')
        html("".join(rows))
    return page


def pipeline_steps() -> list[dict]:
    """Five wizard steps with done flag and a status line, from the saved artifacts."""
    profile = compare.load("profile")
    plan = compare.load("plan")
    proposed = compare.FILES["plan_proposed"].exists()
    approved = compare.approved_at()
    log = compare.load("migration_log") or {}
    ra, rn = compare.load("report_agent"), compare.load("report_naive")
    migrated = [p for p in ("agent", "naive") if log.get(p)]
    validated = " · ".join(f"{p} {compare.overall_status(r)}"
                           for p, r in (("agent", ra), ("naive", rn)) if r)
    return [
        {"name": "Profile", "done": bool(profile),
         "status": f"profiled {short_time(profile.get('profiled_at'))}" if profile
         else "not run"},
        {"name": "Plan", "done": bool(plan) or proposed,
         "status": "proposal awaiting review" if proposed
         else f"{len(plan['tables'])} tables planned" if plan else "no plan"},
        {"name": "Approve", "done": bool(approved),
         "status": f"approved {short_time(approved)}" if approved else "pending"},
        {"name": "Migrate", "done": bool(migrated),
         "status": "migrated: " + ", ".join(migrated) if migrated else "not run"},
        {"name": "Validate", "done": bool(ra or rn), "status": validated or "not run"},
    ]


def current_step(page: str, steps: list[dict]):
    if page == PAGES[1]:
        return "Profile"
    if page == PAGES[2]:
        plan_step = steps[1]
        return "Approve" if plan_step["done"] else "Plan"
    return {PAGES[3]: "Migrate", PAGES[4]: "Validate"}.get(page)


def render_stepper(page: str) -> None:
    steps = pipeline_steps()
    current = current_step(page, steps)
    parts = []
    for s in steps:
        state = "current" if s["name"] == current else "done" if s["done"] else "pending"
        colour, icon, fg = STAGES[s["name"]]
        spec = (colour, icon, fg) if state != "pending" else ("#F1F3F4", icon, "#80868B")
        tick = f'<span class="tick">{msi("check")}</span>' if s["done"] else ""
        ring = f' style="--ring:{colour}"' if state == "current" else ""
        parts.append(f'<div class="step {state}"{ring}><div class="row">{tile(spec, "sm", tick)}'
                     f'{s["name"]}</div><div class="status" title="{esc(s["status"])}">'
                     f'{esc(s["status"])}</div></div>')
    html(f'<div class="stepper">{"<div class=step-line></div>".join(parts)}</div>')


def page_header(title: str, description: str, page: str = None) -> None:
    """Title with the stage's icon tile, a thin accent line in the stage colour, stepper."""
    spec = page_spec(page)
    accent = spec[0] if PAGE_STAGE.get(page) else COMPARE_ACCENT
    html(f'<div class="ph">{tile(spec, "lg")}<div class="ph-title">{esc(title)}</div></div>'
         f'<div class="ph-accent" style="background:{accent}"></div>'
         f'<div class="page-desc">{esc(description)}</div>')
    render_stepper(page)


# ---------------------------------------------------------------- compare view

def plan_header(name: str, s: dict) -> None:
    """Plan name, status chip and one-line verdict."""
    status = s.get("overall", "NOT_RUN") if s else "NOT_RUN"
    html(f'<div class="plan-head"><span class="name">{name} plan</span>{chip(status)}</div>'
         f'<div class="verdict">{esc(compare.headline(s))}</div>')


def plan_kpis(s: dict) -> None:
    """Three headline cards: rows silently changed, empty values lost, reports identical."""
    if not s:
        banner("info", "No report yet.")
        return
    n, changed = s.get("rows_compared", 0), s.get("rows_changed", 0)
    limited = s.get("rows_known_limitation", 0)
    q, qp = s.get("queries", 0), s.get("queries_passed", 0)
    lost = s.get("empty_strings_lost", 0)
    ok_rows, ok_lost, ok_q = changed == 0, lost == 0, q > 0 and qp == q
    kpi_row([
        kpi_card("Rows silently changed", changed, f"of {n:,} rows", ok_tone(ok_rows),
                 "table_rows", "PASS" if ok_rows else "FAIL",
                 note=f"+{limited:,} differ only by known limitations" if limited else ""),
        kpi_card("Empty values lost", lost, "'' turned into NULL", ok_tone(ok_lost),
                 "format_quote", "PASS" if ok_lost else "FAIL"),
        kpi_card("Business reports identical", f"{qp} of {q}", "query results match",
                 ok_tone(ok_q), "summarize", "PASS" if ok_q else "FAIL"),
    ])


def comparison_cell(value: str, ok) -> str:
    main, _, note = value.partition(" (")
    note = f'<span class="note">({esc(note)}</span>' if note else ""
    if ok is None:
        return f"<td>{esc(main)}{note}</td>"
    icon = status_icon("PASS" if ok else "FAIL")
    return f'<td><span class="cell">{icon}{esc(main)}</span>{note}</td>'


def comparison_table_html(rows: list[dict]) -> str:
    """Full comparison as a console data table, status icon + value per cell."""
    body = "".join(f'<tr><td class="metric">{esc(r["metric"])}</td>'
                   f'{comparison_cell(r["agent"], r["agent_ok"])}'
                   f'{comparison_cell(r["naive"], r["naive_ok"])}</tr>' for r in rows)
    return ('<div class="dt-wrap"><table class="dt cmp"><thead><tr>'
            '<th class="metric">Metric</th><th>Agent plan</th><th>Naive plan</th></tr>'
            f'</thead><tbody>{body}</tbody></table></div>')


def merged(agent_rows: list[dict], naive_rows: list[dict], keys: list[str],
           value: str) -> pd.DataFrame:
    a = pd.DataFrame(agent_rows)
    n = pd.DataFrame(naive_rows)
    if a.empty or n.empty:
        return pd.DataFrame()
    return a[keys + [value, "status"]].merge(
        n[keys + [value, "status"]], on=keys, how="outer", suffixes=(" (agent)", " (naive)"))


def page_compare() -> None:
    page_header("Agent vs naive migration",
                "Same source database, same BigQuery dataset. One plan was designed by the "
                "agent and approved by a human; the other is a typical careless migration. "
                "Every number comes from deterministic checks.", PAGES[0])
    ra, rn = compare.load("report_agent"), compare.load("report_naive")
    sa, sn = compare.demo_summary(ra, "agent"), compare.demo_summary(rn, "naive")
    render_hero(sa, sn)
    left, right = st.columns(2, gap="large")
    with left:
        plan_header("Agent", sa)
        plan_kpis(sa)
    with right:
        plan_header("Naive", sn)
        plan_kpis(sn)
    if ra and rn:
        c1, c2 = st.columns([3, 2], gap="medium")
        with c1:
            chart(charts.rows_changed_chart(charts.rows_changed_frame(ra, rn)),
                  "Rows changed per table",
                  "Row fingerprints joined on the primary key. Agent bars exclude rows that "
                  "differ only by a proven known limitation.")
        with c2:
            chart(charts.empty_lost_chart(compare.empty_strings_lost(rn)),
                  "Empty strings lost per column (naive)",
                  "Postgres empty strings that the naive load turned into NULL.")
    section_title("All metrics", "table_chart")
    html(comparison_table_html(compare.comparison_rows(sa, sn)))
    if not (ra and rn):
        return
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
        data_table(df)
    with tabs[1]:
        st.caption("Rows where Postgres stored an empty string '' (distinct from NULL).")
        df = merged(compare.empty_strings_lost(ra), compare.empty_strings_lost(rn),
                    ["table", "column"], "lost")
        src = pd.DataFrame(compare.empty_strings_lost(ra))[["table", "column", "source ''"]]
        data_table(src.merge(df, on=["table", "column"]))
    with tabs[2]:
        down = compare.type_downgrades(rn)
        st.caption(f"Agent: {len(compare.type_downgrades(ra))} downgrades. "
                   f"Naive: {len(down)} columns stored in a type that cannot represent the "
                   f"source exactly.")
        data_table(pd.DataFrame(down))
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
        banner("info", "No query parity results yet. Run step 4 (translator).")
        return
    data_table(pd.DataFrame(rows.values()))


def render_known_limitations(report: dict) -> None:
    lims = compare.known_limitations(report)
    if not lims:
        banner("success", "No known limitations recorded.")
        return
    st.caption("Accepted differences, each proven to be exactly the documented limitation "
               "(deterministic rules, not AI).")
    for text in dict.fromkeys(r["explanation"] for r in lims if r["explanation"]):
        banner("info", text)
    data_table(pd.DataFrame(lims).drop(columns=["explanation"]))


# ---------------------------------------------------------------- stage 1: profile

def page_profile() -> None:
    page_header("Profile source", "Schema, keys and per-column statistics of the legacy "
                "Postgres database, computed in SQL.", PAGES[1])
    if action_button("Run profiler", "run_profiler", primary=True, icon=":material/database:"):
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
        banner("info", "No profile.json yet. Run the profiler (Live run mode).")
        return
    st.caption(f"{profile['database']} · Postgres {profile['server_version']} · "
               f"profiled {profile['profiled_at']}")
    c1, _ = st.columns([2, 3])
    with c1:
        chart(charts.profile_rows_chart(profile), "Rows per table")
    tabs = st.tabs([f"{t['name']} ({t['row_count']:,})" for t in profile["tables"]])
    for tab, table in zip(tabs, profile["tables"]):
        with tab:
            counts = compare.profile_trap_counts(table)
            kpi_row([kpi_card("rows", table["row_count"], tone="info", icon="table_rows"),
                     kpi_card("columns", len(table["columns"]), tone="info",
                              icon="view_column")]
                    + [kpi_card(label, value, tone="info", icon=profile_icon(label))
                       for label, value in counts.items()])
            fks = ", ".join(f"{','.join(f['columns'])} → {f['ref_table']}"
                            for f in table["foreign_keys"]) or "none"
            html(f'<div class="pl">Primary key <code>{esc(", ".join(table["primary_key"]) or "-")}'
                 f'</code> · Foreign keys <code>{esc(fks)}</code></div>')
            data_table(compare.profile_table_frame(table), max_height=640)


# ---------------------------------------------------------------- stage 2: plan

def page_plan() -> None:
    from agent import planner
    page_header("Plan and approve", "Gemini proposes a BigQuery schema with reasons; "
                "deterministic rules validate it; a human approves it.", PAGES[2])
    approved_at = compare.approved_at()
    if approved_at:
        banner("success", f"plan.json approved at {approved_at}. Migration is unlocked.")
    else:
        banner("warn", "No approved plan. Migration is locked until a plan is approved.")
    proposed_exists = compare.FILES["plan_proposed"].exists()
    c1, c2, c3, _ = st.columns([1, 1, 1, 1])
    if c1.button("Generate plan", key="gen", disabled=not live(), type="primary",
                 icon=":material/auto_awesome:",
                 help="Asks Gemini. Writes plan_proposed.json and naive_plan.json; "
                      "never plan.json."):
        run_generate_plan(planner)
        proposed_exists = True
    if c2.button("Review current plan", key="review", icon=":material/edit_note:",
                 disabled=not live() or not compare.path("plan").exists(),
                 help="Copy plan.json to plan_proposed.json for (re-)approval, no Gemini call."):
        planner.propose_from_approved()
        proposed_exists = True
    errors = proposal_errors(planner) if proposed_exists else ["no proposal"]
    if c3.button("Approve plan", key="approve", disabled=not live() or bool(errors),
                 icon=":material/verified:",
                 help="Copies plan_proposed.json to plan.json and records approved_at."):
        stamp = planner.approve_plan(planner.load_profile())
        banner("success", f"Approved at {stamp}.")
        st.rerun()
    if proposed_exists and errors:
        items = "".join(f"<li>{esc(e)}</li>" for e in errors)
        banner("error", f"Proposal fails deterministic validation:<ul>{items}</ul>", raw=True)
    shown = "plan_proposed" if proposed_exists else "plan"
    doc = compare.load(shown)
    if not doc:
        banner("info", "No plan yet. Generate one (Live run mode).")
        return
    if proposed_exists and approved_at:
        render_plan_diff(planner)
    if shown == "plan_proposed":
        label = "Proposed plan (awaiting approval)"
    elif approved_at:
        label = f"Approved plan (approved {approved_at})"
    else:
        label = "Current plan (not approved)"
    st.subheader(label, anchor=False)
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
    with st.expander(f":material/difference: Diff vs approved plan.json ({len(diff)} changes)",
                     expanded=bool(diff)):
        if diff:
            data_table(pd.DataFrame(diff))
        else:
            st.write("Identical decisions.")


def render_plan_tables(doc: dict) -> None:
    tabs = st.tabs([t["source_table"] for t in doc["tables"]])
    for tab, t in zip(tabs, doc["tables"]):
        with tab:
            a, b = st.columns(2)
            a.markdown(f'<div class="pl">Partition</div><div class="pv"><code>'
                       f'{esc(t["partition_column"] or "none")}</code></div>'
                       f'<div class="pl">{esc(t["partition_reason"])}</div>',
                       unsafe_allow_html=True)
            b.markdown(f'<div class="pl">Cluster</div><div class="pv"><code>'
                       f'{esc(", ".join(t["clustering_columns"]) or "none")}</code></div>'
                       f'<div class="pl">{esc(t["clustering_reason"])}</div>',
                       unsafe_allow_html=True)
            data_table(compare.plan_table_frame(t), max_height=640)


# ---------------------------------------------------------------- stage 3: migrate

def page_migrate() -> None:
    page_header("Migrate", "Moves data per the approved plan with BigQuery load jobs "
                "(Parquet, NDJSON for JSON columns).", PAGES[3])
    approved = compare.approved_at()
    if not approved:
        banner("warn", "Locked: approve a plan in step 2 first.")
    sandbox = os.getenv("BQ_SANDBOX", "false").lower() == "true"
    if sandbox:
        banner("info", "Sandbox mode: partitioning is deferred (BigQuery sandbox expires "
               "partitions after 60 days, which would delete historical rows). Clustering "
               "is applied.")
    c1, c2, _ = st.columns([1, 1, 2])
    run = None
    if c1.button("Migrate agent plan", key="mig_agent", type="primary",
                 icon=":material/sync_alt:", disabled=not live() or not approved):
        run = "agent"
    if c2.button("Migrate naive plan", key="mig_naive", icon=":material/sync_alt:",
                 disabled=not live() or not approved):
        run = "naive"
    if run:
        run_migration_live(run)
    log = compare.load("migration_log")
    for plan in ("agent", "naive"):
        df = compare.migration_frame(log, plan)
        st.subheader(f"Last {plan} migration", anchor=False)
        if df.empty:
            st.caption("Not migrated yet.")
        else:
            data_table(df)


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
            table_slot.markdown(table_html(pd.DataFrame(rows)), unsafe_allow_html=True)
            bar.progress(len(rows) / total["n"], text=f"{len(rows)}/{total['n']} tables")

    try:
        run_migration(plan_name, on_event=on_event)
        bar.progress(1.0, text=f"{plan_name} migration complete")
        banner("success", f"{plan_name}: all row counts match. Results saved to "
               "migration_log.json.")
    except Exception as e:
        banner("error", f"Migration stopped: {e}")


# ---------------------------------------------------------------- stage 4: validate

def page_validate() -> None:
    page_header("Validate and prove", "Deterministic source-vs-target checks, row "
                "fingerprints and business query parity. AI never sets a status.", PAGES[4])
    plan = st.segmented_control("Plan", ["agent", "naive"], default="agent", key="vplan",
                                format_func=lambda p: f"{p.capitalize()} plan")
    plan = plan or "agent"
    c1, c2, _ = st.columns([1, 1, 2])
    if c1.button("Run validator", key="run_val", type="primary", disabled=not live(),
                 icon=":material/rule:"):
        run_module("agent.validator", plan, "Validating (deterministic checks) ...")
    if c2.button("Run query translation", key="run_tr", disabled=not live(),
                 icon=":material/translate:",
                 help="Uses Gemini for translation (cached responses are reused)."):
        run_module("agent.translator", plan, "Translating queries and checking parity ...")
    report = compare.load(f"report_{plan}")
    if not report:
        banner("info", f"No report_{plan}.json yet.")
        return
    render_report_header(report)
    render_known_limitations(report)
    section_title("Checks per table", "rule")
    for t in report["tables"]:
        render_table_report(t)
    section_title("Business query parity", "sync_alt")
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
    status = compare.overall_status(report)
    html(f'<div class="plan-head"><span class="name">Overall</span>{chip(status)}</div>'
         f'<div class="verdict">{esc(compare.headline(s))} · report generated '
         f'{esc(str(report.get("generated_at")))}</div>')
    q, qp = s.get("queries", 0), s.get("queries_passed", 0)
    passed, warned, failed = s.get("passed", 0), s.get("warned", 0), s.get("failed", 0)
    left, right = st.columns([3, 2], gap="medium")
    with left:
        kpi_row([
            kpi_card("Tables passed", passed, f"of {s.get('tables', 0)} tables",
                     "good" if passed else "neutral", "check_circle"),
            kpi_card("Tables warned", warned, "known limitations only",
                     "warn" if warned else "neutral", "warning"),
            kpi_card("Tables failed", failed, "data differs", ok_tone(failed == 0),
                     "task_alt" if failed == 0 else "error",
                     "PASS" if failed == 0 else "FAIL"),
            kpi_card("Business queries identical", f"{qp} of {q}", "query results match",
                     ok_tone(q > 0 and qp == q), "summarize",
                     "PASS" if q > 0 and qp == q else "FAIL"),
        ], cls="two")
    with right:
        chart(charts.status_donut(report), "Check statuses",
              "All per-column and per-table checks of this plan.")


def render_table_report(t: dict) -> None:
    fp = t.get("fingerprint", {})
    title = (f"{md_status(t['status'])} · **{t['target_table']}** · "
             f"{fp.get('matching', '-')} of {fp.get('source_rows', '-')} rows identical")
    with st.expander(title, expanded=t["status"] == "FAIL"):
        grid = compare.check_grid(t)
        data_table(grid.reset_index().rename(columns={grid.index.name or "index": "column"}))
        issues = compare.non_pass_checks(t)
        if issues:
            df = pd.DataFrame(issues)
            df[["source", "target"]] = df[["source", "target"]].astype(str)
            st.caption("Checks that did not pass")
            data_table(df)
        if fp.get("examples"):
            st.caption("Example differing rows (canonical values, joined on primary key)")
            data_table(pd.DataFrame(fp["examples"]).astype(str))


def render_query_parity(plan: str, report: dict) -> None:
    translations = {q["name"]: q for q in (compare.load(f"translations_{plan}") or {})
                    .get("queries", [])}
    queries = compare.query_results(report, plan)
    if not queries:
        banner("info", "No query parity results yet. Run query translation.")
        return
    for q in queries:
        tr = translations.get(q["name"], {})
        with st.expander(f"{md_status(q['status'])} · **{q['name']}** · {q.get('detail')}"):
            if q.get("dialect_changes"):
                html('<div class="pl">Dialect changes</div><div class="pv">'
                     + esc("; ".join(q["dialect_changes"])) + "</div>")
            a, b = st.columns(2)
            a.caption("Original (PostgreSQL)")
            a.code(tr.get("original_sql", ""), language="sql")
            b.caption(f"Translated (BigQuery) · confidence {q.get('confidence')} · "
                      f"source {tr.get('translation_source', 'gemini')}")
            b.code(tr.get("translated_sql") or "(no translation)", language="sql")
            if q.get("examples"):
                st.caption("Example differing rows")
                data_table(pd.DataFrame(q["examples"]).astype(str))


def render_ai_box(report: dict) -> None:
    ai = report.get("ai_explanations") or {}
    with st.container(border=True):
        section_title("AI explanation (advisory)", "auto_awesome")
        banner("info", "Written by Gemini from the WARN/FAIL findings only. Advisory: every "
               "status on this page comes from deterministic checks and is never changed "
               "by AI.")
        if ai.get("error"):
            st.write(f"Unavailable: {ai['error'][:300]}")
        for item in ai.get("items", []):
            where = f".{item['column']}" if item.get("column") else ""
            html(f'{chip(item["status"])} <b class="mono">{esc(item["table"] + where)}</b>'
                 f' · {esc(item["check"])}')
            st.markdown(f"{item['explanation']}  \n*Suggested fix:* {item['suggested_fix']}")
        if not ai.get("items") and not ai.get("error"):
            st.write(ai.get("note") or ai.get("label") or "No explanations.")


# ---------------------------------------------------------------- main

def main() -> None:
    st.set_page_config(page_title="BigQuery Migration Agent", page_icon=":material/database:",
                       layout="wide")
    html(CSS + nav_tile_css())
    page = render_sidebar()
    render_app_bar()
    {
        PAGES[0]: page_compare,
        PAGES[1]: page_profile,
        PAGES[2]: page_plan,
        PAGES[3]: page_migrate,
        PAGES[4]: page_validate,
    }[page]()


main()

"""Altair charts for the Streamlit UI (presentation only).

Every number is read from the validator / profiler JSON that agent.compare loads; nothing
is recomputed or judged here. Colours: green #1E8E3E vs red #EA4335 (checked for
colour-vision separation), amber #F29900 (a step darker than #F9AB00 so bars pass the
lightness check; value labels are always shown) and blue #1A73E8. Text stays in text colours.
"""
import altair as alt
import pandas as pd

GREEN, RED, AMBER, BLUE = "#1E8E3E", "#EA4335", "#F29900", "#1A73E8"
STATUS_COLORS = {"PASS": GREEN, "WARN": AMBER, "FAIL": RED, "INFO": BLUE}
TEXT, TEXT2, GRID, BORDER = "#202124", "#5F6368", "#F1F3F4", "#DADCE0"
FONT, MONO = "Roboto", "Roboto Mono"
PLANS = ["Agent", "Naive"]
# same axis on every layer (layers merge axes); labelExpr avoids "4e+3"-style tick labels
COUNT_AXIS = alt.Axis(tickCount=4, labelOverlap=True, labelExpr="format(datum.value, ',')")


def finish(chart, height: int):
    """Console look: no view border, recessive axes and grid, Roboto, legend on top."""
    return (chart.properties(height=height)
            .configure(font=FONT, background="#FFFFFF", numberFormat=",",
                       autosize=alt.AutoSizeParams(type="fit", contains="padding"))
            .configure_view(strokeWidth=0)
            .configure_axis(labelColor=TEXT2, titleColor=TEXT2, labelFont=FONT,
                            titleFont=FONT, labelFontSize=12, titleFontSize=12,
                            titleFontWeight=500, gridColor=GRID, domainColor=BORDER,
                            tickColor=BORDER, labelPadding=6)
            .configure_axisX(grid=False)
            .configure_legend(orient="top", title=None, labelColor=TEXT, labelFont=FONT,
                              labelFontSize=12, symbolType="circle", symbolSize=110))


def rows_changed_frame(agent_report: dict, naive_report: dict) -> pd.DataFrame:
    """Per table and plan: fingerprint mismatches (the validator's 'mismatched' count)."""
    rows = []
    for plan, report in zip(PLANS, (agent_report, naive_report)):
        for t in (report or {}).get("tables", []):
            fp = t.get("fingerprint") or {}
            rows.append({"table": t["source_table"], "plan": plan,
                         "rows changed": fp.get("mismatched", 0),
                         "rows compared": fp.get("source_rows", 0),
                         "known-limitation rows": fp.get("known_limitation_rows", 0)})
    return pd.DataFrame(rows)


def rows_changed_chart(df: pd.DataFrame):
    base = alt.Chart(df).encode(
        x=alt.X("table:N", title=None, scale=alt.Scale(paddingInner=0.3),
                axis=alt.Axis(labelAngle=0, labelFont=MONO)),
        xOffset=alt.XOffset("plan:N", sort=PLANS, scale=alt.Scale(paddingInner=0.08)),
        tooltip=[alt.Tooltip("table:N"), alt.Tooltip("plan:N"),
                 alt.Tooltip("rows changed:Q", format=","),
                 alt.Tooltip("rows compared:Q", format=","),
                 alt.Tooltip("known-limitation rows:Q", format=",")])
    bars = base.mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        y=alt.Y("rows changed:Q", title="rows changed", axis=COUNT_AXIS),
        color=alt.Color("plan:N", scale=alt.Scale(domain=PLANS, range=[GREEN, RED]),
                        legend=alt.Legend(title=None, labelExpr="datum.label + ' plan'")))
    labels = base.mark_text(dy=-8, fontSize=11, font=MONO, color=TEXT).encode(
        y=alt.Y("rows changed:Q", title="rows changed", axis=COUNT_AXIS),
        text=alt.Text("rows changed:Q", format=","))
    return finish(alt.layer(bars, labels), 260)


def empty_lost_chart(rows: list[dict]):
    """Horizontal bars of '' -> NULL losses per column (only columns that lost any)."""
    df = pd.DataFrame([r for r in rows if r.get("lost")])
    if df.empty:
        return None
    df["column"] = df["table"] + "." + df["column"]
    # Vega field names must not contain quotes
    df = df.rename(columns={"source ''": "source empty strings", "target ''": "target empty strings"})
    bars = alt.Chart(df).mark_bar(cornerRadiusEnd=4, color=AMBER).encode(
        y=alt.Y("column:N", sort="-x", title=None, axis=alt.Axis(labelFont=MONO, labelLimit=220)),
        x=alt.X("lost:Q", title="empty strings lost", axis=COUNT_AXIS),
        tooltip=[alt.Tooltip("column:N"), alt.Tooltip("source empty strings:Q", format=","),
                 alt.Tooltip("target empty strings:Q", format=","),
                 alt.Tooltip("lost:Q", format=",")])
    labels = bars.mark_text(align="left", dx=4, font=MONO, fontSize=11, color=TEXT).encode(
        x=alt.X("lost:Q", title="empty strings lost", axis=COUNT_AXIS),
        text=alt.Text("lost:Q", format=","))
    return finish(alt.layer(bars, labels), 28 * len(df) + 40)


def status_counts(report: dict) -> pd.DataFrame:
    """Check statuses summed over the validator's per-table check_counts."""
    totals = {s: 0 for s in STATUS_COLORS}
    for t in (report or {}).get("tables", []):
        for status, n in (t.get("check_counts") or {}).items():
            if status in totals:
                totals[status] += n
    df = pd.DataFrame([{"status": s, "count": n, "rank": i}
                       for i, (s, n) in enumerate(totals.items()) if n])
    if not df.empty:
        df["label"] = df["status"] + " · " + df["count"].map("{:,}".format)
    return df


def status_donut(report: dict):
    df = status_counts(report)
    if df.empty:
        return None
    arc = alt.Chart(df).mark_arc(innerRadius=58, outerRadius=90, cornerRadius=4,
                                 padAngle=0.03).encode(
        theta=alt.Theta("count:Q", stack=True),
        order=alt.Order("rank:Q"),
        color=alt.Color("label:N", sort=list(df["label"]),
                        scale=alt.Scale(domain=list(df["label"]),
                                        range=[STATUS_COLORS[s] for s in df["status"]]),
                        legend=alt.Legend(title=None, orient="right", labelFont=MONO,
                                          labelLimit=240)),
        tooltip=[alt.Tooltip("status:N"), alt.Tooltip("count:Q", format=",")])
    center = pd.DataFrame({"total": [f"{int(df['count'].sum()):,}"], "unit": ["checks"]})
    total = alt.Chart(center).mark_text(font=MONO, fontSize=22, color=TEXT, dy=-6).encode(
        text="total:N")
    unit = alt.Chart(center).mark_text(fontSize=11, color=TEXT2, dy=14).encode(text="unit:N")
    return finish(alt.layer(arc, total, unit), 200)


def profile_rows_chart(profile: dict):
    df = pd.DataFrame([{"table": t["name"], "rows": t["row_count"]}
                       for t in (profile or {}).get("tables", [])])
    if df.empty:
        return None
    bars = alt.Chart(df).mark_bar(cornerRadiusEnd=4, color=BLUE).encode(
        y=alt.Y("table:N", sort="-x", title=None, axis=alt.Axis(labelFont=MONO)),
        x=alt.X("rows:Q", title="rows", axis=COUNT_AXIS),
        tooltip=[alt.Tooltip("table:N"), alt.Tooltip("rows:Q", format=",")])
    labels = bars.mark_text(align="left", dx=4, font=MONO, fontSize=11, color=TEXT).encode(
        x=alt.X("rows:Q", title="rows", axis=COUNT_AXIS), text=alt.Text("rows:Q", format=","))
    return finish(alt.layer(bars, labels), 26 * len(df) + 40)

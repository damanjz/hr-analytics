"""Expected dashboard KPIs for a filter preset, computed in DuckDB independently of Tableau.

Usage: filter_check.py Department="Data and Analytics" Year="FY 2024-25"
Mirrors the workbook's In scope rule: each filter is "All" or an exact match, and the Year filter
does not apply to Employee or Driver rows (current employees have no fiscal year).
"""
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"Department": "department", "Location": "location", "Level": "level_band", "Year": "fiscal_year"}


def scope_sql(preset):
    parts = []
    for cap, val in preset.items():
        col, lit = FIELDS[cap], "'" + val.replace("'", "''") + "'"
        parts.append(f"({col} = {lit} OR record_type IN ('Employee', 'Driver'))" if cap == "Year" else f"{col} = {lit}")
    return " AND ".join(parts) or "TRUE"


def kpis(preset):
    con = duckdb.connect(str(ROOT / "data" / "hr.duckdb"), read_only=True)
    w = scope_sql(preset)
    one = lambda sql: con.sql(sql).fetchone()[0]
    out = {
        "Attrition, annualised": one(f"SELECT SUM(exits_voluntary + exits_involuntary) / SUM(headcount) * 12 FROM hr_workbench WHERE record_type = 'Month' AND {w}"),
        "Voluntary share of exits": one(f"SELECT SUM(exits_voluntary) / SUM(exits_voluntary + exits_involuntary) FROM hr_workbench WHERE record_type = 'Month' AND {w}"),
        "Median time to hire, days": one(f"SELECT MEDIAN(time_to_hire_days) FROM hr_workbench WHERE record_type = 'Requisition' AND {w}"),
        "eNPS": one(f"SELECT (COUNT(*) FILTER (enps_category = 'Promoter') - COUNT(*) FILTER (enps_category = 'Detractor')) * 100.0 / COUNT(*) FROM hr_workbench WHERE record_type = 'Survey' AND {w}"),
        "Pay ratio, women to men": one(f"SELECT MEDIAN(annual_ctc_lakh) FILTER (gender = 'Woman') / MEDIAN(annual_ctc_lakh) FILTER (gender = 'Man') FROM hr_workbench WHERE record_type = 'Employee' AND {w}"),
    }
    levers = con.sql(f"SELECT drivers, count(*) FROM hr_workbench WHERE record_type = 'Driver' AND {w} "
                     "GROUP BY 1 ORDER BY 2 DESC, 1").fetchall()
    con.close()
    return out, levers


def shown(name, v):
    """The value as the dashboard formats it."""
    if v is None:
        return "(no data)"
    if name == "Attrition, annualised" or name.startswith("Pay ratio"):
        return f"{v:.1%}"
    if name == "Voluntary share of exits":
        return f"{v:.0%}"
    return f"{v:,.0f}"


if __name__ == "__main__":
    preset = dict(a.split("=", 1) for a in sys.argv[1:])
    print("preset:", preset or "none (All)")
    values, levers = kpis(preset)
    for k, v in values.items():
        print(f"  {k:<28} {shown(k, v):>10}   (exact {v})")
    print("  people per lever (top 10%):", ", ".join(f"{name} {n}" for name, n in levers))

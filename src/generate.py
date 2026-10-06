"""Generate synthetic HRIS exports for a mid-size Indian IT services company.

Window: April 2022 to March 2026 (four financial years), Hyderabad and Bengaluru.
The exports mirror what an HR analyst pulls from an HRIS, an ATS and a survey tool:
employees, job history, compensation, ratings, exits, requisitions and anonymous
eNPS responses, monthly billable utilisation, plus a market salary benchmark. Everything is fictional and seeded.

Planted effects (so the analysis has something real to find):
- Voluntary exits rise with low pay against market, slow salary growth, long gaps
  since promotion, long commutes and the 1-3 year tenure window, and peak in FY 2022-23.
- Gender has no direct effect on leaving, but women are paid about 3.5% less for the
  same level, role and rating, and are promoted less often, so they thin out by level.
"""
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 11
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
rng = np.random.default_rng(SEED)

START = pd.Timestamp("2022-04-01")
MONTHS = pd.date_range(START, periods=48, freq="MS")
N_OPENING = 2150

LEVELS = ["L1", "L2", "L3", "L4", "L5", "L6"]
LEVEL_TITLE = {"L1": "Associate", "L2": "Engineer", "L3": "Senior engineer", "L4": "Lead", "L5": "Manager", "L6": "Director"}
LEVEL_MIX = [0.30, 0.32, 0.20, 0.10, 0.06, 0.02]
LEVEL_YEARS = {"L1": 0.5, "L2": 3, "L3": 6, "L4": 9, "L5": 12, "L6": 16}       # typical experience
LEVEL_CTC = {"L1": 4.5, "L2": 8, "L3": 14, "L4": 22, "L5": 34, "L6": 55}        # lakh, FY 2022-23 band midpoint
WOMEN_SHARE = {"L1": 0.42, "L2": 0.38, "L3": 0.33, "L4": 0.27, "L5": 0.22, "L6": 0.16}

DEPTS = {  # share, pay multiplier, attrition multiplier
    "Delivery": (0.45, 1.00, 1.00), "Quality Engineering": (0.15, 0.92, 1.05),
    "Cloud and Infrastructure": (0.12, 1.08, 1.05), "Data and Analytics": (0.10, 1.12, 1.25),
    "Consulting": (0.05, 1.10, 0.95), "Sales and Presales": (0.06, 1.05, 1.10),
    "Corporate Functions": (0.07, 0.90, 0.80),
}
NON_BILLABLE = {"Corporate Functions", "Sales and Presales"}
LOCATIONS = {"Hyderabad": 0.60, "Bengaluru": 0.40}
REGIONS = {"South": 0.55, "North": 0.15, "East": 0.15, "West": 0.10, "Northeast": 0.05}
TIERS = {"Tier 1": 0.15, "Tier 2": 0.45, "Tier 3": 0.40}
SOURCES_LATERAL = {"Referral": 0.35, "Job portal": 0.40, "Agency": 0.20, "Rehire": 0.05}

FY_VOLUNTARY = {2022: 0.130, 2023: 0.085, 2024: 0.069, 2025: 0.066}   # base annual voluntary rate by FY start
FY_MARKET_RAISE = {2022: 0.10, 2023: 0.07, 2024: 0.06, 2025: 0.065}
BAND_GROWTH = 0.08                                                # band midpoints move 8% a year
RATING_RAISE = {1: 0.0, 2: 0.35, 3: 1.0, 4: 1.45, 5: 2.0}         # multiplier on the market raise
PROMO_RATE = {1: 0.0, 2: 0.01, 3: 0.10, 4: 0.26, 5: 0.45}
WOMEN_PAY = 0.965
WOMEN_PROMO = 0.82


def pick(options, size):
    keys = list(options)
    return rng.choice(keys, size=size, p=np.array(list(options.values())) / sum(options.values()))


def fy(ts):
    return ts.year if ts.month >= 4 else ts.year - 1


def band_mid(level, dept, location, fy_start):
    return (LEVEL_CTC[level] * DEPTS[dept][1] * (1.05 if location == "Bengaluru" else 1.0)
            * (1 + BAND_GROWTH) ** (fy_start - 2022) * 1e5)


# ---------------------------------------------------------------- people
people, job_rows, comp_rows, rating_rows, exit_rows, req_rows, util_rows = [], [], [], [], [], [], []
next_id = [100001]


def new_person(level, dept, location, hire_date, source, tenure_years=0.0):
    pid = f"E{next_id[0]}"
    next_id[0] += 1
    women = WOMEN_SHARE[level] + (0.04 if source == "Campus" else 0)
    gender = rng.choice(["Woman", "Man", "Non-binary"], p=[women - 0.004, 1 - women, 0.004])
    age = 21 + LEVEL_YEARS[level] + max(rng.normal(0, 1.5), -1)
    p = dict(emp_id=pid, gender=gender, birth_year=int(hire_date.year - age + tenure_years),
             home_region=pick(REGIONS, 1)[0],
             university_tier=pick(TIERS if level != "L1" else {"Tier 1": 0.12, "Tier 2": 0.45, "Tier 3": 0.43}, 1)[0],
             hiring_source=source, hire_date=hire_date, location=location, department=dept, level=level,
             commute_minutes=int(np.clip(rng.lognormal(np.log(40 if location == "Hyderabad" else 52), 0.45), 10, 150)),
             sat=rng.normal(), perf=rng.normal(), last_promo=hire_date, active=True)
    people.append(p)
    return p


def add_comp(p, date, reason):
    comp_rows.append((p["emp_id"], date, p["ctc"], reason))
    p.setdefault("comp_hist", []).append((date, p["ctc"]))


def set_ctc(p, date, reason, fy_start):
    mid = band_mid(p["level"], p["department"], p["location"], fy_start)
    ctc = mid * rng.lognormal(0, 0.10) * (WOMEN_PAY if p["gender"] == "Woman" else 1.0)
    p["ctc"] = round(ctc, -3)
    add_comp(p, date, reason)


def rate(p, fy_start):
    z = 0.8 * p["perf"] + rng.normal(0, 0.7)
    r = int(np.digitize(z, [-1.8, -0.9, 0.55, 1.45]) + 1)
    rating_rows.append((p["emp_id"], f"FY {fy_start}-{(fy_start + 1) % 100:02d}", r))
    p["rating"] = r
    return r


# opening population, with two years of back history so growth features exist from day one
for level in pick(dict(zip(LEVELS, LEVEL_MIX)), N_OPENING):
    dept = pick({k: v[0] for k, v in DEPTS.items()}, 1)[0]
    loc = pick(LOCATIONS, 1)[0]
    tenure = float(np.clip(rng.exponential(2.8) + (LEVEL_YEARS[level] / 4), 0.1, 18))
    hire = START - pd.Timedelta(days=int(tenure * 365))
    src = "Campus" if level == "L1" or (level in ("L2", "L3") and rng.random() < 0.5 and tenure > 2) else pick(SOURCES_LATERAL, 1)[0]
    p = new_person(level, dept, loc, hire, src, tenure)
    job_rows.append((p["emp_id"], hire.normalize(), dept, level, "Hire"))
    years_back = [y for y in (2020, 2021) if pd.Timestamp(f"{y}-04-01") > hire]
    first = years_back[0] if years_back else 2022
    set_ctc(p, max(hire, pd.Timestamp(f"{first}-04-01")).normalize(), "Hire" if not years_back else "Opening", first)
    for y in years_back:
        r = rate(p, y)
        if y > first:
            p["ctc"] = round(p["ctc"] * (1 + FY_MARKET_RAISE[2022] * 0.8 * RATING_RAISE[r]), -3)
            add_comp(p, pd.Timestamp(f"{y}-04-01"), "Annual")
    p["last_promo"] = hire + pd.Timedelta(days=int(rng.uniform(0, max(tenure, 0.1)) * 365))

# ---------------------------------------------------------------- months
REQ_COUNTER = [1]
open_reqs = []          # requisitions waiting to be filled


def open_req(date, dept, level, reason, location=None):
    location = location or pick(LOCATIONS, 1)[0]
    rid = f"R{REQ_COUNTER[0]:05d}"
    REQ_COUNTER[0] += 1
    days = rng.gamma(4, {"L1": 8, "L2": 9, "L3": 11, "L4": 14, "L5": 18, "L6": 24}[level])
    days *= 1.25 if dept == "Data and Analytics" else 1.0
    days *= 1.2 if fy(date) == 2022 else 1.0
    accept = date + pd.Timedelta(days=int(days))
    join = accept + pd.Timedelta(days=int(rng.choice([30, 60, 60, 90])))
    open_reqs.append(dict(req_id=rid, opened=date, dept=dept, level=level, location=location, reason=reason, accept=accept, join=join,
                          candidates=int(rng.integers(6, 40)), declines=int(rng.poisson(0.6 if level in ("L1", "L2") else 1.1))))


for m in MONTHS:
    f = fy(m)
    active = [p for p in people if p["active"] and p["hire_date"] <= m]

    # April: ratings, raises and promotions for the cycle just closed
    if m.month == 4:
        for p in active:
            r = rate(p, f - 1) if (m - p["hire_date"]).days > 180 else None
            if r is None:
                continue
            promo_p = PROMO_RATE[r] * (WOMEN_PROMO if p["gender"] == "Woman" else 1.0) * (0.0 if p["level"] == "L6" else 1.0)
            if (m - p["last_promo"]).days > 540 and rng.random() < promo_p:
                p["level"] = LEVELS[LEVELS.index(p["level"]) + 1]
                p["last_promo"] = m
                job_rows.append((p["emp_id"], m, p["department"], p["level"], "Promotion"))
                p["ctc"] = round(p["ctc"] * rng.uniform(1.15, 1.22), -3)
                add_comp(p, m, "Promotion")
            else:
                raise_ = FY_MARKET_RAISE[f] * RATING_RAISE[r] * rng.uniform(0.85, 1.15)
                p["ctc"] = round(p["ctc"] * (1 + raise_), -3)
                add_comp(p, m, "Annual")

    # billable utilisation from timesheets (non-billable functions have none)
    for p in active:
        if p["department"] in NON_BILLABLE:
            continue
        if p.get("bench", 0) > 0:
            p["bench"] -= 1
            util = rng.uniform(0.0, 0.25)
        elif rng.random() < 0.025 * np.exp(-0.4 * p["sat"]):
            p["bench"] = int(rng.integers(1, 4))
            util = rng.uniform(0.0, 0.3)
        else:
            util = float(np.clip(0.84 + 0.05 * p["sat"] + rng.normal(0, 0.07), 0.3, 1.0))
        p.setdefault("util", []).append(util)
        util_rows.append((p["emp_id"], m, round(util, 2)))

    # exits
    base = 1 - (1 - FY_VOLUNTARY[f]) ** (1 / 12)
    for p in active:
        if not p["active"]:
            continue
        tenure_m = (m - p["hire_date"]).days / 30.4
        cr = p["ctc"] / band_mid(p["level"], p["department"], p["location"], f)
        cutoff = m - pd.DateOffset(months=24)
        old = [c for d, c in p["comp_hist"] if d <= cutoff]
        growth = p["ctc"] / old[-1] - 1 if old else 0.2
        h = base * DEPTS[p["department"]][2]
        h *= 0.5 if tenure_m < 6 else 1.0 if tenure_m < 12 else 1.55 if tenure_m < 36 else 1.0 if tenure_m < 72 else 0.6
        h *= {"L1": 1.05, "L2": 1.25, "L3": 1.05, "L4": 0.8, "L5": 0.6, "L6": 0.5}[p["level"]]
        h *= 2.1 if cr < 0.88 else 1.35 if cr < 0.98 else 0.65 if cr > 1.10 else 1.0
        h *= 1.6 if growth < 0.12 else 1.0
        h *= 1.5 if (m - p["last_promo"]).days > 1100 and p["level"] in ("L2", "L3", "L4") else 1.0
        h *= 1.6 if p["commute_minutes"] > 75 else 1.0
        h *= 1.3 if p.get("rating", 3) >= 4 and cr < 0.95 else 1.0
        recent = p.get("util", [])[-3:]
        h *= 1.7 if recent and np.mean(recent) < 0.5 else 1.0
        h *= np.exp(-0.35 * p["sat"])
        r = p.get("rating", 3)
        invol = {1: 0.03, 2: 0.006}.get(r, 0.0006)
        if f == 2023 and m.month in (10, 11, 12) and p["department"] == "Delivery" and p["level"] in ("L1", "L2"):
            invol += 0.012                                              # bench restructuring
        u = rng.random()
        if u < invol or u < invol + h:
            kind = "Involuntary" if u < invol else "Voluntary"
            if kind == "Involuntary":
                reason = "Restructuring" if (f == 2023 and m.month in (10, 11, 12) and r >= 3) else "Performance"
            else:
                weights = {"Better pay": 1 + 2 * (cr < 0.95) + (growth < 0.12), "Career growth": 1 + 2 * ((m - p["last_promo"]).days > 1100),
                           "Relocation or commute": 0.6 + 2 * (p["commute_minutes"] > 75), "Higher studies": 0.5 + (p["level"] in ("L1", "L2")),
                           "Personal": 0.8}
                reason = pick(weights, 1)[0]
            day = m + pd.Timedelta(days=int(rng.integers(0, 27)))
            p["active"] = False
            exit_rows.append((p["emp_id"], day, kind, reason))
            if rng.random() < 0.85:
                open_req(day, p["department"], p["level"], "Backfill", p["location"])

    # growth requisitions (about 5% net growth a year) and the January campus drive
    for _ in range(rng.poisson(4)):
        open_req(m, pick({k: v[0] for k, v in DEPTS.items()}, 1)[0],
                 pick(dict(zip(LEVELS[1:], [0.40, 0.30, 0.17, 0.09, 0.04])), 1)[0], "Growth")
    if m.month == 1:
        for _ in range(int(rng.integers(150, 200))):
            dept = pick({k: v[0] for k, v in DEPTS.items() if k != "Corporate Functions"}, 1)[0]
            rid = f"R{REQ_COUNTER[0]:05d}"
            REQ_COUNTER[0] += 1
            accept = m + pd.Timedelta(days=int(rng.normal(55, 10)))
            open_reqs.append(dict(req_id=rid, opened=m, dept=dept, level="L1", location=pick(LOCATIONS, 1)[0], reason="Campus", accept=accept,
                                  join=pd.Timestamp(f"{m.year}-08-01") + pd.Timedelta(days=int(rng.integers(0, 20))),
                                  candidates=int(rng.integers(20, 120)), declines=int(rng.poisson(0.3))))

    # fill requisitions whose joiner arrives this month
    month_end = m + pd.offsets.MonthEnd(0)
    for req in [r for r in open_reqs if m <= r["join"] <= month_end]:
        src = "Campus" if req["reason"] == "Campus" else pick(SOURCES_LATERAL, 1)[0]
        p = new_person(req["level"], req["dept"], req["location"], req["join"].normalize(), src)
        job_rows.append((p["emp_id"], p["hire_date"], p["department"], p["level"], "Hire"))
        set_ctc(p, p["hire_date"], "Hire", fy(p["hire_date"]))
        if src != "Campus":
            p["ctc"] = round(p["ctc"] * rng.uniform(1.0, 1.08), -3)    # laterals negotiate up
            comp_rows[-1] = (p["emp_id"], p["hire_date"], p["ctc"], "Hire")
            p["comp_hist"][-1] = (p["hire_date"], p["ctc"])
        req_rows.append((req["req_id"], req["dept"], req["level"], req["location"], req["reason"], req["opened"].normalize(),
                         req["accept"].normalize(), req["join"].normalize(), req["candidates"], req["declines"], p["emp_id"]))
        open_reqs.remove(req)

for req in open_reqs:   # still open at the end of the window
    req_rows.append((req["req_id"], req["dept"], req["level"], req["location"], req["reason"], req["opened"].normalize(),
                     pd.NaT, pd.NaT, req["candidates"], req["declines"], None))

# ---------------------------------------------------------------- eNPS (anonymous)
enps_rows = []
quarter_effect = {2022: -0.55, 2023: -0.15, 2024: 0.25, 2025: 0.45}
exit_lookup = {e[0]: e[1] for e in exit_rows}
for q_end in [d for d in MONTHS if d.month in (6, 9, 12, 3)]:
    f = fy(q_end)
    resp = 0
    for p in people:
        if p["hire_date"] > q_end or (p["emp_id"] in exit_lookup and exit_lookup[p["emp_id"]] <= q_end):
            continue
        if rng.random() < 0.55:
            resp += 1
            score = int(np.clip(round(7.2 + 1.6 * p["sat"] + quarter_effect[f] + rng.normal(0, 1.3)), 0, 10))
            band = "Junior (L1-L2)" if p["level"] in ("L1", "L2") else "Mid (L3-L4)" if p["level"] in ("L3", "L4") else "Senior (L5-L6)"
            enps_rows.append((f"S{len(enps_rows) + 1:06d}", q_end.normalize() + pd.offsets.MonthEnd(0), p["department"], p["location"], band, score))

# ---------------------------------------------------------------- write
RAW.mkdir(parents=True, exist_ok=True)
emp = pd.DataFrame([{k: p[k] for k in ("emp_id", "gender", "birth_year", "home_region", "university_tier", "hiring_source",
                                       "hire_date", "location", "commute_minutes")} for p in people])
emp["hire_date"] = pd.to_datetime(emp["hire_date"]).dt.normalize()
emp.sort_values("emp_id").to_csv(RAW / "employees.csv", index=False, date_format="%Y-%m-%d")

pd.DataFrame(job_rows, columns=["emp_id", "effective_date", "department", "level", "event"]).sort_values(["emp_id", "effective_date"]) \
    .to_csv(RAW / "job_history.csv", index=False, date_format="%Y-%m-%d")
pd.DataFrame(comp_rows, columns=["emp_id", "effective_date", "annual_ctc", "reason"]).sort_values(["emp_id", "effective_date"]) \
    .to_csv(RAW / "compensation.csv", index=False, date_format="%Y-%m-%d")
pd.DataFrame(rating_rows, columns=["emp_id", "review_cycle", "rating"]).drop_duplicates(["emp_id", "review_cycle"], keep="last") \
    .sort_values(["emp_id", "review_cycle"]).to_csv(RAW / "ratings.csv", index=False)
ex = pd.DataFrame(exit_rows, columns=["emp_id", "exit_date", "exit_type", "exit_reason"])
ex["exit_date"] = ex["exit_date"].dt.normalize()
ex.sort_values("exit_date").to_csv(RAW / "exits.csv", index=False, date_format="%Y-%m-%d")
pd.DataFrame(req_rows, columns=["req_id", "department", "level", "location", "reason", "opened_date", "offer_accepted_date", "join_date",
                                "candidates", "offer_declines", "hired_emp_id"]).sort_values("req_id") \
    .to_csv(RAW / "requisitions.csv", index=False, date_format="%Y-%m-%d")
pd.DataFrame(enps_rows, columns=["response_id", "survey_date", "department", "location", "level_band", "score"]) \
    .to_csv(RAW / "enps_responses.csv", index=False, date_format="%Y-%m-%d")

pd.DataFrame(util_rows, columns=["emp_id", "month", "billable_utilisation"])     .to_csv(RAW / "utilisation.csv", index=False, date_format="%Y-%m-%d")

bands = [(f"FY {y}-{(y + 1) % 100:02d}", lvl, d, loc, round(band_mid(lvl, d, loc, y) * 0.80, -3),
          round(band_mid(lvl, d, loc, y), -3), round(band_mid(lvl, d, loc, y) * 1.20, -3))
         for y in range(2020, 2026) for lvl in LEVELS for d in DEPTS for loc in LOCATIONS]
pd.DataFrame(bands, columns=["fiscal_year", "level", "department", "location", "band_min", "band_mid", "band_max"])     .to_csv(RAW / "salary_bands.csv", index=False)

hc_end = sum(1 for p in people if p["active"])
print(f"people {len(people)}, exits {len(ex)} ({(ex.exit_type == 'Voluntary').mean():.0%} voluntary), "
      f"headcount start {N_OPENING} end {hc_end}, requisitions {len(req_rows)}, eNPS responses {len(enps_rows)}")

-- Marts: report-ready tables, plus one combined source for the Tableau workbench.

CREATE OR REPLACE MACRO level_band(lvl) AS
    CASE WHEN lvl IN ('L1', 'L2') THEN 'Junior (L1-L2)' WHEN lvl IN ('L3', 'L4') THEN 'Mid (L3-L4)' ELSE 'Senior (L5-L6)' END;

-- Exits and hires, with the job the person held at the time.
CREATE OR REPLACE TABLE mart_exits AS
SELECT e.emp_id, e.exit_date, date_trunc('month', e.exit_date)::DATE AS month, e.exit_type, e.exit_reason,
       j.department, j.level, e.location, e.gender, r.rating AS last_rating,
       (e.exit_type = 'Voluntary' AND coalesce(r.rating, 0) >= 4) AS regretted
FROM dim_employee e
ASOF JOIN stg_job_history j ON e.emp_id = j.emp_id AND e.exit_date >= j.effective_date
ASOF LEFT JOIN stg_ratings r ON e.emp_id = r.emp_id AND e.exit_date >= r.known_from
WHERE e.exit_date IS NOT NULL;

CREATE OR REPLACE TABLE mart_hires AS
SELECT e.emp_id, e.hire_date, date_trunc('month', e.hire_date)::DATE AS month, e.hiring_source,
       j.department, j.level, e.location, e.gender
FROM dim_employee e
JOIN stg_job_history j ON j.emp_id = e.emp_id AND j.event = 'Hire'
WHERE e.hire_date >= DATE '2022-04-01';

CREATE OR REPLACE TABLE mart_headcount_monthly AS
WITH hc AS (
    SELECT month, department, level, location, gender, count(*) AS headcount
    FROM fact_employee_month GROUP BY ALL
),
hires AS (SELECT month, department, level, location, gender, count(*) AS hires FROM mart_hires GROUP BY ALL),
ex AS (
    SELECT month, department, level, location, gender,
           count(*) FILTER (exit_type = 'Voluntary') AS exits_voluntary,
           count(*) FILTER (exit_type = 'Involuntary') AS exits_involuntary,
           count(*) FILTER (regretted) AS exits_regretted
    FROM mart_exits GROUP BY ALL
),
-- Dense grid: every segment in every month (zeros where nothing happened), so rolling sums see all 12 months.
segments AS (SELECT department, level, location, gender FROM hc UNION SELECT department, level, location, gender FROM hires
             UNION SELECT department, level, location, gender FROM ex),
grid AS (
    SELECT d.month, d.fiscal_year, s.department, s.level, s.location, s.gender,
           coalesce(hc.headcount, 0) AS headcount, coalesce(hires.hires, 0) AS hires,
           coalesce(ex.exits_voluntary, 0) AS exits_voluntary, coalesce(ex.exits_involuntary, 0) AS exits_involuntary,
           coalesce(ex.exits_regretted, 0) AS exits_regretted
    FROM segments s
    CROSS JOIN dim_month d
    LEFT JOIN hc USING (month, department, level, location, gender)
    LEFT JOIN hires USING (month, department, level, location, gender)
    LEFT JOIN ex USING (month, department, level, location, gender)
)
-- Rolling 12 months (this month and the 11 before): additive, so any filter combination sums to the right
-- rate. NULL until 12 months of history exist.
SELECT month, fiscal_year, department, level, level_band(level) AS level_band, location, gender,
       headcount, hires, exits_voluntary, exits_involuntary, exits_regretted,
       CASE WHEN month >= (SELECT min(month) FROM dim_month) + INTERVAL 11 MONTH
            THEN sum(exits_voluntary + exits_involuntary) OVER seg12 END AS exits_12m,
       CASE WHEN month >= (SELECT min(month) FROM dim_month) + INTERVAL 11 MONTH
            THEN sum(headcount) OVER seg12 END AS headcount_12m
FROM grid
WINDOW seg12 AS (PARTITION BY department, level, location, gender ORDER BY month ROWS BETWEEN 11 PRECEDING AND CURRENT ROW);

-- Everyone on the books at 31 March 2026, with pay position and flight risk.
CREATE OR REPLACE TABLE mart_current_employees AS
SELECT f.emp_id, f.department, f.level, level_band(f.level) AS level_band, f.location, f.gender,
       f.home_region, f.university_tier, f.hiring_source, round(f.tenure_months / 12.0, 1) AS tenure_years,
       f.last_rating AS rating, f.annual_ctc, round(f.compa_ratio, 3) AS compa_ratio,
       CASE WHEN f.compa_ratio < 0.9 THEN 'Below band' WHEN f.compa_ratio > 1.1 THEN 'Above band' ELSE 'Within band' END AS pay_position,
       f.commute_minutes, f.utilisation_3m,
       r.risk_score, r.risk_percentile, r.risk_band, r.drivers
FROM fact_employee_month f
JOIN risk_scores r USING (emp_id)
WHERE f.month_end = DATE '2026-03-31';

CREATE OR REPLACE TABLE mart_requisitions AS
SELECT req_id, department, level, level_band(level) AS level_band, location, reason, opened_date,
       'FY ' || fy_start || '-' || right(CAST(fy_start + 1 AS VARCHAR), 2) AS fiscal_year,
       offer_accepted_date, join_date, time_to_hire_days, time_to_fill_days,
       (offer_accepted_date IS NOT NULL) AS filled, candidates, offer_declines
FROM fact_requisition;

CREATE OR REPLACE TABLE mart_enps AS
SELECT response_id, survey_date, d.fiscal_year,
       'Q' || ((month(survey_date) + 8) % 12 // 3 + 1) || ' ' || d.fiscal_year AS survey_quarter,
       department, location, level_band, score,
       CASE WHEN score >= 9 THEN 'Promoter' WHEN score >= 7 THEN 'Passive' ELSE 'Detractor' END AS category
FROM stg_enps
JOIN dim_month d ON d.month = date_trunc('month', survey_date)::DATE;

-- One long table so every workbench filter (department, location, level band, year) reaches every view.
CREATE OR REPLACE TABLE hr_workbench AS
SELECT 'Month' AS record_type, month AS date, fiscal_year, department, location, level, level_band, gender,
       headcount, hires, exits_voluntary, exits_involuntary, exits_regretted, exits_12m, headcount_12m,
       NULL::VARCHAR AS emp_id, NULL::VARCHAR AS home_region, NULL::VARCHAR AS university_tier, NULL::VARCHAR AS hiring_source,
       NULL::DOUBLE AS tenure_years, NULL::INTEGER AS rating, NULL::DOUBLE AS annual_ctc_lakh, NULL::DOUBLE AS compa_ratio,
       NULL::VARCHAR AS pay_position, NULL::DOUBLE AS risk_score, NULL::DOUBLE AS risk_percentile, NULL::VARCHAR AS risk_band,
       NULL::VARCHAR AS drivers, NULL::VARCHAR AS req_reason, NULL::INTEGER AS time_to_hire_days, NULL::BOOLEAN AS filled,
       NULL::INTEGER AS enps_score, NULL::VARCHAR AS enps_category
FROM mart_headcount_monthly
UNION ALL
SELECT 'Employee', DATE '2026-03-31', 'FY 2025-26', department, location, level, level_band, gender,
       1, NULL, NULL, NULL, NULL, NULL, NULL,
       emp_id, home_region, university_tier, hiring_source, tenure_years, rating, round(annual_ctc / 100000, 2), compa_ratio,
       pay_position, risk_score, risk_percentile, risk_band, drivers, NULL, NULL, NULL, NULL, NULL
FROM mart_current_employees
UNION ALL
-- One row per lever behind each top-10% score, so the drivers summary can count people per lever
SELECT 'Driver', DATE '2026-03-31', 'FY 2025-26', department, location, level, level_band, gender,
       NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       emp_id, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       NULL, NULL, NULL, risk_band, lever, NULL, NULL, NULL, NULL, NULL
FROM (SELECT *, unnest(string_split(drivers, ', ')) AS lever FROM mart_current_employees WHERE risk_band = 'High')
UNION ALL
SELECT 'Requisition', opened_date, fiscal_year, department, location, level, level_band, NULL,
       NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       NULL, reason, time_to_hire_days, filled, NULL, NULL
FROM mart_requisitions
UNION ALL
SELECT 'Survey', survey_date, fiscal_year, department, location, NULL, level_band, NULL,
       NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
       NULL, NULL, NULL, NULL, score, category
FROM mart_enps;

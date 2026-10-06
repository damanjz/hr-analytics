-- Model: month dimension, employee dimension and a monthly employee snapshot.
-- Every attribute in the snapshot is taken "as of" the month end, so nothing from the future leaks in.

CREATE OR REPLACE TABLE dim_month AS
SELECT CAST(m AS DATE) AS month,
       last_day(CAST(m AS DATE)) AS month_end,
       CASE WHEN month(m) >= 4 THEN year(m) ELSE year(m) - 1 END AS fy_start,
       'FY ' || fy_start || '-' || right(CAST(fy_start + 1 AS VARCHAR), 2) AS fiscal_year,
       strftime(m, '%b %Y') AS month_label
FROM range(DATE '2022-04-01', DATE '2026-04-01', INTERVAL 1 MONTH) AS t(m);

CREATE OR REPLACE TABLE dim_employee AS
SELECT e.*, x.exit_date, x.exit_type, x.exit_reason,
       (x.exit_type = 'Voluntary') AS left_voluntarily
FROM stg_employees e
LEFT JOIN stg_exits x USING (emp_id);

-- One row per employee per month end at which they were on the books.
CREATE OR REPLACE TABLE fact_employee_month AS
WITH base AS (
    SELECT d.month, d.month_end, d.fiscal_year, e.emp_id
    FROM dim_month d
    JOIN dim_employee e ON e.hire_date <= d.month_end AND (e.exit_date IS NULL OR e.exit_date > d.month_end)
),
job AS (
    SELECT b.*, j.department, j.level
    FROM base b ASOF JOIN stg_job_history j ON b.emp_id = j.emp_id AND b.month_end >= j.effective_date
),
promo AS (
    SELECT j.*, coalesce(p.effective_date, e.hire_date) AS last_promotion
    FROM job j
    JOIN dim_employee e USING (emp_id)
    ASOF LEFT JOIN (SELECT emp_id, effective_date FROM stg_job_history WHERE event = 'Promotion') p
      ON j.emp_id = p.emp_id AND j.month_end >= p.effective_date
),
pay AS (
    SELECT p.*, c.annual_ctc
    FROM promo p ASOF JOIN stg_compensation c ON p.emp_id = c.emp_id AND p.month_end >= c.effective_date
),
pay_before AS (
    SELECT p.*, c.annual_ctc AS ctc_24m_ago
    FROM pay p ASOF LEFT JOIN stg_compensation c
      ON p.emp_id = c.emp_id AND (p.month_end - INTERVAL 24 MONTH)::DATE >= c.effective_date
),
rated AS (
    SELECT p.*, r.rating AS last_rating, r.review_cycle
    FROM pay_before p ASOF LEFT JOIN stg_ratings r ON p.emp_id = r.emp_id AND p.month_end >= r.known_from
),
rated_prev AS (
    SELECT r.*, r2.rating AS prev_rating
    FROM rated r ASOF LEFT JOIN stg_ratings r2
      ON r.emp_id = r2.emp_id AND (r.month_end - INTERVAL 12 MONTH)::DATE >= r2.known_from
)
SELECT r.month, r.month_end, r.fiscal_year, r.emp_id, r.department, r.level,
       (SELECT avg(u.billable_utilisation) FROM stg_utilisation u
         WHERE u.emp_id = r.emp_id AND u.month BETWEEN (r.month - INTERVAL 2 MONTH)::DATE AND r.month) AS utilisation_3m,
       (SELECT count(*) FROM stg_utilisation u
         WHERE u.emp_id = r.emp_id AND u.month BETWEEN (r.month - INTERVAL 11 MONTH)::DATE AND r.month
           AND u.billable_utilisation < 0.3) AS bench_months_12m,
       CAST(substr(r.level, 2) AS INTEGER) AS level_num,
       e.location, e.gender, e.home_region, e.university_tier, e.hiring_source, e.commute_minutes,
       datediff('month', e.hire_date, r.month_end) AS tenure_months,
       datediff('month', r.last_promotion, r.month_end) AS months_since_promotion,
       r.annual_ctc, b.band_mid,
       r.annual_ctc / b.band_mid AS compa_ratio,
       r.annual_ctc / r.ctc_24m_ago - 1 AS salary_growth_24m,
       r.last_rating, r.last_rating - r.prev_rating AS rating_change
FROM rated_prev r
JOIN dim_employee e USING (emp_id)
LEFT JOIN stg_salary_bands b
  ON b.level = r.level AND b.department = r.department AND b.location = e.location AND b.fiscal_year = r.fiscal_year;

CREATE OR REPLACE TABLE fact_requisition AS
SELECT *,
       datediff('day', opened_date, offer_accepted_date) AS time_to_hire_days,
       datediff('day', opened_date, join_date) AS time_to_fill_days,
       CASE WHEN month(opened_date) >= 4 THEN year(opened_date) ELSE year(opened_date) - 1 END AS fy_start
FROM stg_requisitions;

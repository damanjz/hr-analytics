-- Features for the flight-risk model.
-- One row per active employee per quarter end. Label: left voluntarily within the next 6 months.
-- Protected and background attributes (gender, age, region, university tier) are deliberately not features.

CREATE OR REPLACE TABLE model_snapshots AS
SELECT f.month_end AS snapshot_date, f.emp_id,
       f.tenure_months,
       CASE WHEN f.tenure_months < 12 THEN 'Under 1 year' WHEN f.tenure_months < 36 THEN '1 to 3 years'
            WHEN f.tenure_months < 72 THEN '3 to 6 years' ELSE '6 years or more' END AS tenure_stage,
       f.level_num, f.department, f.location, f.hiring_source,
       f.months_since_promotion, f.compa_ratio, f.salary_growth_24m,
       f.last_rating, f.rating_change, f.commute_minutes, f.utilisation_3m, f.bench_months_12m,
       CASE
           WHEN e.exit_date > f.month_end AND e.exit_date <= (f.month_end + INTERVAL 6 MONTH)::DATE
                AND e.exit_type = 'Voluntary' THEN 1
           ELSE 0
       END AS left_within_6m,
       -- an involuntary exit inside the window means the outcome was never observable
       coalesce(e.exit_date > f.month_end AND e.exit_date <= (f.month_end + INTERVAL 6 MONTH)::DATE
                AND e.exit_type = 'Involuntary', false) AS censored,
       (f.month_end + INTERVAL 6 MONTH)::DATE <= DATE '2026-03-31' AS label_observable
FROM fact_employee_month f
JOIN dim_employee e USING (emp_id)
WHERE month(f.month_end) IN (3, 6, 9, 12);

CREATE OR REPLACE TABLE model_features AS
SELECT * FROM (VALUES
    ('tenure_months', 'numeric'), ('level_num', 'numeric'), ('months_since_promotion', 'numeric'),
    ('compa_ratio', 'numeric'), ('salary_growth_24m', 'numeric'), ('last_rating', 'numeric'),
    ('rating_change', 'numeric'), ('commute_minutes', 'numeric'),
    ('utilisation_3m', 'numeric'), ('bench_months_12m', 'numeric'),
    ('tenure_stage', 'categorical'), ('department', 'categorical'), ('location', 'categorical'), ('hiring_source', 'categorical')
) AS t(feature, kind);

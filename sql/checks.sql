-- Each check returns the rows that break a rule. A clean build returns zero rows for every check.

-- name: headcount rolls forward (last month + hires - exits = this month)
SELECT month FROM (
    SELECT month, sum(headcount) AS hc, sum(hires) AS hires, sum(exits_voluntary + exits_involuntary) AS exits,
           lag(sum(headcount)) OVER (ORDER BY month) AS prev_hc
    FROM mart_headcount_monthly GROUP BY month
) WHERE prev_hc IS NOT NULL AND hc <> prev_hc + hires - exits;

-- name: every employee-month has a level, a salary and a market band
SELECT emp_id, month FROM fact_employee_month WHERE level IS NULL OR annual_ctc IS NULL OR band_mid IS NULL;

-- name: nobody leaves before joining, and nobody leaves twice
SELECT x.emp_id FROM stg_exits x JOIN stg_employees e USING (emp_id) WHERE x.exit_date < e.hire_date
UNION ALL SELECT emp_id FROM stg_exits GROUP BY emp_id HAVING count(*) > 1;

-- name: every employee has exactly one hire record
SELECT emp_id FROM stg_employees e
LEFT JOIN (SELECT emp_id, count(*) AS n FROM stg_job_history WHERE event = 'Hire' GROUP BY emp_id) h USING (emp_id)
WHERE coalesce(h.n, 0) <> 1;

-- name: promotions move exactly one level up
SELECT emp_id, effective_date FROM (
    SELECT emp_id, effective_date, event, CAST(substr(level, 2) AS INTEGER) AS lvl,
           lag(CAST(substr(level, 2) AS INTEGER)) OVER (PARTITION BY emp_id ORDER BY effective_date) AS prev
    FROM stg_job_history
) WHERE event = 'Promotion' AND lvl <> prev + 1;

-- name: filled requisitions match the person hired (date, department, level, location)
SELECT r.req_id FROM stg_requisitions r
JOIN stg_employees e ON e.emp_id = r.hired_emp_id
JOIN stg_job_history j ON j.emp_id = e.emp_id AND j.event = 'Hire'
WHERE e.hire_date <> r.join_date OR j.department <> r.department OR j.level <> r.level OR e.location <> r.location;

-- name: ratings are 1 to 5 and eNPS scores 0 to 10
SELECT emp_id FROM stg_ratings WHERE rating NOT BETWEEN 1 AND 5
UNION ALL SELECT response_id FROM stg_enps WHERE score NOT BETWEEN 0 AND 10;

-- name: survey responses carry no employee identifier
SELECT column_name FROM information_schema.columns WHERE table_name = 'stg_enps' AND column_name ILIKE '%emp%';

-- name: exit and hire totals in the monthly mart match the source
SELECT 'exits' FROM (SELECT sum(exits_voluntary + exits_involuntary) AS n FROM mart_headcount_monthly)
WHERE n <> (SELECT count(*) FROM stg_exits WHERE exit_date BETWEEN DATE '2022-04-01' AND DATE '2026-03-31')
UNION ALL
SELECT 'hires' FROM (SELECT sum(hires) AS n FROM mart_headcount_monthly)
WHERE n <> (SELECT count(*) FROM stg_employees WHERE hire_date BETWEEN DATE '2022-04-01' AND DATE '2026-03-31');

-- name: the model uses no protected or background attributes
SELECT feature FROM model_features
WHERE feature IN ('gender', 'home_region', 'university_tier', 'birth_year', 'age');

-- name: test period starts after every training label has resolved
SELECT * FROM model_metrics WHERE CAST(test_start AS DATE) < (CAST(train_end AS DATE) + INTERVAL 6 MONTH)::DATE;

-- name: one risk score per current employee, between 0 and 1
SELECT 'count' FROM (SELECT count(*) AS n FROM risk_scores)
WHERE n <> (SELECT count(*) FROM fact_employee_month WHERE month_end = DATE '2026-03-31')
UNION ALL SELECT emp_id FROM risk_scores WHERE risk_score NOT BETWEEN 0 AND 1;

-- name: pay equity covers every current woman and man
SELECT metric FROM pay_equity
WHERE employees <> (SELECT count(*) FROM fact_employee_month WHERE month_end = DATE '2026-03-31' AND gender IN ('Woman', 'Man'));

-- name: workbench rows reconcile to their marts
SELECT record_type FROM (
    SELECT record_type, count(*) AS n FROM hr_workbench GROUP BY record_type
) w
WHERE n <> CASE record_type
    WHEN 'Month' THEN (SELECT count(*) FROM mart_headcount_monthly)
    WHEN 'Employee' THEN (SELECT count(*) FROM mart_current_employees)
    WHEN 'Requisition' THEN (SELECT count(*) FROM mart_requisitions)
    WHEN 'Survey' THEN (SELECT count(*) FROM mart_enps)
    WHEN 'Driver' THEN (SELECT sum(len(string_split(drivers, ', '))) FROM mart_current_employees WHERE risk_band = 'High') END;

-- name: rolling 12-month exits and headcount match a direct count from the source, per department and month
WITH m AS (SELECT DISTINCT month FROM mart_headcount_monthly),
direct AS (
    SELECT m.month, d.department,
           (SELECT count(*) FROM mart_exits x WHERE x.department = d.department
                AND x.month BETWEEN m.month - INTERVAL 11 MONTH AND m.month) AS exits,
           (SELECT count(*) FROM fact_employee_month f WHERE f.department = d.department
                AND f.month BETWEEN m.month - INTERVAL 11 MONTH AND m.month) AS heads
    FROM m CROSS JOIN (SELECT DISTINCT department FROM mart_headcount_monthly) d
    WHERE m.month >= (SELECT min(month) FROM m) + INTERVAL 11 MONTH
),
mart AS (SELECT month, department, sum(exits_12m) AS exits, sum(headcount_12m) AS heads
         FROM mart_headcount_monthly GROUP BY ALL)
SELECT d.month, d.department FROM direct d JOIN mart USING (month, department)
WHERE d.exits <> mart.exits OR d.heads <> mart.heads
UNION ALL
SELECT month, 'rolling value before 12 months of history' FROM mart_headcount_monthly
WHERE month < (SELECT min(month) FROM mart_headcount_monthly) + INTERVAL 11 MONTH AND exits_12m IS NOT NULL;

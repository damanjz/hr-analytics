-- Staging: typed copies of the raw HRIS, ATS, survey and benchmark exports.

CREATE OR REPLACE TABLE stg_employees AS
SELECT emp_id, gender, birth_year, home_region, university_tier, hiring_source,
       CAST(hire_date AS DATE) AS hire_date, location, commute_minutes
FROM read_csv('data/raw/employees.csv', header = true);

CREATE OR REPLACE TABLE stg_job_history AS
SELECT emp_id, CAST(effective_date AS DATE) AS effective_date, department, level, event
FROM read_csv('data/raw/job_history.csv', header = true);

CREATE OR REPLACE TABLE stg_compensation AS
SELECT emp_id, CAST(effective_date AS DATE) AS effective_date, CAST(annual_ctc AS DOUBLE) AS annual_ctc, reason
FROM read_csv('data/raw/compensation.csv', header = true);

-- A review cycle "FY 2022-23" closes on 31 March 2023 and is known from 1 April 2023.
CREATE OR REPLACE TABLE stg_ratings AS
SELECT emp_id, review_cycle, rating,
       make_date(CAST(substr(review_cycle, 4, 4) AS INTEGER) + 1, 4, 1) AS known_from
FROM read_csv('data/raw/ratings.csv', header = true);

CREATE OR REPLACE TABLE stg_exits AS
SELECT emp_id, CAST(exit_date AS DATE) AS exit_date, exit_type, exit_reason
FROM read_csv('data/raw/exits.csv', header = true);

CREATE OR REPLACE TABLE stg_requisitions AS
SELECT req_id, department, level, location, reason, CAST(opened_date AS DATE) AS opened_date,
       CAST(offer_accepted_date AS DATE) AS offer_accepted_date, CAST(join_date AS DATE) AS join_date,
       candidates, offer_declines, hired_emp_id
FROM read_csv('data/raw/requisitions.csv', header = true, types = {'hired_emp_id': 'VARCHAR'});

CREATE OR REPLACE TABLE stg_utilisation AS
SELECT emp_id, CAST(month AS DATE) AS month, billable_utilisation
FROM read_csv('data/raw/utilisation.csv', header = true);

CREATE OR REPLACE TABLE stg_enps AS
SELECT response_id, CAST(survey_date AS DATE) AS survey_date, department, location, level_band, score
FROM read_csv('data/raw/enps_responses.csv', header = true);

CREATE OR REPLACE TABLE stg_salary_bands AS
SELECT fiscal_year, level, department, location, band_min, band_mid, band_max
FROM read_csv('data/raw/salary_bands.csv', header = true);

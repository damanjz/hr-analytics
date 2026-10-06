"""Flight-risk model and pay-equity regression.

Reads model_snapshots and fact_employee_month from DuckDB and writes back:
model_metrics, model_calibration, model_importance, model_fairness, risk_scores, pay_equity.

Validation is out-of-time: train on snapshots up to Sep 2023, test on snapshots from
Mar 2024 (labels never overlap). The chosen model is refit on all observable snapshots
and used to score everyone on the books at 31 March 2026.
"""
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

SEED = 7
TRAIN_END = "2023-09-30"
TEST_START = "2024-03-31"
SCORE_DATE = "2026-03-31"
PROTECTED = {"gender", "home_region", "university_tier", "birth_year", "age"}


def make_models(numeric, categorical):
    def prep(scale):
        num = [("impute", SimpleImputer(strategy="median", add_indicator=True))]
        if scale:
            num.append(("scale", StandardScaler()))
        return ColumnTransformer([("num", Pipeline(num), numeric),
                                  ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), categorical)])
    return {
        "Logistic regression": Pipeline([("prep", prep(True)),
                                         ("clf", LogisticRegression(max_iter=2000, C=0.5))]),
        "Gradient boosting": Pipeline([("prep", prep(False)),
                                       ("clf", HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05,
                                                                              max_iter=300, random_state=SEED))]),
    }


def top_decile_capture(y, p):
    cut = np.quantile(p, 0.9)
    return y[p >= cut].sum() / max(y.sum(), 1)


def evaluate(name, y, p):
    return dict(model=name, roc_auc=roc_auc_score(y, p), pr_auc=average_precision_score(y, p),
                brier=brier_score_loss(y, p), base_rate=y.mean(), top_decile_capture=top_decile_capture(y, p))


def ols(X, y):
    """Least squares with classical standard errors."""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    sigma2 = resid @ resid / (len(y) - X.shape[1])
    se = np.sqrt(np.diag(sigma2 * np.linalg.inv(X.T @ X)))
    return beta, se


def pay_equity(con):
    cur = con.sql(f"""
        SELECT emp_id, gender, level, department, location, last_rating, tenure_months, annual_ctc
        FROM fact_employee_month WHERE month_end = DATE '{SCORE_DATE}' AND gender IN ('Woman', 'Man')
        ORDER BY emp_id
    """).df()
    women, men = cur[cur.gender == "Woman"], cur[cur.gender == "Man"]
    raw = women.annual_ctc.median() / men.annual_ctc.median()
    d = cur.assign(rating=cur.last_rating.fillna(cur.last_rating.median()))
    X = pd.get_dummies(d[["level", "department", "location"]], drop_first=True, dtype=float)
    X["rating"] = d.rating
    X["tenure_years"] = d.tenure_months / 12
    X["woman"] = (d.gender == "Woman").astype(float)
    X.insert(0, "const", 1.0)
    beta, se = ols(X.astype(float).to_numpy(), np.log(d.annual_ctc.astype(float).to_numpy()))
    i = list(X.columns).index("woman")
    adj, lo, hi = (np.exp(beta[i]), np.exp(beta[i] - 1.96 * se[i]), np.exp(beta[i] + 1.96 * se[i]))
    return pd.DataFrame([
        ("Unadjusted pay ratio (women / men, median)", raw, None, None, len(cur)),
        ("Adjusted pay ratio (same level, department, location, rating, tenure)", adj, lo, hi, len(cur)),
    ], columns=["metric", "value", "ci_low", "ci_high", "employees"])


def run(con):
    feats = con.sql("SELECT feature, kind FROM model_features").df()
    assert not PROTECTED & set(feats.feature), "protected attribute in feature list"
    numeric = feats[feats.kind == "numeric"].feature.tolist()
    categorical = feats[feats.kind == "categorical"].feature.tolist()
    cols = numeric + categorical

    # Fixed row order: DuckDB returns rows in any order, and the model's internal validation split follows it
    snaps = con.sql("SELECT * FROM model_snapshots WHERE label_observable AND NOT censored "
                    "ORDER BY snapshot_date, emp_id").df()
    snaps["snapshot_date"] = pd.to_datetime(snaps.snapshot_date)
    train = snaps[snaps.snapshot_date <= TRAIN_END]
    test = snaps[snaps.snapshot_date >= TEST_START]

    metrics, preds = [], {}
    for name, model in make_models(numeric, categorical).items():
        model.fit(train[cols], train.left_within_6m)
        p = model.predict_proba(test[cols])[:, 1]
        preds[name] = (model, p)
        metrics.append(evaluate(name, test.left_within_6m.to_numpy(), p))
    metrics = pd.DataFrame(metrics)
    best_auc = metrics.roc_auc.max()
    # prefer the interpretable model unless the other is clearly better
    lr_auc = metrics.loc[metrics.model == "Logistic regression", "roc_auc"].item()
    chosen = "Logistic regression" if best_auc - lr_auc < 0.01 else metrics.loc[metrics.roc_auc.idxmax(), "model"]
    metrics["chosen"] = metrics.model == chosen
    metrics["train_rows"], metrics["test_rows"] = len(train), len(test)
    metrics["train_end"], metrics["test_start"] = TRAIN_END, TEST_START

    model, p_test = preds[chosen]
    y_test = test.left_within_6m.to_numpy()
    bins = pd.qcut(p_test, 10, labels=False, duplicates="drop")
    calib = pd.DataFrame({"decile": bins + 1, "predicted": p_test, "actual": y_test}) \
        .groupby("decile").agg(predicted=("predicted", "mean"), actual=("actual", "mean"), employees=("actual", "size")).reset_index()

    imp = permutation_importance(model, test[cols], y_test, scoring="roc_auc", n_repeats=5, random_state=SEED)
    importance = pd.DataFrame({"feature": cols, "importance": imp.importances_mean, "std": imp.importances_std}) \
        .sort_values("importance", ascending=False)

    # fairness: the model never sees gender; check it treats groups alike anyway
    g = con.sql("SELECT emp_id, month_end AS snapshot_date, gender FROM fact_employee_month").df()
    g["snapshot_date"] = pd.to_datetime(g.snapshot_date)
    t = test.assign(score=p_test).merge(g, on=["emp_id", "snapshot_date"])
    top_cut = np.quantile(p_test, 0.9)
    fairness = []
    for grp, d in t[t.gender.isin(["Woman", "Man"])].groupby("gender"):
        fairness.append(dict(gender=grp, employees=len(d), actual_rate=d.left_within_6m.mean(), mean_score=d.score.mean(),
                             roc_auc=roc_auc_score(d.left_within_6m, d.score), flagged_share=(d.score >= top_cut).mean(),
                             recall_in_top_decile=d.loc[d.score >= top_cut, "left_within_6m"].sum() / max(d.left_within_6m.sum(), 1)))
    fairness = pd.DataFrame(fairness)

    # refit on all observable history, then score everyone on the books today
    final = make_models(numeric, categorical)[chosen].fit(snaps[cols], snaps.left_within_6m)
    now = con.sql(f"SELECT * FROM model_snapshots WHERE snapshot_date = DATE '{SCORE_DATE}' ORDER BY emp_id").df()
    now["risk_score"] = final.predict_proba(now[cols])[:, 1]
    hi, mid = np.quantile(now.risk_score, 0.9), np.quantile(now.risk_score, 0.7)
    now["risk_band"] = np.where(now.risk_score >= hi, "High", np.where(now.risk_score >= mid, "Medium", "Low"))
    now["risk_percentile"] = now.risk_score.rank(pct=True)
    now["drivers"] = drivers(final, now[cols], snaps)
    risk = now[["emp_id", "risk_score", "risk_percentile", "risk_band", "drivers"] + cols]

    for name, df in dict(model_metrics=metrics, model_calibration=calib, model_importance=importance,
                         model_fairness=fairness, risk_scores=risk, pay_equity=pay_equity(con)).items():
        con.register("tmp_df", df)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM tmp_df")
        con.unregister("tmp_df")
    return metrics


# Only levers HR can act on are reported as drivers, with a "typical" reference value for each.
LEVERS = {"compa_ratio": ("pay below market", "median"), "salary_growth_24m": ("slow pay growth", "median"),
          "months_since_promotion": ("long wait for promotion", "median"), "commute_minutes": ("long commute", "median"),
          "utilisation_3m": ("low utilisation", "median"), "bench_months_12m": ("time on bench", 0),
          "rating_change": ("falling rating", 0), "tenure_stage": ("1 to 3 years in", "3 to 6 years")}


def drivers(model, X, history):
    """Model-agnostic: reset one lever at a time to a typical value and measure how far the risk falls."""
    base = model.predict_proba(X)[:, 1]
    drops = {}
    for col, (label, ref) in LEVERS.items():
        X2 = X.copy()
        X2[col] = history[col].median() if ref == "median" else ref
        drops[label] = base - model.predict_proba(X2)[:, 1]
    d = pd.DataFrame(drops)
    out = []
    for _, row in d.iterrows():
        top = row[row > 0.005].sort_values(ascending=False).index[:3]
        out.append(", ".join(top))
    return out

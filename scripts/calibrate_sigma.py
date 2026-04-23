"""
MLE calibration script: estimate sigma(city, horizon_bin) from stored
(forecast, actual) pairs, and AR(1) phi per city.

Run weekly once you have >=14 days of data.

Usage:
    python scripts/calibrate_sigma.py
    python scripts/calibrate_sigma.py --min-days 30
"""
import math
import sys
import argparse
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.dwtrader import DWTraderDB


def _mle_sigma(errors: list) -> float:
    """MLE estimator for sigma: sqrt((1/N) sum(e^2))."""
    if not errors:
        return float("nan")
    return math.sqrt(sum(e ** 2 for e in errors) / len(errors))


def _qlike_loss(errors: list, sigma: float) -> float:
    """
    QLIKE loss: L(σ) = log(σ²) + (1/N)·Σ(eᵢ²/σ²).
    Proper scoring rule for variance forecasts; penalises overconfident σ
    (too small) more severely than MSE-based MLE.
    The MLE estimator minimises QLIKE, but QLIKE is useful as a held-out
    evaluation metric to compare two sigma estimates on the same error set.
    Lower is better.
    """
    if not errors or sigma <= 0:
        return float("nan")
    s2 = sigma ** 2
    return math.log(s2) + sum(e ** 2 for e in errors) / (len(errors) * s2)


def _brier_by_group(db: DWTraderDB):
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT city, horizon_bin, COUNT(*) n,
                   AVG(brier_score) avg_brier,
                   AVG(predicted_p) avg_p
            FROM predictions
            WHERE actual_outcome IS NOT NULL
              AND city IS NOT NULL
              AND horizon_bin IS NOT NULL
            GROUP BY city, horizon_bin
            ORDER BY city, horizon_bin
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _ar1_residuals(db: DWTraderDB):
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT city, target_date, error_f FROM ar1_residuals ORDER BY city, target_date"
        ).fetchall()
    by_city = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r["error_f"])
    return dict(by_city)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-days", type=int, default=14,
                        help="Minimum data points required to report an estimate")
    args = parser.parse_args()

    db = DWTraderDB()

    print("\n" + "=" * 65)
    print("CALIBRATION REPORT")
    print("=" * 65)

    # -- AR(1) phi per city ---------------------------------------------
    print("\n-- AR(1) phi estimates (OLS on daily forecast residuals) --")
    print(f"  {'City':<10} {'N days':>7}  {'phi':>8}  {'Status'}")
    print(f"  {'-'*50}")
    ar1_data = _ar1_residuals(db)
    any_ar1 = False
    for city, errors in sorted(ar1_data.items()):
        phi = db.get_ar1_phi_estimate(city, min_days=args.min_days)
        status = "OK use" if phi is not None else f"need >={args.min_days} days"
        phi_str = f"{phi:.3f}" if phi is not None else "  n/a"
        print(f"  {city:<10} {len(errors):>7}  {phi_str:>8}  {status}")
        any_ar1 = True
    if not any_ar1:
        print("  No AR(1) residual data yet. Run trade cycle first.")

    # -- sigma MLE + QLIKE per city / horizon ------------------------------
    print("\n-- sigma MLE from predictions + weather_actuals --")
    print(f"  (queries ar1_residuals as proxy - true per-horizon MLE needs")
    print(f"   forecast stored at prediction time, not yet available)")
    print(f"\n  {'City':<10} {'N errors':>9}  {'sigma MLE (F)':>12}  {'QLIKE(MLE)':>11}  {'QLIKE(4.0F)':>11}  Flag")
    print(f"  {'-'*70}")
    for city, errors in sorted(ar1_data.items()):
        if len(errors) < args.min_days:
            print(f"  {city:<10} {len(errors):>9}  {'n/a':>12}  (need >={args.min_days})")
            continue
        sigma = _mle_sigma(errors)
        qlike_mle     = _qlike_loss(errors, sigma)
        qlike_current = _qlike_loss(errors, 4.0)
        delta = sigma - 4.0
        flag = "^ underestimated" if delta > 0.5 else ("v overestimated" if delta < -0.5 else "~ calibrated")
        print(f"  {city:<10} {len(errors):>9}  {sigma:>12.2f}  {qlike_mle:>11.4f}  {qlike_current:>11.4f}  {flag}")

    # -- Brier scores --------------------------------------------------
    print("\n-- Brier scores by city / horizon --")
    rows = _brier_by_group(db)
    if rows:
        print(f"  {'City':<10} {'Horizon':<10} {'N':>5} {'Avg Brier':>10} {'Avg p':>8}")
        print(f"  {'-'*50}")
        for r in rows:
            flag = "OK" if r["avg_brier"] < 0.10 else ("!!" if r["avg_brier"] < 0.20 else "XX")
            print(f"  {r['city']:<10} {r['horizon_bin'] or 'unknown':<10} "
                  f"{r['n']:>5} {r['avg_brier']:>10.4f} {r['avg_p']:>8.3f}  {flag}")
        overall = db.get_brier_summary()
        print(f"\n  Overall Brier: {overall.get('avg_brier', float('nan')):.4f} "
              f"(n={overall.get('n', 0)})  target <0.10")
    else:
        print("  No settled predictions yet. Run check_outcomes.py first.")

    print("\n" + "=" * 65 + "\n")


if __name__ == "__main__":
    main()

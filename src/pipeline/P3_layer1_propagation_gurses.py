"""Fit source-processed Gürses/AERPAW path loss; hold out complete flights.

This estimates a campaign-scoped log-distance propagation model. It does not
turn path loss into a calibrated C2 goodput/delay/loss model.
"""
import argparse
import json
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("P3_Gurses_Calibrated")


def _freq_ghz(values):
    values = pd.to_numeric(values, errors="coerce")
    return pd.Series(np.select([values > 1e6, values > 100],
                               [values / 1e9, values / 1e3], default=values), index=values.index)


def _score(actual, predicted):
    err = np.asarray(predicted) - np.asarray(actual)
    return {"n": int(len(err)), "rmse_db": float(np.sqrt(np.mean(err ** 2))),
            "mae_db": float(np.mean(np.abs(err))), "bias_pred_minus_measured_db": float(np.mean(err))}


def _fit_ci(train, freq):
    pl0 = 20.0 * math.log10(4.0 * math.pi * freq * 1e9 / 299792458.0)
    x = np.log10(train.distance_m.to_numpy())
    y = train.path_loss_db.to_numpy()
    denom = float(np.dot(x, x))
    if denom <= 0:
        raise ValueError("No distance leverage for CI fit")
    n = float(np.dot(x, y - pl0) / (10.0 * denom))
    if not np.isfinite(n) or n <= 0:
        raise ValueError(f"Nonphysical fitted path-loss exponent n={n}")
    return pl0, n


def fit(input_csv, output_dir, test_size=0.25, seed=42, repeats=30):
    src = Path(input_csv)
    df = pd.read_csv(src)
    required = {"flight_id", "dist", "fit_path_loss_db", "center_freq", "pl_valid",
                "cal_tx_ref_source", "cal_rx_ref_source", "cal_tx_extrapolated", "cal_rx_extrapolated"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Expected consolidated, provenance-checked source post-processor fields: {missing}; run P2 collector")
    df["flight_id"] = df.flight_id.astype(str)
    df["distance_m"] = pd.to_numeric(df.dist, errors="coerce")
    df["path_loss_db"] = pd.to_numeric(df.fit_path_loss_db, errors="coerce")
    df["frequency_ghz"] = _freq_ghz(df.center_freq)
    # Normalize small per-capture center-frequency offsets to 10-MHz carrier
    # labels so one physical carrier does not create many near-duplicate fits.
    df["frequency_group_ghz"] = (df.frequency_ghz * 100).round() / 100
    valid = df.pl_valid.astype(str).str.lower().isin({"true", "1", "1.0"})
    valid &= df.cal_tx_ref_source.isin({"power_refs_csv", "uhd_calibration"})
    valid &= df.cal_rx_ref_source.isin({"power_refs_csv", "uhd_calibration"})
    for col in ("cal_tx_extrapolated", "cal_rx_extrapolated"):
        valid &= ~df[col].astype(str).str.lower().isin({"true", "1", "yes"})
    df = df[valid & np.isfinite(df.distance_m) & np.isfinite(df.path_loss_db)
            & np.isfinite(df.frequency_ghz) & (df.distance_m > 1) & (df.frequency_ghz > 0)].copy()
    if df.flight_id.nunique() < 3 or len(df) < 30:
        raise ValueError("Require >=30 valid samples from >=3 independent flights for flight-grouped evaluation")

    models = {}
    for freq in sorted(df.frequency_group_ghz.unique()):
        part = df[df.frequency_group_ghz == freq].copy()
        freq = float(part.frequency_ghz.median())
        if part.flight_id.nunique() < 3 or len(part) < 30 or part.distance_m.nunique() < 5:
            LOG.warning("Skip %.4f GHz: insufficient independent flights/distance support", freq)
            continue
        groups = part.flight_id.to_numpy()
        split_metrics = []
        split_fi_metrics = []
        split_flights = []
        # Repeated grouped holdouts quantify sensitivity to which whole flights
        # are withheld. They are not independent additional experiments.
        for i in range(repeats):
            splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed + i)
            train_i, test_i = next(splitter.split(part, groups=groups))
            train, test = part.iloc[train_i], part.iloc[test_i]
            if train.flight_id.nunique() < 2 or test.flight_id.nunique() < 1:
                continue
            try:
                pl0, n = _fit_ci(train, freq)
            except ValueError:
                continue
            pred = pl0 + 10.0 * n * np.log10(test.distance_m.to_numpy())
            split_metrics.append(_score(test.path_loss_db.to_numpy(), pred))
            fi_slope, fi_intercept = np.polyfit(np.log10(train.distance_m.to_numpy()), train.path_loss_db.to_numpy(), 1)
            fi_pred = fi_intercept + fi_slope * np.log10(test.distance_m.to_numpy())
            split_fi_metrics.append(_score(test.path_loss_db.to_numpy(), fi_pred))
            split_flights.append({"train": sorted(train.flight_id.unique().tolist()),
                                  "heldout": sorted(test.flight_id.unique().tolist())})
        if len(split_metrics) < min(5, repeats):
            LOG.warning("Skip %.4f GHz: only %d valid grouped splits", freq, len(split_metrics))
            continue
        # Refit deployment parameters only after obtaining held-out estimates.
        pl0_all, n_all = _fit_ci(part, freq)
        # Free-intercept log-distance comparator (FI) is reported as a baseline.
        beta, alpha = np.polyfit(np.log10(part.distance_m.to_numpy()), part.path_loss_db.to_numpy(), 1)
        metric_summary = {}
        for key in ("rmse_db", "mae_db", "bias_pred_minus_measured_db"):
            values = [m[key] for m in split_metrics]
            metric_summary[key] = {"mean": float(np.mean(values)), "std": float(np.std(values, ddof=1)),
                                   "p05": float(np.percentile(values, 5)), "p95": float(np.percentile(values, 95))}
        fi_metric_summary = {}
        for key in ("rmse_db", "mae_db", "bias_pred_minus_measured_db"):
            values = [m[key] for m in split_fi_metrics]
            fi_metric_summary[key] = {"mean": float(np.mean(values)), "std": float(np.std(values, ddof=1)),
                                      "p05": float(np.percentile(values, 5)), "p95": float(np.percentile(values, 95))}
        altitude = pd.to_numeric(part.get("alt", pd.Series(index=part.index, dtype=float)), errors="coerce")
        models[f"{freq:.4f}"] = {
            "frequency_ghz": float(freq), "reference_distance_m": 1.0,
            "path_loss_at_1m_db_fspl": float(pl0_all), "path_loss_exponent_n": float(n_all),
            "fit_samples_all_flights": int(len(part)), "flight_count": int(part.flight_id.nunique()),
            "repeated_grouped_holdout_repeats": int(len(split_metrics)),
            "holdout_metrics_db_mean_std_percentiles": metric_summary,
            "free_intercept_holdout_metrics_db_mean_std_percentiles": fi_metric_summary,
            "holdout_splits": split_flights,
            "free_intercept_baseline_refit_all_data": {"alpha_db": float(alpha), "beta": float(beta)},
            "domain": {"distance_min_m": float(part.distance_m.min()), "distance_max_m": float(part.distance_m.max()),
                       "altitude_m_min": float(altitude.min()) if altitude.notna().any() else None,
                       "altitude_m_max": float(altitude.max()) if altitude.notna().any() else None,
                       "trajectory_types": sorted(part["flight_id"].unique().tolist())}}
    if not models:
        raise ValueError("No carrier had enough calibrated flight groups for fit and grouped validation")
    provenance = {"fit_measurement_columns": sorted(df.fit_measurement_column.dropna().astype(str).unique().tolist())
                  if "fit_measurement_column" in df else [],
                  "tx_reference_sources": sorted(df.cal_tx_ref_source.dropna().astype(str).unique().tolist()),
                  "rx_reference_sources": sorted(df.cal_rx_ref_source.dropna().astype(str).unique().tolist())}
    result = {
        "model_type": "gurses_aerpaw_measured_path_loss_log_distance_CI",
        "calibration_status": "source_processed_path_loss_fit_with_repeated_flight_grouped_holdout",
        "absolute_power_calibrated": False,
        "measurement": "source post-processor measured path loss; not calibrated received power or end-to-end C2 KPI",
        "source_csv": str(src.resolve()), "provenance": provenance, "frequency_fits": models,
        "limitations": ["Use only inside the reported carrier, distance, altitude, trajectory and campaign support.",
                        "Repeated holdout splits reuse flights and do not increase independent sample count.",
                        "The one-carrier/one-campaign dataset does not establish generalization across frequencies or environments.",
                        "AADM is separate external evaluation, not part of this fit.",
                        "No calibrated RF-to-delay/loss/goodput mapping is produced."]}
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "layer1_gurses_calibrated.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    LOG.info("Wrote %s", out / "layer1_gurses_calibrated.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_csv", required=True)
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--test_size", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--repeats", type=int, default=30)
    a = ap.parse_args()
    fit(a.input_csv, a.output_dir, a.test_size, a.seed, a.repeats)

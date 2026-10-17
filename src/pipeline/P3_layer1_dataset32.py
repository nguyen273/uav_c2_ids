"""Fit and flight-holdout-evaluate a Dataset-32 path-loss distance slope.

The model removes an intercept for each flight before estimating the common
log-distance slope. Scores are therefore within-flight relative errors; the
artifact does not claim cross-flight absolute path-loss prediction.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _slope(frame: pd.DataFrame) -> float:
    x = np.log10(frame["distance_m"].to_numpy(dtype=float))
    y = frame["path_loss_db"].to_numpy(dtype=float)
    groups = frame["flight_id"].to_numpy()
    x_centered = x - pd.Series(x).groupby(groups).transform("mean").to_numpy()
    y_centered = y - pd.Series(y).groupby(groups).transform("mean").to_numpy()
    counts = pd.Series(groups).map(pd.Series(groups).value_counts()).to_numpy(dtype=float)
    weights = 1.0 / counts
    denominator = float(np.dot(weights, x_centered ** 2))
    if denominator <= 1e-12:
        raise ValueError("Insufficient within-flight distance variation")
    beta = float(np.dot(weights * x_centered, y_centered) / denominator)
    if not np.isfinite(beta) or beta <= 0:
        raise ValueError(f"Expected path loss to increase with distance; fitted slope={beta}")
    return beta


def _metrics(actual: np.ndarray, predicted: np.ndarray) -> dict:
    err = np.asarray(predicted, dtype=float) - np.asarray(actual, dtype=float)
    return {"n_rows": int(len(err)), "rmse_db": float(np.sqrt(np.mean(err ** 2))),
            "mae_db": float(np.mean(np.abs(err)))}


def fit(input_csv: Path, output_dir: Path) -> Path:
    data = pd.read_csv(input_csv)
    required = {"flight_id", "uav2ground_dist", "path_loss_db", "frequency_hz", "rx_altitude_agl_m"}
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError(f"Dataset-32 consolidated input missing fields: {missing}")
    for field in required - {"flight_id"}:
        data[field] = pd.to_numeric(data[field], errors="coerce")
    data["flight_id"] = data["flight_id"].astype(str)
    data = data.replace([np.inf, -np.inf], np.nan).dropna(subset=list(required)).copy()
    data = data.rename(columns={"uav2ground_dist": "distance_m"})
    if (data["distance_m"] <= 0).any():
        raise ValueError("All distance values must be positive")
    if not np.allclose(data["frequency_hz"], 3_400_000_000, rtol=0, atol=1.0):
        raise ValueError("This fitter is restricted to Dataset-32's measured 3.4 GHz carrier")
    flights = sorted(data["flight_id"].unique().tolist())
    if len(flights) != 4 or len(data) < 100:
        raise ValueError(f"Expected all four Dataset-32 flights and >=100 rows; found {len(flights)} flights/{len(data)} rows")
    if any(data.loc[data.flight_id == f, "distance_m"].nunique() < 10 for f in flights):
        raise ValueError("Each flight needs at least 10 distinct distance values")

    folds = []
    for heldout in flights:
        train = data[data.flight_id != heldout]
        test = data[data.flight_id == heldout]
        beta = _slope(train)
        # Offset is estimated from the held-out flight only to score its
        # distance-dependent residual. This is a trend validation, not a
        # deployable absolute prediction for a new flight.
        x_test = np.log10(test.distance_m.to_numpy(dtype=float))
        y_test = test.path_loss_db.to_numpy(dtype=float)
        centered_y = y_test - y_test.mean()
        centered_pred = beta * (x_test - x_test.mean())
        folds.append({"heldout_flight": heldout,
                      "training_flights": [f for f in flights if f != heldout],
                      "path_loss_slope_db_per_decade": beta,
                      "path_loss_exponent_n": beta / 10.0,
                      "within_flight_demeaned_metrics": _metrics(centered_y, centered_pred)})

    beta = _slope(data)
    per_flight = {}
    for flight, part in data.groupby("flight_id"):
        x = np.log10(part.distance_m.to_numpy(dtype=float))
        y = part.path_loss_db.to_numpy(dtype=float)
        per_flight[flight] = {
            "rows": int(len(part)), "campaign_id": str(part.campaign_id.iloc[0]) if "campaign_id" in part else None,
            "distance_min_m": float(part.distance_m.min()), "distance_max_m": float(part.distance_m.max()),
            "altitude_agl_min_m": float(part.rx_altitude_agl_m.min()),
            "altitude_agl_max_m": float(part.rx_altitude_agl_m.max()),
            "mean_path_loss_db": float(y.mean()),
            "flight_specific_slope_db_per_decade": float(np.polyfit(x, y, 1)[0]),
        }
    folds_metrics = [f["within_flight_demeaned_metrics"] for f in folds]
    all_centered_actual, all_centered_pred = [], []
    for flight, part in data.groupby("flight_id"):
        x = np.log10(part.distance_m.to_numpy(dtype=float))
        y = part.path_loss_db.to_numpy(dtype=float)
        all_centered_actual.extend(y - y.mean())
        all_centered_pred.extend(beta * (x - x.mean()))

    artifact = {
        "model_type": "dataset32_flight_fixed_effects_log_distance_path_loss_slope",
        "calibration_status": "dataset32_empirical_path_loss_distance_slope_flight_grouped_holdout",
        "absolute_power_calibrated": False,
        "absolute_path_loss_prediction_supported": False,
        "frequency_ghz": 3.4, "reference_distance_m": None,
        "path_loss_slope_db_per_decade": beta, "path_loss_exponent_n": beta / 10.0,
        "fit_equation": "path_loss_db = flight_specific_offset_db + path_loss_slope_db_per_decade * log10(distance_3d_m) + residual",
        "fit_method": "within-flight centered least squares with equal total weight per flight",
        "validation_method": "exact leave-one-flight-out; each holdout is demeaned within that flight before scoring; scores validate trend only",
        "validation_summary": {
            "flight_count": len(flights), "row_count": int(len(data)), "folds": folds,
            "mean_fold_rmse_db": float(np.mean([m["rmse_db"] for m in folds_metrics])),
            "mean_fold_mae_db": float(np.mean([m["mae_db"] for m in folds_metrics])),
            "pooled_within_flight_demeaned_metrics": _metrics(np.asarray(all_centered_actual), np.asarray(all_centered_pred)),
        },
        "flights": per_flight,
        "domain": {"distance_min_m": float(data.distance_m.min()), "distance_max_m": float(data.distance_m.max()),
                   "altitude_agl_min_m": float(data.rx_altitude_agl_m.min()),
                   "altitude_agl_max_m": float(data.rx_altitude_agl_m.max()),
                   "frequency_ghz": 3.4, "environment": "AERPAW Lake Wheeler rural testbed; fixed LW1 transmitter"},
        "source": {"dataset_id": "AERPAW Dataset-32", "doi": "10.5061/dryad.2ngf1vj4r",
                   "input_csv": str(input_csv.resolve()), "rows_used": int(len(data)),
                   "measurement_semantics": "source-provided processed path_loss_db; raw-IQ not recalculated"},
        "limitations": [
            "Four flights are the independent grouping units; rows within each flight are temporally dependent.",
            "Flight-specific offsets are removed for slope estimation and holdout scoring; absolute path loss on a new flight is unsupported.",
            "Dataset documentation lists processed path_loss_db but does not establish traceable TX/RX power calibration uncertainty.",
            "Coverage is limited to 3.4 GHz, approximately 60 m UAV AGL, the fixed LW1 site and observed distance span.",
            "This path-loss slope does not calibrate received power, SNR, fading, PER, delay, packet loss or goodput.",
        ],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "layer1_dataset32_pathloss_fit.json"
    target.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input_csv", required=True)
    parser.add_argument("--output_dir", required=True)
    args = parser.parse_args()
    print(fit(Path(args.input_csv), Path(args.output_dir)))

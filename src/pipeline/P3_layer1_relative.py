"""Fit a flight-fixed-effects relative received-level model from raw IQ.

The response is IQ RMS in dBFS-like units. This estimates only the within-flight
trend with distance; it is not absolute received power or calibrated path loss.
Each flight is held out once, and held-out scores compare demeaned within-flight
trends so unknown per-flight receiver offsets cannot leak into the score.
"""
import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("P3_Relative_Layer1")


def _score(actual, predicted):
    error = np.asarray(predicted, dtype=float) - np.asarray(actual, dtype=float)
    return {"n": int(len(error)), "rmse_dbfs": float(np.sqrt(np.mean(error ** 2))),
            "mae_dbfs": float(np.mean(np.abs(error))), "bias_pred_minus_measured_dbfs": float(np.mean(error))}


def _demeaned_slope(frame):
    """Estimate a common slope after removing each flight's unknown level offset."""
    x = np.log10(frame.distance_m.to_numpy(dtype=float))
    y = frame.iq_power_dbfs.to_numpy(dtype=float)
    groups = frame.flight_id.to_numpy()
    x_centered = x - pd.Series(x).groupby(groups).transform("mean").to_numpy()
    y_centered = y - pd.Series(y).groupby(groups).transform("mean").to_numpy()
    # Equal total weight per flight prevents longer captures dominating the
    # campaign-level slope simply because they contain more samples.
    counts = pd.Series(groups).map(pd.Series(groups).value_counts()).to_numpy(dtype=float)
    weights = 1.0 / counts
    denominator = float(np.dot(weights, x_centered ** 2))
    if denominator <= 1e-12:
        raise ValueError("Insufficient within-flight distance variation")
    slope = float(np.dot(weights * x_centered, y_centered) / denominator)
    if not np.isfinite(slope) or slope >= 0:
        raise ValueError(f"Expected received level to decrease with distance; fitted slope={slope}")
    return slope


def fit(input_csv, output_dir):
    src = Path(input_csv)
    df = pd.read_csv(src)
    required = {"flight_id", "distance_m", "iq_power_dbfs", "frequency_ghz"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"Input must be P2 raw-IQ descriptor CSV; missing {missing}")
    for col in ("distance_m", "iq_power_dbfs", "frequency_ghz"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["flight_id"] = df.flight_id.astype(str)
    df = df.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["flight_id", "distance_m", "iq_power_dbfs", "frequency_ghz"])
    df = df[(df.distance_m > 1) & (df.frequency_ghz > 0)].copy()
    # Cluster small metadata frequency offsets into nominal 10-MHz carrier bins.
    df["frequency_group_ghz"] = (df.frequency_ghz * 100).round() / 100
    if len(df) < 30:
        raise ValueError("Require at least 30 finite captures")

    fits = {}
    skipped_frequency_groups = []
    for group_freq in sorted(df.frequency_group_ghz.unique()):
        part = df[df.frequency_group_ghz == group_freq].copy()
        if part.flight_id.nunique() < 3 or len(part) < 30 or part.distance_m.nunique() < 5:
            LOG.warning("Skip %.4f GHz: require >=3 flights, >=30 captures and >=5 distances", group_freq)
            skipped_frequency_groups.append({"frequency_group_ghz": float(group_freq),
                                             "reason": "insufficient independent flights, captures, or distance support",
                                             "flight_count": int(part.flight_id.nunique()),
                                             "capture_count": int(len(part))})
            continue
        scores, splits = [], []
        # With only three independent flights per carrier in this campaign,
        # use exact leave-one-flight-out folds instead of repeated random
        # resamples that would duplicate the same few holdouts.
        for heldout_flight in sorted(part.flight_id.unique()):
            test_mask = part.flight_id == heldout_flight
            train, test = part.loc[~test_mask], part.loc[test_mask]
            if train.flight_id.nunique() < 2 or test.flight_id.nunique() < 1:
                continue
            try:
                slope = _demeaned_slope(train)
            except ValueError:
                continue
            # The held-out flight mean is removed only for scoring its relative
            # trend. This is not a single-sample, absolute-level prediction.
            pred = slope * np.log10(test.distance_m.to_numpy(dtype=float))
            actual = test.iq_power_dbfs.to_numpy(dtype=float)
            centered_pred = pred - pd.Series(pred).groupby(test.flight_id.to_numpy()).transform("mean").to_numpy()
            centered_actual = actual - pd.Series(actual).groupby(test.flight_id.to_numpy()).transform("mean").to_numpy()
            scores.append(_score(centered_actual, centered_pred))
            splits.append({"train_flights": sorted(train.flight_id.unique().tolist()),
                           "heldout_flights": sorted(test.flight_id.unique().tolist()),
                           "relative_path_loss_exponent_n": float(-slope / 10.0)})
        if len(scores) < 3:
            LOG.warning("Skip %.4f GHz: only %d valid leave-one-flight-out folds", group_freq, len(scores))
            skipped_frequency_groups.append({"frequency_group_ghz": float(group_freq),
                                             "reason": "fewer than three valid LOFO fits; inspect flight-level slopes and distance leverage",
                                             "valid_holdout_folds": int(len(scores)),
                                             "flight_count": int(part.flight_id.nunique())})
            continue
        slope_all = _demeaned_slope(part)
        metric_summary = {}
        for key in ("rmse_dbfs", "mae_dbfs", "bias_pred_minus_measured_dbfs"):
            vals = np.array([s[key] for s in scores], dtype=float)
            metric_summary[key] = {"mean": float(vals.mean()), "std": float(vals.std(ddof=1)),
                                   "p05": float(np.percentile(vals, 5)), "p95": float(np.percentile(vals, 95))}
        carrier = float(part.frequency_ghz.median())
        holdout_n = [float(s["relative_path_loss_exponent_n"]) for s in splits]
        review_flags = []
        if -slope_all / 10.0 < 2.0:
            review_flags.append("fitted relative slope is shallower than free-space n=2; inspect noise floor, capture response, antenna pattern, and geometry")
        if holdout_n and max(holdout_n) - min(holdout_n) > 1.0:
            review_flags.append("large between-flight variation in leave-one-flight-out exponents")
        fits[f"{carrier:.4f}"] = {
            "frequency_ghz": carrier,
            "relative_path_loss_exponent_n": float(-slope_all / 10.0),
            "received_level_slope_dbfs_per_decade": slope_all,
            "fit_samples": int(len(part)), "flight_count": int(part.flight_id.nunique()),
            "heldout_metrics_within_flight_relative_level": metric_summary,
            "leave_one_flight_out_folds": len(scores), "holdout_splits": splits,
            "review_flags": review_flags,
            "domain": {"distance_min_m": float(part.distance_m.min()),
                       "distance_max_m": float(part.distance_m.max()),
                       "flight_ids": sorted(part.flight_id.unique().tolist()),
                       "waveforms": sorted(part.waveform.dropna().astype(str).unique().tolist()) if "waveform" in part else [],
                       "sample_rates_hz": sorted(pd.to_numeric(part.sample_rate_hz, errors="coerce").dropna().unique().tolist()) if "sample_rate_hz" in part else []}}
    if not fits:
        raise ValueError("No carrier had enough independent flights and distance support")

    result = {
        "model_type": "relative_received_level_log_distance_flight_fixed_effects",
        "calibration_status": "relative_empirical_trend_only_grouped_flight_holdout",
        "absolute_power_calibrated": False,
        "path_loss_calibrated": False,
        "measurement": "raw IQ RMS in dBFS-like units; not dBm, RSRP, channel gain or absolute path loss",
        "validation_semantics": "exact leave-one-flight-out; one equal-weighted flight is held out at a time; observed and predicted levels are demeaned within the held-out flight to score relative trend only",
        "source_csv": str(src.resolve()), "frequency_fits": fits,
        "source_summary": {"capture_count_used": int(len(df)),
                           "flight_count": int(df.flight_id.nunique()),
                           "declared_tx_gain_reference_values_db": sorted(pd.to_numeric(df.get("tx_gain_ref_db", pd.Series(dtype=float)), errors="coerce").dropna().unique().tolist()),
                           "declared_rx_gain_reference_values_db": sorted(pd.to_numeric(df.get("rx_gain_ref_db", pd.Series(dtype=float)), errors="coerce").dropna().unique().tolist()),
                           "metadata_reference_status": "SigMF declarations only; not accepted as traceable TX/RX calibration"},
        "excluded_frequency_groups": skipped_frequency_groups,
        "limitations": [
            "Per-flight hardware and receiver offsets are not estimated for a new flight; absolute level prediction is unsupported.",
            "The exponent is an empirical within-flight slope, not a calibrated propagation constant.",
            "There are only three independent flights per carrier in the current metadata; fold-level uncertainty is consequently coarse.",
            "Do not infer SNR, packet error rate, delay, loss, goodput or C2 performance from this model.",
            "Use only within the reported carrier, distance, waveform, sample-rate and campaign domain."]}
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "layer1_relative.json"
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    LOG.info("Wrote %s", target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_csv", required=True, help="P2 gurses_iq_descriptors.csv")
    parser.add_argument("--output_dir", required=True)
    args = parser.parse_args()
    fit(args.input_csv, args.output_dir)

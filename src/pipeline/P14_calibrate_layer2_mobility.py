"""Fit AFAR kinematic mobility profiles on train groups and score holdout groups.

The fitted scalar-speed Gauss-Markov parameters, turn-rate cap, and
train-median altitude profile are coupled to PX4 waypoint commands. Other
reported distributions (vertical rate and normalized spatial occupancy) are
diagnostic calibration targets; this single-UAV workflow does not claim
swarm-topology calibration.
"""
import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np

EARTH_RADIUS_M = 6_371_000.0
FEATURE_EDGES = {
    "speed_m_s": np.linspace(0.0, 20.0, 41),
    "turn_rate_deg_s": np.linspace(-90.0, 90.0, 37),
    "vertical_rate_m_s": np.linspace(-10.0, 10.0, 41),
    "altitude_relative_m": np.linspace(-20.0, 80.0, 41),
}
OCCUPANCY_BINS = 10
PROFILE_POINTS = 50


def read_trace(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    required = {"time_s", "latitude", "longitude", "altitude_relative_m"}
    if len(rows) < 3 or not required.issubset(rows[0]):
        raise ValueError(f"{path} must contain >=3 rows and {sorted(required)}")
    arrays = {name: np.asarray([float(row[name]) for row in rows], dtype=float)
              for name in required}
    if not all(np.isfinite(values).all() for values in arrays.values()):
        raise ValueError(f"Non-finite values in {path}")
    order = np.argsort(arrays["time_s"])
    arrays = {name: values[order] for name, values in arrays.items()}
    dt = np.diff(arrays["time_s"])
    if np.any(dt <= 0):
        raise ValueError(f"Timestamps must be strictly increasing in {path}")
    return arrays


def _trace_features(trace):
    lat, lon = trace["latitude"], trace["longitude"]
    lat0 = math.radians(float(lat[0]))
    east = np.radians(lon - lon[0]) * EARTH_RADIUS_M * math.cos(lat0)
    north = np.radians(lat - lat[0]) * EARTH_RADIUS_M
    dx, dy = np.diff(east), np.diff(north)
    dt = np.diff(trace["time_s"])
    speed = np.hypot(dx, dy) / dt
    valid = np.isfinite(speed) & (speed <= 20.0)
    heading = np.degrees(np.arctan2(dx, dy))
    turn = (heading[1:] - heading[:-1] + 180.0) % 360.0 - 180.0
    turn_rate = turn / ((dt[1:] + dt[:-1]) / 2.0)
    vertical_rate = np.diff(trace["altitude_relative_m"]) / dt
    occupancy, _, _ = np.histogram2d(
        np.clip((east - east.min()) / max(np.ptp(east), 1e-9), 0.0, 1.0),
        np.clip((north - north.min()) / max(np.ptp(north), 1e-9), 0.0, 1.0),
        bins=OCCUPANCY_BINS, range=[[0, 1], [0, 1]],
    )
    features = {
        "speed_m_s": speed[valid],
        "turn_rate_deg_s": turn_rate[np.isfinite(turn_rate) & (np.abs(turn_rate) <= 90.0)],
        "vertical_rate_m_s": vertical_rate[np.isfinite(vertical_rate) & (np.abs(vertical_rate) <= 10.0)],
        "altitude_relative_m": trace["altitude_relative_m"],
        "spatial_occupancy": occupancy.ravel() / max(float(occupancy.sum()), 1.0),
    }
    if len(features["speed_m_s"]) < 2:
        raise ValueError("Insufficient valid speed samples after quality bounds")
    features["trace_median_speed_m_s"] = float(np.median(features["speed_m_s"]))
    # Fit a one-second scalar-speed Gauss-Markov process per trace. The
    # irregular AFAR samples are first resampled in local ENU coordinates.
    sample_t = np.arange(trace["time_s"][0], trace["time_s"][-1], 1.0)
    if len(sample_t) >= 4:
        east_1s = np.interp(sample_t, trace["time_s"], east)
        north_1s = np.interp(sample_t, trace["time_s"], north)
        speed_1s = np.hypot(np.diff(east_1s), np.diff(north_1s))
        previous, current = speed_1s[:-1], speed_1s[1:]
        denom = float(np.dot(previous - previous.mean(), previous - previous.mean()))
        alpha = (float(np.dot(previous - previous.mean(), current - current.mean()) / denom)
                 if denom > 1e-9 else 0.0)
        alpha = float(np.clip(alpha, 0.0, 0.98))
        mean_speed = float(np.mean(speed_1s))
        innovation = current - (mean_speed + alpha * (previous - mean_speed))
        sigma = float(np.std(innovation, ddof=1)) if len(innovation) > 1 else 0.0
    else:
        alpha, mean_speed, sigma = 0.0, features["trace_median_speed_m_s"], 0.0
    features["speed_1s_m_s"] = speed_1s if len(sample_t) >= 4 else features["speed_m_s"]
    features["gauss_markov"] = {"alpha_1s": alpha, "mean_speed_m_s": mean_speed,
                                "innovation_sigma_m_s": sigma}
    return features


def _hist(values, edges):
    counts, _ = np.histogram(values, bins=edges)
    total = int(counts.sum())
    if total == 0:
        return np.zeros(len(edges) - 1, dtype=float)
    return counts.astype(float) / total


def _mean_trace_hist(traces, feature, edges):
    return np.mean([_hist(trace[feature], edges) for trace in traces], axis=0)


def _jsd_bits(p, q):
    p = np.asarray(p, dtype=float); q = np.asarray(q, dtype=float)
    p = p / p.sum(); q = q / q.sum(); m = 0.5 * (p + q)
    def kl(a, b):
        mask = a > 0
        return float(np.sum(a[mask] * np.log2(a[mask] / b[mask])))
    return 0.5 * kl(p, m) + 0.5 * kl(q, m)


def _fit_gauss_markov(train_features):
    """Fit AR(1) from lag-one train dynamics; use JSD as a distribution diagnostic."""
    edges = FEATURE_EDGES["speed_m_s"]
    observed = _mean_trace_hist(train_features, "speed_1s_m_s", edges)
    per_trace = []
    for feature in train_features:
        values = feature["speed_1s_m_s"]
        if len(values) < 3:
            continue
        previous, current = values[:-1], values[1:]
        centered = previous - previous.mean()
        variance = float(np.dot(centered, centered))
        alpha = float(np.dot(centered, current - current.mean()) / variance) if variance > 1e-9 else 0.0
        alpha = float(np.clip(alpha, 0.0, 0.98))
        mean = float(np.mean(values))
        residual = current - (mean + alpha * (previous - mean))
        per_trace.append((alpha, mean, float(np.std(residual, ddof=1)) if len(residual) > 1 else 0.0))
    if not per_trace:
        raise ValueError("Need at least one train trace with three 1-second speed samples")
    alpha_fit = float(np.median([x[0] for x in per_trace]))
    mean_speed = float(np.median([x[1] for x in per_trace]))
    sigma = float(np.median([x[2] for x in per_trace]))
    candidates = []
    candidate_alphas = sorted(set([float(x) for x in np.linspace(0.0, 0.95, 20)] + [alpha_fit]))
    for candidate_index, alpha in enumerate(candidate_alphas):
        generated = []
        for trace_index, feature in enumerate(train_features):
            count = len(feature["speed_1s_m_s"])
            rng = np.random.default_rng(41000 + candidate_index * 101 + trace_index)
            values = np.empty(count, dtype=float)
            values[0] = mean_speed
            for i in range(1, count):
                values[i] = np.clip(mean_speed + alpha * (values[i - 1] - mean_speed)
                                    + rng.normal(0.0, sigma), 0.0, 20.0)
            generated.append({"speed_m_s": values})
        generated_hist = _mean_trace_hist(generated, "speed_m_s", edges)
        candidates.append({"alpha_1s": float(alpha), "training_speed_jsd_bits": _jsd_bits(observed, generated_hist)})
    fit_jsd = next(item["training_speed_jsd_bits"] for item in candidates if item["alpha_1s"] == alpha_fit)
    fitted = {"alpha_1s": alpha_fit, "mean_speed_m_s": mean_speed,
              "innovation_sigma_m_s": sigma, "training_speed_jsd_bits": fit_jsd}
    return fitted, candidates


def calibrate(missions_dir, output_dir):
    missions_root = Path(missions_dir)
    manifest_path = missions_root / "missions_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing P4 manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    train_entries = [m for m in manifest.get("missions", []) if m.get("partition") == "train"]
    holdout_entries = [m for m in manifest.get("missions", []) if m.get("partition") == "holdout"]
    if len({m["source_group_id"] for m in train_entries}) < 2:
        raise ValueError("Need training traces from at least two source groups for a grouped mobility fit")
    if not train_entries or not holdout_entries:
        raise ValueError("Manifest must contain both train and holdout AFAR traces")

    def load(entries):
        loaded = []
        for entry in entries:
            features = _trace_features(read_trace(missions_root / entry["trace_file"]))
            loaded.append({"entry": entry, "features": features})
        return loaded

    train, holdout = load(train_entries), load(holdout_entries)
    model = {
        "schema_version": 1,
        "calibration_status": "afar_train_source_group_mobility_profile_with_heldout_distribution_evaluation",
        "model_type": "AFAR_empirical_kinematic_profile_single_UAV",
        "source": manifest.get("source"),
        "split_unit": manifest.get("split_unit"),
        "training_source_group_ids": sorted({str(x["entry"]["source_group_id"]) for x in train}),
        "holdout_source_group_ids": sorted({str(x["entry"]["source_group_id"]) for x in holdout}),
        "fit_trace_count": len(train), "holdout_trace_count": len(holdout),
        "mission_speed_m_s": float(np.median([x["features"]["trace_median_speed_m_s"] for x in train])),
        "altitude_profile_normalized_progress_m": [],
        "altitude_profile_semantics": "equal-trace median of AFAR train relative altitude resampled over normalized route progress; test-group altitude is not used in the fit",
        "gauss_markov_speed_model": {},
        "model_selection": {},
        "speed_parameter_semantics": "median of per-trace median ground speeds across training traces; used as a robust reference speed",
        "training_per_trace_median_speed_m_s": {
            x["entry"]["mission_id"]: x["features"]["trace_median_speed_m_s"] for x in train
        },
        "feature_distributions": {},
        "holdout_distribution_scores": {},
        "limitations": [
            "Fitted speed, turn-rate cap, and altitude profile are coupled to waypoint commands; they do not reproduce PX4 attitude, acceleration, wind, or full vehicle dynamics.",
            "Holdout scores compare AFAR kinematic feature distributions with the training profile; they do not score simulated-versus-real trajectories.",
            "Spatial occupancy is normalized within each route bounding box and is a shape descriptor, not a shared geographic map.",
            "No swarm topology, inter-UAV spacing, link lifetime, or physical aircraft dynamics are calibrated.",
            "Source-group holdout is the independent split unit; multiple routes within a source group are not independent source campaigns.",
        ],
    }
    gm_fit, gm_candidates = _fit_gauss_markov([x["features"] for x in train])
    turn_rate_p95 = float(np.median([
        np.quantile(np.abs(x["features"]["turn_rate_deg_s"]), 0.95)
        for x in train if len(x["features"]["turn_rate_deg_s"])
    ]))
    progress = np.linspace(0.0, 1.0, PROFILE_POINTS)
    altitude_profiles = []
    for item in train:
        trace = read_trace(missions_root / item["entry"]["trace_file"])
        phase = (trace["time_s"] - trace["time_s"][0]) / (trace["time_s"][-1] - trace["time_s"][0])
        altitude_profiles.append(np.interp(progress, phase, trace["altitude_relative_m"]))
    model["altitude_profile_normalized_progress_m"] = np.median(np.stack(altitude_profiles), axis=0).tolist()
    model["gauss_markov_speed_model"] = {
        "alpha_1s": gm_fit["alpha_1s"], "mean_speed_m_s": gm_fit["mean_speed_m_s"],
        "innovation_sigma_m_s": gm_fit["innovation_sigma_m_s"],
        "fitting": "equal-trace median of per-trace lag-one AR(1) least-squares alpha, speed mean, and innovation standard deviation; JSD is retained as a diagnostic, not used to select temporal parameters",
        "coupling": "sample seeded waypoint speeds with the 1-second AR(1) coefficient converted to estimated inter-waypoint travel-time steps, then pass speeds to PX4 mission items",
        "turn_rate_abs_p95_deg_s": turn_rate_p95,
        "turn_rate_coupling": "cap requested waypoint speed around geometric turns so implied turn rate does not exceed the train-group p95 reference, subject to PX4 minimum speed",
    }
    model["model_selection"] = {"objective": "estimate temporal AR(1) parameters from train traces; report generated-vs-observed speed distribution JSD (bits) diagnostically",
                                 "candidate_count": len(gm_candidates), "candidates": gm_candidates,
                                 "selected_alpha_1s": gm_fit["alpha_1s"],
                                 "fitted_train_speed_jsd_bits": gm_fit["training_speed_jsd_bits"]}
    for feature, edges in FEATURE_EDGES.items():
        p = _mean_trace_hist([x["features"] for x in train], feature, edges)
        q = _mean_trace_hist([x["features"] for x in holdout], feature, edges)
        model["feature_distributions"][feature] = {
            "bin_edges": edges.tolist(), "training_equal_trace_weighted_probability": p.tolist(),
        }
        model["holdout_distribution_scores"][feature] = {
            "metric": "Jensen-Shannon divergence (bits)", "jsd_bits": _jsd_bits(p, q),
            "train_trace_count": len(train), "holdout_trace_count": len(holdout),
            "interpretation": "distribution shift from training AFAR source groups to held-out AFAR source groups; lower is closer",
        }
    edges = np.linspace(0.0, 1.0, OCCUPANCY_BINS + 1)
    p = np.mean([x["features"]["spatial_occupancy"] for x in train], axis=0)
    q = np.mean([x["features"]["spatial_occupancy"] for x in holdout], axis=0)
    model["feature_distributions"]["spatial_occupancy_normalized_10x10"] = {
        "normalized_route_axis_bin_edges": edges.tolist(),
        "training_equal_trace_weighted_probability": p.tolist(),
    }
    model["holdout_distribution_scores"]["spatial_occupancy_normalized_10x10"] = {
        "metric": "Jensen-Shannon divergence (bits)", "jsd_bits": _jsd_bits(p, q),
        "train_trace_count": len(train), "holdout_trace_count": len(holdout),
        "interpretation": "shape-only spatial visit-distribution shift; each route is normalized to its bounding box",
    }

    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    target = out / "layer2_afar_mobility_calibration.json"
    target.write_text(json.dumps(model, indent=2), encoding="utf-8")
    return target, model


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--missions_dir", required=True)
    parser.add_argument("--output_dir", required=True)
    args = parser.parse_args()
    path, model = calibrate(args.missions_dir, args.output_dir)
    print(json.dumps({"output": str(path), "train_groups": model["training_source_group_ids"],
                      "holdout_groups": model["holdout_source_group_ids"],
                      "mission_speed_m_s": model["mission_speed_m_s"],
                      "holdout_jsd_bits": {k: v["jsd_bits"] for k, v in model["holdout_distribution_scores"].items()}}, indent=2))

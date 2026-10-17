"""Score per-quantity Layer-2 kinematic milestones on PX4 SITL holdout runs.

Distribution limits are derived from within-source-group AFAR train-trace
distances. This is a pilot acceptance rule for this archive, not a universal
flight standard or evidence of physical-aircraft equivalence.
"""
import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

EARTH_RADIUS_M = 6_371_000.0
FEATURES = ("horizontal_speed_m_s", "vertical_rate_abs_m_s",
            "turn_rate_abs_deg_s", "horizontal_acceleration_abs_m_s2")
FEATURE_UNITS = {"horizontal_speed_m_s": "m/s", "vertical_rate_abs_m_s": "m/s",
                 "turn_rate_abs_deg_s": "deg/s", "horizontal_acceleration_abs_m_s2": "m/s^2",
                 "heading_deg": "deg"}


def _read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def _enu(lat, lon, lat0, lon0):
    return (math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0)),
            math.radians(lat - lat0) * EARTH_RADIUS_M)


def _trajectory_features(points):
    """Resample ENU trajectory at 1 Hz, then derive course and kinematics."""
    if len(points) < 5:
        raise ValueError("at least five valid trajectory points are required")
    t = np.asarray([p[0] for p in points], dtype=float)
    lat0, lon0 = points[0][1], points[0][2]
    xy = np.asarray([_enu(p[1], p[2], lat0, lon0) for p in points])
    alt = np.asarray([p[3] for p in points], dtype=float)
    t1 = np.arange(t[0], t[-1], 1.0)
    if len(t1) < 5:
        raise ValueError("trajectory is shorter than five seconds")
    x, y, z = (np.interp(t1, t, arr) for arr in (xy[:, 0], xy[:, 1], alt))
    dt = np.diff(t1)
    dx, dy, dz = np.diff(x), np.diff(y), np.diff(z)
    speed = np.hypot(dx, dy) / dt
    vertical = dz / dt
    heading = np.unwrap(np.arctan2(dx, dy))
    turn_rate = np.diff(heading) / dt[1:]
    acceleration = np.diff(speed) / dt[1:]
    centered = speed[:-1] - np.mean(speed[:-1])
    denom = float(np.dot(centered, centered))
    lag1 = (float(np.dot(centered, speed[1:] - np.mean(speed[1:])) / denom)
            if denom > 1e-9 else 0.0)
    return {
        "horizontal_speed_m_s": speed,
        "vertical_rate_abs_m_s": np.abs(vertical),
        "turn_rate_abs_deg_s": np.abs(np.degrees(turn_rate)),
        "horizontal_acceleration_abs_m_s2": np.abs(acceleration),
        "heading_deg": (np.degrees(np.arctan2(dx, dy)) + 360.0) % 360.0,
        "speed_lag1_autocorrelation": lag1,
        "duration_s": float(t[-1] - t[0]),
    }


def _read_afar_raw(path):
    points = []
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.reader(stream):
            if len(row) < 4:
                continue
            try:
                lon, lat, altitude, timestamp = map(float, row[:4])
            except (ValueError, TypeError):
                continue
            if not all(map(math.isfinite, (lon, lat, altitude, timestamp))):
                continue
            points.append((timestamp, lat, lon, altitude))
    points.sort(key=lambda p: p[0])
    unique = []
    for point in points:
        if not unique or point[0] > unique[-1][0]:
            unique.append(point)
    if len(unique) < 5:
        raise ValueError(f"Insufficient valid AFAR samples in {path}")
    t0 = unique[0][0]
    return [(p[0] - t0, p[1], p[2], p[3]) for p in unique]


def _read_sitl_telemetry(path):
    rows = _read_csv(path)
    points = []
    for row in rows:
        try:
            if int(float(row.get("is_in_air", 0))) != 1:
                continue
            total = int(float(row.get("mission_total", 0)))
            current = int(float(row.get("mission_current", -1)))
            if total < 2 or not 0 <= current < total:
                continue
            t = float(row["elapsed_s"])
            point = (t, float(row["latitude"]), float(row["longitude"]),
                     float(row["altitude_relative_m"]))
            if all(map(math.isfinite, point)):
                points.append(point)
        except (KeyError, TypeError, ValueError):
            continue
    points.sort(key=lambda p: p[0])
    if len(points) < 5:
        raise ValueError("fewer than five in-air, mission-progress telemetry points")
    t0 = points[0][0]
    return [(p[0] - t0, p[1], p[2], p[3]) for p in points]


def _wasserstein_1d(a, b):
    """Exact empirical 1-D Wasserstein distance for equally weighted samples."""
    a, b = np.sort(np.asarray(a, dtype=float)), np.sort(np.asarray(b, dtype=float))
    if not len(a) or not len(b):
        raise ValueError("empty feature distribution")
    n = max(len(a), len(b))
    q = (np.arange(n) + 0.5) / n
    aq = np.quantile(a, q); bq = np.quantile(b, q)
    return float(np.mean(np.abs(aq - bq)))


def _circular_wasserstein_deg(a, b):
    """Circular W1, minimizing over the arbitrary 0/360-degree cut."""
    a, b = np.asarray(a) % 360.0, np.asarray(b) % 360.0
    if not len(a) or not len(b):
        raise ValueError("empty heading distribution")
    n = max(len(a), len(b))
    q = (np.arange(n) + 0.5) / n
    a, b = np.sort(a), np.sort(b)
    aq, bq = np.quantile(a, q), np.quantile(b, q)
    best = 180.0
    for shift in np.linspace(0.0, 360.0, 73)[:-1]:
        delta = (aq - ((bq + shift) % 360.0) + 180.0) % 360.0 - 180.0
        best = min(best, float(np.mean(np.abs(delta))))
    return best


def _feature_distance(feature, a, b):
    if feature == "heading_deg":
        return _circular_wasserstein_deg(a[feature], b[feature])
    return _wasserstein_1d(a[feature], b[feature])


def _train_thresholds(train_features):
    by_group = defaultdict(list)
    for group, features in train_features:
        by_group[str(group)].append(features)
    distances = defaultdict(list)
    lag_differences = []
    for traces in by_group.values():
        for i in range(len(traces)):
            for j in range(i + 1, len(traces)):
                for feature in (*FEATURES, "heading_deg"):
                    distances[feature].append(_feature_distance(feature, traces[i], traces[j]))
                lag_differences.append(abs(traces[i]["speed_lag1_autocorrelation"] -
                                           traces[j]["speed_lag1_autocorrelation"]))
    thresholds = {}
    for feature in (*FEATURES, "heading_deg"):
        values = distances[feature]
        if values:
            thresholds[feature] = {"value": float(np.quantile(values, 0.95)),
                                   "units": FEATURE_UNITS[feature],
                                   "n_train_within_group_pairs": len(values),
                                   "rule": "95th percentile of pairwise within-source-group train-trace Wasserstein distances"}
    if lag_differences:
        thresholds["speed_lag1_autocorrelation"] = {
            "value": float(np.quantile(lag_differences, 0.95)), "units": "absolute correlation difference",
            "n_train_within_group_pairs": len(lag_differences),
            "rule": "95th percentile of absolute pairwise within-source-group train-trace lag-1 differences"}
    return thresholds


def evaluate(campaign_dir, missions_dir, p13_report=None, output_file=None, required_partition="holdout"):
    campaign, missions = Path(campaign_dir), Path(missions_dir)
    manifest_path = missions / "missions_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing P4 manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mission_index = {m["mission_id"]: m for m in manifest.get("missions", [])}
    train_features = []
    reference_features = {}
    for item in manifest.get("missions", []):
        try:
            features = _trajectory_features(_read_afar_raw(item["source_file"]))
        except (OSError, ValueError, KeyError) as exc:
            if item.get("partition") == "train":
                raise ValueError(f"Cannot construct train thresholds from {item.get('mission_id')}: {exc}") from exc
            continue
        if item.get("partition") == "train":
            train_features.append((item["source_group_id"], features))
        elif item.get("partition") == "holdout":
            reference_features[item["mission_id"]] = features
    thresholds = _train_thresholds(train_features)
    if not thresholds:
        raise ValueError("No within-source-group train pairs; cannot derive empirical pilot thresholds")

    run_results, skipped = [], []
    for run_dir in sorted(campaign.rglob("run_*")):
        meta_path, telemetry_path = run_dir / "metadata.json", run_dir / "flight_telemetry.csv"
        if not meta_path.is_file() or not telemetry_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        mission_id = meta.get("mission_id")
        mission = mission_index.get(mission_id)
        if meta.get("status") != "complete" or not mission or mission.get("partition") != required_partition:
            continue
        try:
            observed = _trajectory_features(_read_sitl_telemetry(telemetry_path))
            reference = reference_features[mission_id]
        except (OSError, ValueError, KeyError) as exc:
            skipped.append({"run_id": meta.get("run_id", run_dir.name), "reason": str(exc)})
            continue
        scores = {}
        for feature, threshold in thresholds.items():
            if feature == "speed_lag1_autocorrelation":
                distance = abs(observed[feature] - reference[feature])
                label = "absolute_difference"
            else:
                distance = _feature_distance(feature, observed, reference)
                label = "wasserstein_distance"
            scores[feature] = {"distance": distance, "distance_type": label,
                               "threshold": threshold["value"], "units": threshold["units"],
                               "status": "pass" if distance <= threshold["value"] else "fail"}
        # AFAR duration is descriptive only: P7 intentionally does not replay its schedule.
        run_results.append({"run_id": meta.get("run_id", run_dir.name), "mission_id": mission_id,
                            "source_group_id": mission["source_group_id"],
                            "scores": scores,
                            "descriptive": {"sitl_duration_s": observed["duration_s"],
                                            "afar_reference_duration_s": reference["duration_s"],
                                            "duration_comparison": "not a pass/fail target; AFAR timestamps are not PX4 timing commands",
                                            "observed_speed_lag1_autocorrelation": observed["speed_lag1_autocorrelation"],
                                            "reference_speed_lag1_autocorrelation": reference["speed_lag1_autocorrelation"]}})

    p13 = {}
    if p13_report:
        p13_path = Path(p13_report)
        p13 = json.loads(p13_path.read_text(encoding="utf-8"))
        if p13.get("required_partition") != required_partition:
            raise ValueError("P13 report partition does not match requested milestone partition")
    route_results = p13.get("results", [])
    route_milestone = {
        "status": "scored_threshold_pending" if route_results else "not_evaluated",
        "basis": "P13 run-level route tracking report; no universal numeric cutoff imposed by this script",
        "n_runs": len(route_results),
        "results": [{"run_id": r.get("run_id"), "mission_id": r.get("mission_id"),
                     "cross_track_rmse_m": r.get("horizontal_cross_track_error_m", {}).get("rmse"),
                     "cross_track_p95_m": r.get("horizontal_cross_track_error_m", {}).get("p95"),
                     "altitude_rmse_m": r.get("altitude_error_m", {}).get("rmse"),
                     "altitude_p95_abs_m": r.get("altitude_error_m", {}).get("p95_absolute")}
                    for r in route_results],
    }
    def milestone(feature, metric):
        statuses = [run["scores"][feature]["status"] for run in run_results if feature in run["scores"]]
        return {"status": ("not_evaluated" if not statuses else
                           "pass" if all(value == "pass" for value in statuses) else "fail"),
                "metric": metric, "n_runs": len(statuses),
                "run_statuses": [{"run_id": run["run_id"], "status": run["scores"][feature]["status"],
                                  "distance": run["scores"][feature]["distance"],
                                  "threshold": run["scores"][feature]["threshold"]}
                                 for run in run_results if feature in run["scores"]]}

    lag_statuses = [run["scores"]["speed_lag1_autocorrelation"]["status"]
                    for run in run_results if "speed_lag1_autocorrelation" in run["scores"]]
    report = {
        "evaluation_type": "Layer-2 per-quantity kinematic milestone assessment",
        "required_partition": required_partition,
        "train_source_group_ids": sorted({str(g) for g, _ in train_features}),
        "evaluation_source_group_ids": sorted({r["source_group_id"] for r in run_results}),
        "threshold_method": "Empirical pilot limits from the 95th percentile of pairwise within-source-group AFAR train-trace distances; do not interpret as universal UAV acceptance standards.",
        "milestones": {
            "route_tracking": route_milestone,
            "horizontal_speed_distribution": milestone("horizontal_speed_m_s", "1-Hz Wasserstein distance to paired holdout AFAR trace"),
            "vertical_rate_distribution": milestone("vertical_rate_abs_m_s", "1-Hz Wasserstein distance of absolute vertical-rate distribution"),
            "turn_rate_distribution": milestone("turn_rate_abs_deg_s", "1-Hz Wasserstein distance of absolute course turn-rate distribution"),
            "heading_distribution": milestone("heading_deg", "circular Wasserstein distance of course-over-ground headings"),
            "acceleration_distribution": milestone("horizontal_acceleration_abs_m_s2", "1-Hz Wasserstein distance of absolute horizontal acceleration"),
            "temporal_speed_correlation": {"status": ("not_evaluated" if not lag_statuses else
                                                         "pass" if all(s == "pass" for s in lag_statuses) else "fail"),
                                           "metric": "absolute difference in 1-Hz lag-1 speed autocorrelation",
                                           "n_runs": len(lag_statuses),
                                           "run_statuses": [{"run_id": r["run_id"],
                                                             **r["scores"]["speed_lag1_autocorrelation"]}
                                                            for r in run_results if "speed_lag1_autocorrelation" in r["scores"]]},
            "mission_duration": {"status": "descriptive_only", "reason": "AFAR timestamps were not used as PX4 schedule targets"},
            "attitude_angular_rate_and_3d_acceleration": {"status": "not_confirmable", "reason": "AFAR GPS reference has no synchronized attitude, body-rate, or three-axis acceleration ground truth"},
        },
        "thresholds": thresholds,
        "run_results": run_results,
        "skipped_runs": skipped,
        "interpretation": "A pass means the selected SITL feature distance is within observed same-source-group train variability. It is a distributional pilot gate, not paired trajectory dynamics validation or proof of physical-UAV equivalence.",
        "limitations": [
            "Thresholds use four train source groups and one held-out source group in this archive; broader external confirmation needs more independent campaigns.",
            "AFAR and SITL trajectories are not synchronized twin/field pairs; temporal checks compare feature statistics only.",
            "GPS-derived course and acceleration are not body heading, attitude, angular rate, or full 3-D aircraft dynamics.",
            "Mission duration is descriptive because source AFAR timestamps are not PX4 route timing commands.",
            "The route-tracking milestone reports P13 errors but requires a separately pre-registered engineering tolerance to label a numeric pass.",
        ],
    }
    output = Path(output_file) if output_file else campaign / "layer2_kinematic_milestones.json"
    if not output.is_absolute():
        output = campaign / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return output, report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign_dir", required=True)
    parser.add_argument("--missions_dir", default="data/missions")
    parser.add_argument("--p13_report", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--partition", choices=["train", "holdout", "all"], default="holdout")
    args = parser.parse_args()
    output, report = evaluate(args.campaign_dir, args.missions_dir, args.p13_report,
                              args.output, args.partition)
    print(json.dumps({"output": str(output), "partition": report["required_partition"],
                      "evaluated_runs": len(report["run_results"]),
                      "skipped_runs": len(report["skipped_runs"]),
                      "threshold_metrics": sorted(report["thresholds"])}, indent=2))

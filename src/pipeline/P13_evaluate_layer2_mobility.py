"""Evaluate PX4 SITL path tracking against held-out AFAR route geometry.

This is a spatial, path-parametric SITL tracking check. AFAR timestamps are not
treated as PX4 schedule targets, and the result is not physical flight-model
validation because both reference and replay are software/simulation records.
"""
import argparse
import csv
import json
import math
from pathlib import Path

import numpy as np

EARTH_RADIUS_M = 6_371_000.0
ALTITUDE_OFFSET_M = 15.0
REQUIRED_TELEMETRY = {
    "latitude", "longitude", "altitude_relative_m", "mission_current",
    "mission_total", "is_in_air",
}


def _read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def _enu(lat, lon, origin_lat, origin_lon):
    """Local tangent approximation; suitable for these short AFAR routes."""
    north = math.radians(lat - origin_lat) * EARTH_RADIUS_M
    east = (math.radians(lon - origin_lon) * EARTH_RADIUS_M
            * math.cos(math.radians(origin_lat)))
    return east, north


def _route_tracking_errors(route_rows, telemetry_rows, altitude_offset_m):
    origin_lat = float(route_rows[0]["latitude"])
    origin_lon = float(route_rows[0]["longitude"])
    rx, ry, rz = [], [], []
    for row in route_rows:
        x, y = _enu(float(row["latitude"]), float(row["longitude"]), origin_lat, origin_lon)
        rx.append(x); ry.append(y)
        rz.append(float(row["altitude_relative_m"]) + altitude_offset_m)
    rx, ry, rz = np.asarray(rx), np.asarray(ry), np.asarray(rz)
    starts = np.column_stack([rx[:-1], ry[:-1]])
    vectors = np.column_stack([np.diff(rx), np.diff(ry)])
    lengths_sq = np.einsum("ij,ij->i", vectors, vectors)
    valid = lengths_sq > 1e-9
    starts, vectors, lengths_sq = starts[valid], vectors[valid], lengths_sq[valid]
    alt_start, alt_delta = rz[:-1][valid], np.diff(rz)[valid]
    if not len(starts):
        raise ValueError("Reference route has no non-zero horizontal segments")

    horizontal_errors, altitude_errors = [], []
    scored = 0
    for row in telemetry_rows:
        try:
            current = int(float(row["mission_current"]))
            total = int(float(row["mission_total"]))
            if int(float(row["is_in_air"])) != 1 or total < 2 or not 0 <= current < total:
                continue
            lat, lon = float(row["latitude"]), float(row["longitude"])
            altitude = float(row["altitude_relative_m"])
            if not all(map(math.isfinite, (lat, lon, altitude))):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        x, y = _enu(lat, lon, origin_lat, origin_lon)
        delta = np.column_stack([np.full(len(starts), x), np.full(len(starts), y)]) - starts
        fraction = np.clip(np.einsum("ij,ij->i", delta, vectors) / lengths_sq, 0.0, 1.0)
        closest = starts + fraction[:, None] * vectors
        distance_sq = np.einsum("ij,ij->i", np.column_stack([np.full(len(starts), x), np.full(len(starts), y)]) - closest,
                                np.column_stack([np.full(len(starts), x), np.full(len(starts), y)]) - closest)
        segment = int(np.argmin(distance_sq))
        horizontal_errors.append(math.sqrt(float(distance_sq[segment])))
        expected_altitude = float(alt_start[segment] + fraction[segment] * alt_delta[segment])
        altitude_errors.append(altitude - expected_altitude)
        scored += 1
    if scored < 5:
        raise ValueError(f"Only {scored} telemetry samples satisfy in-air and mission-progress filters")
    h, z = np.asarray(horizontal_errors), np.asarray(altitude_errors)
    return {
        "scored_telemetry_samples": int(scored),
        "horizontal_cross_track_error_m": {
            "mean": float(np.mean(h)), "median": float(np.median(h)),
            "rmse": float(np.sqrt(np.mean(h ** 2))), "p95": float(np.quantile(h, 0.95)),
        },
        "altitude_error_m": {
            "bias": float(np.mean(z)), "mae": float(np.mean(np.abs(z))),
            "rmse": float(np.sqrt(np.mean(z ** 2))), "p95_absolute": float(np.quantile(np.abs(z), 0.95)),
        },
    }


def evaluate(campaign_dir, missions_dir, output_file, required_partition="holdout",
             altitude_offset_m=ALTITUDE_OFFSET_M):
    campaign, missions = Path(campaign_dir), Path(missions_dir)
    manifest_path = missions / "missions_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing AFAR split manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mission_index = {m["mission_id"]: m for m in manifest.get("missions", [])}
    if required_partition not in {"train", "holdout", "all"}:
        raise ValueError("required_partition must be train, holdout, or all")

    results, skipped = [], []
    for run_dir in sorted(campaign.rglob("run_*")):
        meta_path = run_dir / "metadata.json"
        telemetry_path = run_dir / "flight_telemetry.csv"
        if not meta_path.is_file() or not telemetry_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("status") != "complete":
            continue
        mission_id = meta.get("mission_id")
        mission = mission_index.get(mission_id)
        if mission is None:
            skipped.append({"run_id": meta.get("run_id", run_dir.name), "reason": "mission absent from AFAR manifest"})
            continue
        partition = mission.get("partition")
        if required_partition != "all" and partition != required_partition:
            continue
        missing = REQUIRED_TELEMETRY - set(_read_csv(telemetry_path)[0].keys()) if _read_csv(telemetry_path) else REQUIRED_TELEMETRY
        if missing:
            skipped.append({"run_id": meta.get("run_id", run_dir.name), "reason": f"telemetry missing fields: {sorted(missing)}"})
            continue
        try:
            scores = _route_tracking_errors(_read_csv(missions / mission["trace_file"]),
                                            _read_csv(telemetry_path), altitude_offset_m)
        except (OSError, ValueError, KeyError) as exc:
            skipped.append({"run_id": meta.get("run_id", run_dir.name), "reason": str(exc)})
            continue
        results.append({
            "run_id": meta.get("run_id", run_dir.name), "mission_id": mission_id,
            "source_group_id": mission["source_group_id"], "partition": partition,
            "channel_id": meta.get("channel_id"), "attack_id": meta.get("attack_id"),
            "altitude_offset_m": altitude_offset_m, **scores,
        })

    evaluated_groups = sorted({item["source_group_id"] for item in results})
    metric_names = ("horizontal_cross_track_error_m", "altitude_error_m")
    group_summary = {}
    for group in evaluated_groups:
        rows = [r for r in results if r["source_group_id"] == group]
        group_summary[group] = {"n_runs": len(rows), "mission_ids": sorted({r["mission_id"] for r in rows})}
        for metric in metric_names:
            for statistic in ("rmse",):
                vals = [r[metric][statistic] for r in rows]
                group_summary[group][f"{metric}_{statistic}_median_across_runs"] = float(np.median(vals))

    output = Path(output_file)
    if not output.is_absolute():
        output = campaign / output
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "evaluation_type": "PX4_SITL_AFAR_route_geometry_tracking",
        "split_unit": manifest.get("split_unit"),
        "required_partition": required_partition,
        "evaluated_source_group_ids": evaluated_groups,
        "altitude_mapping": "reference AFAR altitude_relative_m + configured offset; SITL telemetry relative_altitude_m",
        "spatial_method": "local ENU projection and shortest horizontal distance to reference polyline; altitude interpolated on closest segment",
        "temporal_method": "path-parametric; AFAR source timestamps are not used as PX4 timing targets",
        "results": results,
        "source_group_summary": group_summary,
        "skipped_runs": skipped,
        "limitations": [
            "Only complete runs from the requested AFAR source-group partition are scored; report the holdout partition for an untouched mobility evaluation.",
            "This measures PX4 SITL route tracking against a recorded AFAR path, not real-aircraft flight-dynamics fidelity.",
            "The 15 m altitude offset is a simulator mission mapping, not a measured AFAR AGL correction.",
            "Horizontal distance is path-parametric and does not validate speed/time dynamics; AFAR timestamps are not assumed to be desired SITL timing.",
            "Runs within one source_group_id are repeated/software observations, not independent source groups.",
        ],
    }
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return output, report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign_dir", required=True, help="Root containing P8 run directories (recursive scan)")
    parser.add_argument("--missions_dir", default="data/missions")
    parser.add_argument("--output", default="layer2_mobility_holdout.json")
    parser.add_argument("--partition", choices=["train", "holdout", "all"], default="holdout")
    parser.add_argument("--altitude_offset_m", type=float, default=ALTITUDE_OFFSET_M)
    args = parser.parse_args()
    output, report = evaluate(args.campaign_dir, args.missions_dir, args.output,
                              args.partition, args.altitude_offset_m)
    print(json.dumps({"output": str(output), "partition": args.partition,
                      "evaluated_runs": len(report["results"]),
                      "source_groups": report["evaluated_source_group_ids"],
                      "skipped_runs": len(report["skipped_runs"])}, indent=2))

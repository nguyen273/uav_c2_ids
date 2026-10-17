"""Validate and consolidate processed AERPAW Dataset-32 CSV measurements."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


FLIGHTS = ("f10", "f11", "f12", "f13")
REQUIRED = {
    "measurement_id", "campaign_id", "timestamp_utc", "time_s",
    "rx_latitude", "rx_longitude", "rx_altitude_agl_m",
    "tx_latitude", "tx_longitude", "tx_altitude_agl_m", "heading_deg",
    "uav2ground_dist", "path_loss_db",
}
NUMERIC = (
    "time_s", "rx_latitude", "rx_longitude", "rx_altitude_agl_m",
    "tx_latitude", "tx_longitude", "tx_altitude_agl_m", "heading_deg",
    "uav2ground_dist", "path_loss_db",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare(dataset_dir: Path, output_dir: Path) -> Path:
    dataset_dir = dataset_dir.resolve()
    readme = dataset_dir / "README.md"
    if not readme.is_file():
        raise FileNotFoundError(f"Dataset-32 README not found: {readme}")

    frames, input_manifest = [], []
    for flight in FLIGHTS:
        csv_path = dataset_dir / f"{flight}.csv"
        meta_path = dataset_dir / f"{flight}.sigmf-meta"
        if not csv_path.is_file() or not meta_path.is_file():
            raise FileNotFoundError(f"Dataset-32 requires {flight}.csv and {flight}.sigmf-meta")
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        captures = metadata.get("captures", [])
        freqs = {float(c["core:frequency"]) for c in captures if "core:frequency" in c}
        if len(freqs) != 1 or not np.isclose(next(iter(freqs)), 3.4e9, rtol=0, atol=1.0):
            raise ValueError(f"{flight}: expected one 3.4 GHz carrier in capture metadata, got {sorted(freqs)}")
        frame = pd.read_csv(csv_path)
        missing = sorted(REQUIRED - set(frame.columns))
        if missing:
            raise ValueError(f"{csv_path}: missing required columns {missing}")
        frame["flight_id"] = flight
        frame["frequency_hz"] = 3_400_000_000.0
        for col in NUMERIC:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame["timestamp_utc"] = pd.to_datetime(frame["timestamp_utc"], utc=True, errors="coerce")
        if frame[list(NUMERIC)].isna().any().any() or frame["timestamp_utc"].isna().any():
            raise ValueError(f"{csv_path}: non-numeric or missing measurement values")
        if not np.isfinite(frame[list(NUMERIC)].to_numpy(dtype=float)).all():
            raise ValueError(f"{csv_path}: non-finite measurement values")
        if (frame["uav2ground_dist"] <= 0).any():
            raise ValueError(f"{csv_path}: distance must be positive")
        if frame["measurement_id"].duplicated().any():
            raise ValueError(f"{csv_path}: duplicate measurement_id values")
        if frame["campaign_id"].nunique() != 1:
            raise ValueError(f"{csv_path}: expected exactly one campaign_id")
        if not frame["rx_altitude_agl_m"].between(0, 500, inclusive="both").all():
            raise ValueError(f"{csv_path}: implausible receiver AGL values")
        frames.append(frame)
        input_manifest.append({
            "flight_id": flight, "campaign_id": str(frame["campaign_id"].iloc[0]),
            "rows": int(len(frame)), "capture_count": int(len(captures)),
            "frequency_hz": 3_400_000_000,
            "csv_sha256": sha256(csv_path), "sigmf_meta_sha256": sha256(meta_path),
        })

    combined = pd.concat(frames, ignore_index=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_out = output_dir / "dataset32_pathloss.csv"
    combined.to_csv(csv_out, index=False, date_format="%Y-%m-%dT%H:%M:%S.%fZ")
    manifest = {
        "dataset_id": "AERPAW-Dataset-32",
        "citation": "Gurses and Sichitiu (2026), Dryad DOI 10.5061/dryad.2ngf1vj4r",
        "source_readme": str(readme), "source_readme_sha256": sha256(readme),
        "target_frequency_hz": 3_400_000_000,
        "input_flights": input_manifest,
        "rows_total": int(len(combined)), "output_csv": str(csv_out.resolve()),
        "measurement_semantics": "source-provided processed path_loss_db and 3D uav2ground_dist; not independently verified metrology-traceable absolute calibration",
    }
    (output_dir / "dataset32_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return csv_out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset_dir", required=True)
    parser.add_argument("--output_dir", required=True)
    args = parser.parse_args()
    result = prepare(Path(args.dataset_dir), Path(args.output_dir))
    print(result)

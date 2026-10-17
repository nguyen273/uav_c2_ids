"""Consolidate source Gürses post-processor outputs for Layer-1 fitting.

This script does not process IQ. It collects official per-flight processed.csv
files, validates their path-loss fields, and adds provenance identifiers.
"""
import argparse
from pathlib import Path

import pandas as pd

REQUIRED = {"dist", "avg_pl", "center_freq", "pl_valid"}
MEASURED_REFERENCE_SOURCES = {"power_refs_csv", "uhd_calibration"}


def _read_campaign_calibration(processed_csv):
    summary_path = processed_csv.parent / "summary.json"
    if not summary_path.is_file():
        raise ValueError(f"Missing processor summary/calibration provenance: {summary_path}")
    import json
    import math

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    link = summary.get("path_loss_calibration") or {}
    # post_process.py writes tx_source/rx_source (the *_ref_source spellings
    # are accepted for older local outputs).
    tx_source = str(link.get("tx_source", link.get("tx_ref_source", ""))).strip().lower()
    rx_source = str(link.get("rx_source", link.get("rx_ref_source", ""))).strip().lower()
    if tx_source not in MEASURED_REFERENCE_SOURCES or rx_source not in MEASURED_REFERENCE_SOURCES:
        raise ValueError(f"{summary_path}: non-measured/fallback link-budget reference source (TX={tx_source!r}, RX={rx_source!r})")
    def as_bool(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in {"true", "1", "yes", "y"}
    if as_bool(link.get("tx_extrapolated", True)) or as_bool(link.get("rx_extrapolated", True)):
        raise ValueError(f"{summary_path}: TX/RX reference extrapolation is missing or flagged")
    for field in ("tx_ref_dbm", "rx_ref_dbm"):
        try:
            value = float(link[field])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{summary_path}: missing finite {field}")
        if not math.isfinite(value):
            raise ValueError(f"{summary_path}: {field} is not finite")
    warnings = summary.get("link_budget_warnings") or []
    if warnings:
        raise ValueError(f"{summary_path}: link-budget warnings must be resolved before calibration: {warnings}")
    valid_fraction = float(summary.get("path_loss_valid_fraction", 0.0) or 0.0)
    if not math.isfinite(valid_fraction) or valid_fraction <= 0:
        raise ValueError(f"{summary_path}: no valid path-loss observations")
    antenna = summary.get("antenna_calibration") or {}
    confidence = str(antenna.get("confidence", "unavailable")).strip().lower()
    use_antcal = confidence in {"medium", "high"}
    return {
        "tx_ref_source": tx_source, "rx_ref_source": rx_source,
        "tx_ref_dbm": float(link["tx_ref_dbm"]), "rx_ref_dbm": float(link["rx_ref_dbm"]),
        "tx_extrapolated": False, "rx_extrapolated": False,
        "path_loss_valid_fraction_campaign": valid_fraction,
        "antenna_calibration_confidence": confidence,
        "fit_path_loss_column": "avg_pl_antcal" if use_antcal else "avg_pl",
        "calibration_summary_path": str(summary_path.resolve()),
    }


def collect(processed_root, output_csv):
    root = Path(processed_root)
    if not root.is_dir():
        raise FileNotFoundError(f"Gürses post-processor result directory not found: {root}")
    files = sorted(p for p in root.rglob("processed.csv") if p.is_file())
    if not files:
        raise FileNotFoundError(f"No per-flight processed.csv found under {root}")
    frames = []
    selected_columns = set()
    for file in files:
        df = pd.read_csv(file)
        missing = sorted(REQUIRED - set(df.columns))
        if missing:
            raise ValueError(f"{file} lacks source post-processor columns: {missing}")
        campaign_cal = _read_campaign_calibration(file)
        selected = campaign_cal["fit_path_loss_column"]
        if selected not in df.columns:
            raise ValueError(f"{file}: summary selects {selected}, but that measurement column is absent")
        selected_columns.add(selected)
        df["fit_path_loss_db"] = pd.to_numeric(df[selected], errors="coerce")
        # Antenna-corrected path loss is only selected when the processor
        # reports medium/high confidence; otherwise the fitted quantity remains
        # the measured link path loss including the antenna pattern.
        if selected == "avg_pl_antcal" and df.fit_path_loss_db.notna().sum() == 0:
            raise ValueError(f"{file}: antenna calibration confidence is high/medium but avg_pl_antcal is empty")
        df["flight_id"] = file.parent.parent.name
        for key, value in campaign_cal.items():
            if key != "calibration_summary_path":
                df[f"cal_{key}"] = value
        df["source_processed_csv"] = str(file.resolve())
        frames.append(df)
    result = pd.concat(frames, ignore_index=True, sort=False)
    if len(selected_columns) != 1:
        raise ValueError("Campaigns mix antenna-corrected and uncorrected path loss; use one consistent measurement definition across all flights")
    result["fit_measurement_column"] = next(iter(selected_columns))
    out = Path(output_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False)
    print(f"Collected {len(files)} official processed.csv files, {len(result)} rows, {result.flight_id.nunique()} flight groups -> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--processed_root", required=True, help="Root containing per-flight outputs from Gürses post_process.py")
    ap.add_argument("--output_csv", required=True)
    args = ap.parse_args()
    collect(args.processed_root, args.output_csv)

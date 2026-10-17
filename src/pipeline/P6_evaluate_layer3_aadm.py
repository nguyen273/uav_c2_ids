"""Flight-grouped evaluation of Layer 3 against synchronized AADM testbed data.

The input is a normalized AADM USRP CSV with one row per timestamp/base
station. This evaluates AADM's measured link and optional network KPIs; it
does not transfer Gürses coefficients across different radios/campaigns.
"""
import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("P6_AADM_Layer3_Eval")


def metrics(actual, predicted):
    e = np.asarray(predicted) - np.asarray(actual)
    return {"n": int(len(e)), "rmse": float(np.sqrt(np.mean(e ** 2))),
            "mae": float(np.mean(np.abs(e))), "bias_pred_minus_measured": float(np.mean(e))}


def run(input_csv, output_dir, seed=42, test_size=0.25):
    src = Path(input_csv)
    df = pd.read_csv(src)
    # Accept the column names emitted by uav-cas/Scripts/0-step1_process_aadm.py.
    aliases = {"snr_dB": "snr_db", "power_dBm": "power_dbm", "goodput_Mbps": "goodput_mbps"}
    df = df.rename(columns={k: v for k, v in aliases.items() if k in df.columns and v not in df.columns})
    required = {"flight_id", "bs_id", "distance_to_bs_m", "snr_db"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"AADM normalized input is missing required paired fields: {missing}")
    for col in ("distance_to_bs_m", "snr_db", "goodput_mbps", "power_dbm", "rtt_ms", "packet_loss_pct"):
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["flight_id"] = df.flight_id.astype(str)
    df["bs_id"] = df.bs_id.astype(str)
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["flight_id", "bs_id", "distance_to_bs_m", "snr_db"])
    df = df[(df.distance_to_bs_m > 1) & df.snr_db.between(-100, 100)].copy()
    if df.flight_id.nunique() < 4:
        raise ValueError("Need >=4 AADM flights to retain independent train and test flights")
    train_flights, test_flights = next(GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed).split(df, groups=df.flight_id))
    train, test = df.iloc[train_flights], df.iloc[test_flights]
    result = {"evaluation_type": "AADM_testbed_flight_grouped_layer3_validation",
              "source_csv": str(src), "train_flights": sorted(train.flight_id.unique().tolist()),
              "test_flights": sorted(test.flight_id.unique().tolist()),
              "row_count": int(len(df)), "base_stations": {},
              "interpretation": "Independent AADM empirical link validation. Gürses path-loss coefficients are not applied because campaign/radio/frequency/link-budget equivalence is not established."}
    for bs in sorted(set(train.bs_id) & set(test.bs_id)):
        tr, te = train[train.bs_id == bs], test[test.bs_id == bs]
        if len(tr) < 10 or len(te) < 5 or tr.distance_to_bs_m.nunique() < 4:
            continue
        # Empirical AADM SNR-versus-geometry baseline; fit uses training flights only.
        xtr = np.log10(tr.distance_to_bs_m.to_numpy())
        slope, intercept = np.polyfit(xtr, tr.snr_db.to_numpy(), 1)
        snr_pred = intercept + slope * np.log10(te.distance_to_bs_m.to_numpy())
        entry = {"train_samples": int(len(tr)), "test_samples": int(len(te)),
                 "snr_vs_log_distance_slope_db_per_decade": float(slope),
                 "heldout_snr_metrics_db": metrics(te.snr_db.to_numpy(), snr_pred)}
        # Optional empirical throughput mapping. AADM download counters are not
        # throughput until converted to a rate with their source semantics.
        if "goodput_mbps" in df and tr.goodput_mbps.notna().sum() >= 10 and te.goodput_mbps.notna().sum() >= 5:
            a, b = np.polyfit(tr.loc[tr.goodput_mbps.notna(), "snr_db"],
                              tr.loc[tr.goodput_mbps.notna(), "goodput_mbps"], 1)
            subset = te.dropna(subset=["goodput_mbps"])
            pred = np.maximum(0.0, a * subset.snr_db.to_numpy() + b)
            entry["heldout_goodput_metrics_mbps"] = metrics(subset.goodput_mbps.to_numpy(), pred)
            entry["goodput_mapping"] = {"type": "train_flight_linear_snr_baseline", "slope_mbps_per_db": float(a), "intercept_mbps": float(b)}
        result["base_stations"][bs] = entry
    if not result["base_stations"]:
        raise ValueError("No base station had enough paired, flight-independent AADM samples")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "layer3_aadm_holdout_metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    LOG.info("Wrote %s", out / "layer3_aadm_holdout_metrics.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_csv", required=True, help="Normalized synchronized AADM testbed telemetry/link CSV")
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--test_size", type=float, default=0.25)
    a = ap.parse_args()
    run(a.input_csv, a.output_dir, a.seed, a.test_size)

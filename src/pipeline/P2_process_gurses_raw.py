"""Extract a relative raw-IQ level descriptor from AERPAW SigMF captures.

IQ RMS is not channel gain, RSRP, or dBm without waveform reconstruction and
traceable RF calibration. This output is exploratory only.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("P2_Gurses_Raw")


def iq_power_dbfs_like(iq_path, meta):
    glob = meta.get("global", {})
    if glob.get("core:datatype") != "f32_le":
        raise ValueError(f"Unsupported IQ datatype {glob.get('core:datatype')!r}; expected interleaved f32_le")
    raw = np.fromfile(iq_path, dtype="<f4")
    if raw.size < 2 or raw.size % 2:
        raise ValueError("IQ capture must contain at least one I/Q pair and an even number of float components")
    iq = raw[0::2].astype(np.float64) + 1j * raw[1::2].astype(np.float64)
    if not np.isfinite(iq.real).all() or not np.isfinite(iq.imag).all():
        raise ValueError("IQ contains NaN/Inf samples")
    mean_power = float(np.mean(np.abs(iq) ** 2))
    if not np.isfinite(mean_power) or mean_power <= 0:
        raise ValueError("IQ capture has no finite positive mean power")
    return float(10.0 * np.log10(mean_power))


def process_gurses_raw(raw_dir, output_dir):
    root = Path(raw_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"AERPAW SigMF directory not found: {root}")
    rows, dropped = [], []
    folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("._"))
    for folder in folders:
        for meta_path in sorted(folder.glob("*.sigmf-meta")):
            data_path = meta_path.with_suffix(".sigmf-data")
            try:
                if not data_path.exists():
                    raise FileNotFoundError(data_path.name)
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                captures = meta.get("captures", [])
                if len(captures) != 1:
                    raise ValueError(f"Expected exactly one capture record, got {len(captures)}")
                cap = captures[0]
                rx, tx = cap["core:rx_location"], cap["core:tx_location"]
                rel_power = iq_power_dbfs_like(data_path, meta)
                freq = float(cap["core:frequency"]) / 1e9
                dist = float(cap["core:dist"])
                rows.append({"flight_id": folder.name, "capture_id": meta_path.stem,
                             "timestamp_s": float(cap["core:timestamp"]), "distance_m": dist,
                             "relative_altitude_m": abs(float(rx["altitude"]) - float(tx["altitude"])),
                             "iq_power_dbfs": rel_power, "frequency_ghz": freq,
                             "waveform": meta["global"]["core:waveform"],
                             "sample_rate_hz": float(meta["global"]["core:sample_rate"]),
                             "rx_gain_ref_db": meta["global"].get("core:rx_gain_ref"),
                             "tx_gain_ref_db": meta["global"].get("core:tx_gain_ref"),
                             "provenance": "AERPAW SigMF; raw IQ RMS in arbitrary dBFS-like units; not RF-calibrated"})
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                dropped.append({"file": str(meta_path), "reason": str(exc)})
    if not rows:
        raise RuntimeError(f"No usable SigMF captures found; rejected {len(dropped)} metadata files")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out / "gurses_iq_descriptors.csv", index=False)
    (out / "gurses_rejected.json").write_text(json.dumps(dropped, indent=2), encoding="utf-8")
    logger.warning("Saved %d raw-IQ level descriptors; rejected %d. These are not channel gain, dBm or RSRP.", len(rows), len(dropped))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw_dir", required=True)
    ap.add_argument("--output_dir", required=True)
    args = ap.parse_args()
    try:
        process_gurses_raw(args.raw_dir, args.output_dir)
    except Exception as exc:
        logger.error("FAIL-CLOSED: %s", exc)
        sys.exit(1)

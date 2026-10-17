"""Extract the supplied 3.6 GHz A2G path-loss and CIR reference datasets.

Uses the authors' Appendix/PL_data MATLAB arrays as path-loss observations.
The raw XLSX CIR snapshots are summarized separately and never relabeled as
absolute path loss or calibrated received power.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def _mat_pair(path: Path):
    try:
        from scipy.io import loadmat
    except ImportError as exc:
        raise RuntimeError("Install pipeline requirements (scipy) to read the authors' PL .mat files") from exc
    try:
        values = loadmat(path)
    except NotImplementedError as exc:
        raise ValueError(f"Unsupported MATLAB v7.3 file: {path}") from exc
    arrays = [np.asarray(v).squeeze() for k, v in values.items() if not k.startswith("__") and np.asarray(v).size >= 4]
    candidates = []
    for arr in arrays:
        try:
            numeric = arr.astype(float)
        except (TypeError, ValueError):
            continue
        if numeric.ndim == 2 and numeric.shape[0] == 2:
            candidates.append((numeric[0].ravel(), numeric[1].ravel()))
        elif numeric.ndim == 2 and numeric.shape[1] == 2:
            candidates.append((numeric[:, 0].ravel(), numeric[:, 1].ravel()))
    for x, y in candidates:
        n = min(len(x), len(y))
        mask = np.isfinite(x[:n]) & np.isfinite(y[:n]) & (x[:n] > 0)
        if mask.sum() >= 4:
            return x[:n][mask], y[:n][mask]
    raise ValueError(f"Could not identify the documented 2-row distance/path-loss array in {path}")


def prepare(dataset_dir: Path, output_dir: Path) -> Path:
    root = dataset_dir.resolve()
    pl_root = root / "Appendix" / "PL_data"
    files = sorted(pl_root.rglob("*.mat"))
    if len(files) != 6:
        raise FileNotFoundError(f"Expected six scenario/altitude PL .mat files under {pl_root}, found {len(files)}")
    rows, sources = [], []
    for file in files:
        stem = file.stem
        m = re.search(r"(10|15)$", stem)
        if not m:
            raise ValueError(f"Cannot parse documented 10/15 m flight altitude from {file.name}")
        altitude = int(m.group(1))
        scenario = "over_water" if "overwater" in stem.lower() else "sports_field" if "sportsfield" in stem.lower() else "farmland"
        distance, pl = _mat_pair(file)
        for i, (d, y) in enumerate(zip(distance, pl)):
            rows.append({"dataset_id": "measured_a2g_cir_36ghz", "group_id": f"{scenario}_{altitude}m",
                         "scenario": scenario, "altitude_m_source_label": altitude,
                         "frequency_ghz": 3.6, "distance_m": float(d), "path_loss_db_source": float(y),
                         "sample_index": i, "source_file": str(file.resolve())})
        sources.append({"file": str(file.resolve()), "sha256": _hash(file), "scenario": scenario,
                        "altitude_m_source_label": altitude, "rows": int(len(distance)),
                        "distance_min_m": float(np.min(distance)), "distance_max_m": float(np.max(distance))})

    # Raw workbook CIR is retained as a distinct channel-shape product.
    cir_files = sorted((root / "DATASET").rglob("*.xlsx"))
    cir_summary, cir_sources = [], []
    for file in cir_files:
        tab = pd.read_excel(file, header=None)
        if tab.shape[1] < 10 or tab.shape[0] < 4:
            raise ValueError(f"Unexpected CIR workbook shape {tab.shape}: {file}")
        samples = tab.iloc[2:, 6:].apply(pd.to_numeric, errors="coerce").to_numpy(float)
        cir_sources.append({"file": str(file.resolve()), "sha256": _hash(file), "rows": int(len(tab)),
                            "columns": int(tab.shape[1])})
        valid = np.isfinite(samples).any(axis=1)
        # Only shape summaries: do not interpret CIR dB bins as path loss.
        for idx in np.flatnonzero(valid):
            vec = samples[idx]
            finite = vec[np.isfinite(vec)]
            cir_summary.append({"source_file": str(file.resolve()), "snapshot_index": int(idx),
                                "scenario_altitude_group": file.parent.name + "_" + file.stem.rsplit("_", 1)[-1] + "m",
                                "time_s": pd.to_numeric(tab.iloc[idx + 2, 3], errors="coerce"),
                                "carrier_ghz": pd.to_numeric(tab.iloc[idx + 2, 4], errors="coerce"),
                                "sample_rate_mhz": pd.to_numeric(tab.iloc[idx + 2, 5], errors="coerce"),
                                "cir_bin_count": int(len(finite)), "cir_db_median": float(np.median(finite)),
                                "cir_db_peak": float(np.max(finite)), "cir_db_min": float(np.min(finite))})
    out = output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    path = out / "a2g36_pathloss.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    pd.DataFrame(cir_summary).to_csv(out / "a2g36_cir_snapshot_summary.csv", index=False)
    manifest = {"dataset_id": "Measured A2G Channel Dataset (CIR) in Multiple Scenarios",
                "data_doi": "10.17632/8p6r8ft4fz.2", "article_doi": "10.1016/j.dib.2025.112086",
                "path_loss_role": "independent 3.6 GHz propagation trend fit/validation, kept separate from Dataset-32 3.4 GHz",
                "path_loss_semantics": "authors' processed Appendix PL_data; source README describes rows as Tx-Rx distance and calculated path loss; traceable absolute metrology not asserted",
                "cir_role": "CIR descriptive multipath-shape summary only; not absolute path loss or received power",
                "groups_are": "scenario x source-labeled flight altitude; not six independent campaigns",
                "path_loss_inputs": sources, "cir_workbooks": cir_sources,
                "cir_workbook_count": len(cir_files), "cir_snapshots": len(cir_summary),
                "outputs": [str(path), str(out / "a2g36_cir_snapshot_summary.csv")]}
    (out / "a2g36_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset_dir", required=True)
    ap.add_argument("--output_dir", required=True)
    args = ap.parse_args()
    print(prepare(Path(args.dataset_dir), Path(args.output_dir)))

"""Fit and group-holdout-evaluate the separate 3.6 GHz A2G dataset."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _fit(train):
    x = np.log10(train.distance_m.to_numpy(float))
    y = train.path_loss_db_source.to_numpy(float)
    g = train.group_id.to_numpy()
    xc = x - pd.Series(x).groupby(g).transform("mean").to_numpy()
    yc = y - pd.Series(y).groupby(g).transform("mean").to_numpy()
    count = pd.Series(g).map(pd.Series(g).value_counts()).to_numpy(float)
    w = 1.0 / count  # equal aggregate influence per scenario-altitude group
    den = np.dot(w, xc * xc)
    if den <= 1e-12:
        raise ValueError("No within-group distance variation")
    return float(np.dot(w * xc, yc) / den)


def _metrics(actual, pred):
    e = np.asarray(pred) - np.asarray(actual)
    return {"n": int(len(e)), "rmse_db": float(np.sqrt(np.mean(e * e))),
            "mae_db": float(np.mean(np.abs(e))), "bias_pred_minus_measured_db": float(np.mean(e))}


def fit(input_csv: Path, output_dir: Path) -> Path:
    df = pd.read_csv(input_csv)
    req = {"group_id", "scenario", "altitude_m_source_label", "frequency_ghz", "distance_m", "path_loss_db_source"}
    if req - set(df.columns):
        raise ValueError(f"3.6 GHz input missing columns: {sorted(req-set(df.columns))}")
    for c in req - {"group_id", "scenario"}:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=list(req)).copy()
    if not np.allclose(df.frequency_ghz, 3.6, atol=1e-6) or (df.distance_m <= 0).any():
        raise ValueError("Expected positive distances and exactly 3.6 GHz observations")
    groups = sorted(df.group_id.unique())
    if len(groups) != 6 or any((df.group_id == g).sum() < 10 for g in groups):
        raise ValueError(f"Expected six scenario-altitude groups with >=10 rows each; got {[(g,int((df.group_id==g).sum())) for g in groups]}")
    folds = []
    for held in groups:
        train, test = df[df.group_id != held], df[df.group_id == held]
        beta = _fit(train)
        y = test.path_loss_db_source.to_numpy(float)
        x = np.log10(test.distance_m.to_numpy(float))
        # Group offset removed: validates distance trend, not absolute transfer.
        folds.append({"heldout_group": held, "training_groups": [g for g in groups if g != held],
                      "slope_db_per_decade": beta,
                      "within_group_demeaned_metrics": _metrics(y-y.mean(), beta*(x-x.mean()))})
    beta = _fit(df)
    out = output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    artifact = {"model_type": "a2g36_group_fixed_effects_log_distance_slope",
                "calibration_status": "separate_3p6ghz_empirical_distance_trend_group_holdout",
                "frequency_ghz": 3.6, "path_loss_slope_db_per_decade": beta,
                "path_loss_exponent_n": beta/10.0,
                "fit_equation": "PL = group_offset + beta*log10(3D_distance_m) + residual",
                "fit_method": "within-group centered least squares with equal total weight per scenario-altitude group",
                "validation": {"method": "leave-one-scenario-altitude-group-out; demeaned holdout validates trend only",
                               "groups": groups, "folds": folds,
                               "mean_group_rmse_db": float(np.mean([x['within_group_demeaned_metrics']['rmse_db'] for x in folds]))},
                "domain": {"distance_min_m": float(df.distance_m.min()), "distance_max_m": float(df.distance_m.max()),
                           "altitudes_source_labeled_m": sorted(df.altitude_m_source_label.unique().astype(float).tolist()),
                           "scenarios": sorted(df.scenario.unique().tolist()), "frequency_ghz": 3.6,
                           "distance_semantics": "source-provided Tx-Rx distance; not independently reconstructed as horizontal or 3D"},
                "source": {"dataset_id": "Measured A2G Channel Dataset (CIR) in Multiple Scenarios",
                           "input_csv": str(input_csv.resolve()), "rows": int(len(df)),
                           "groups": "six scenario x altitude source files; group holdout is internal validation, not six independent datasets"},
                "limitations": ["Kept separate from 3.4 GHz Dataset-32; no pooled fit or frequency transfer.",
                                "Group-specific offsets are removed for fitting and scoring; absolute path loss on a new campaign is not validated.",
                                "Only two source-labeled heights (10 and 15 m) and three environments; not a general altitude law.",
                                "Source-processed PL is used as supplied; traceable TX/RX metrology uncertainty is not established.",
                                "This does not calibrate SNR, PER, C2 delay, packet loss, or goodput."]}
    target=out/"layer1_a2g36_pathloss_fit.json"; target.write_text(json.dumps(artifact,indent=2),encoding="utf-8"); return target


if __name__ == "__main__":
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument("--input_csv",required=True); ap.add_argument("--output_dir",required=True)
    a=ap.parse_args(); print(fit(Path(a.input_csv),Path(a.output_dir)))

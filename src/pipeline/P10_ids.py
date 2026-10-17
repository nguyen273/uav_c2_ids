"""Evaluate IDS on PX4 SITL cyber-physical twin traffic.

Splits keep repeated realizations of an identical mission/channel/attack
scenario together. Metrics are saved with the evaluation definition.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report, confusion_matrix, f1_score
from sklearn.model_selection import GroupShuffleSplit

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("P10_IDS")
NETWORK_FEATURES = ["packet_count", "bytes_total", "packet_rate_pps", "byte_rate_bps",
                    "packet_length_mean", "packet_length_std", "packet_length_p95",
                    "interarrival_mean_ms", "interarrival_std_ms", "udp_fraction", "active_flow_count"]
TELEMETRY_FEATURES = ["altitude_relative_m", "speed_m_s", "distance_to_gcs_m"]
FEATURE_SETS = {"network_only": NETWORK_FEATURES, "telemetry_only": TELEMETRY_FEATURES,
                "network_plus_telemetry": NETWORK_FEATURES + TELEMETRY_FEATURES}
LABEL_MAP = {"T0_Benign": 0, "T1_Replay": 1, "T2_FDI": 2, "T4_DoS": 3}


def load_dataset(campaign_dir):
    root = Path(campaign_dir)
    if not root.is_dir():
        raise FileNotFoundError(root)
    dfs = []
    for run in sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("run_")):
        view, manifest = run / "views" / "window_level.parquet", run / "metadata.json"
        if not view.is_file() or not manifest.is_file():
            continue
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        if meta.get("status") != "complete":
            continue
        df = pd.read_parquet(view)
        df["run_id"] = meta["run_id"]
        df["mission"] = meta["mission_id"]
        df["channel"] = meta["channel_id"]
        df["attack_type"] = meta["attack_id"]
        dfs.append(df)
    if not dfs:
        raise ValueError("No complete run with window-level dataset found")
    data = pd.concat(dfs, ignore_index=True)
    required = set(sum(FEATURE_SETS.values(), []) + ["attack_label", "attack_class", "run_id", "mission", "channel", "attack_type"])
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"Dataset missing columns: {sorted(missing)}")
    unknown = set(data.attack_type.dropna().unique()) - set(LABEL_MAP)
    if unknown:
        raise ValueError(f"Unknown attack labels: {sorted(unknown)}")
    # Empty resample windows are benign/background by definition; telemetry is
    # required and must have been aligned upstream rather than silently faked.
    for c in set(sum(FEATURE_SETS.values(), [])):
        data[c] = pd.to_numeric(data[c], errors="coerce")
    all_features = list(dict.fromkeys(sum(FEATURE_SETS.values(), [])))
    if data[all_features].isna().any().any():
        raise ValueError("Missing/non-numeric feature values remain after telemetry alignment")
    return data


def scenario_groups(df):
    return df["mission"].astype(str) + "|" + df["channel"].astype(str) + "|" + df["attack_type"].astype(str)


def split_groups(X, y, groups):
    if pd.Series(groups).nunique() < 2:
        raise ValueError("At least two distinct mission/channel/attack groups are required")
    train_idx, test_idx = next(GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42).split(X, y, groups))
    if set(y.iloc[train_idx].unique()) != set(y.unique()) or set(y.iloc[test_idx].unique()) != set(y.unique()):
        raise ValueError("Grouped split does not contain every target class in both train and test; collect more scenarios or use a predeclared split")
    return train_idx, test_idx


def score_split(name, X_train, y_train, X_test, y_test, labels):
    clf = RandomForestClassifier(n_estimators=200, random_state=42, class_weight="balanced_subsample", n_jobs=-1)
    clf.fit(X_train, y_train)
    pred = clf.predict(X_test)
    return {"experiment": name, "f1_macro": float(f1_score(y_test, pred, average="macro", zero_division=0)),
            "classification_report": classification_report(y_test, pred, labels=labels, output_dict=True, zero_division=0),
            "confusion_matrix": confusion_matrix(y_test, pred, labels=labels).tolist(), "labels": labels}


def evaluate(df):
    groups = scenario_groups(df)
    results = []
    y_bin = (df["attack_label"].astype(int) > 0).astype(int)
    y_multi = df["attack_class"].astype(int)
    if not set(y_multi.unique()).issubset(set(LABEL_MAP.values())):
        raise ValueError(f"Unknown event-derived attack classes: {sorted(set(y_multi.unique()) - set(LABEL_MAP.values()))}")
    observed_labels = sorted(y_multi.unique().tolist())
    for feature_set, feature_names in FEATURE_SETS.items():
        X = df[feature_names]
        tr_bin, te_bin = split_groups(X, y_bin, groups)
        result = score_split(f"{feature_set}_binary_grouped_scenario_holdout",
                             X.iloc[tr_bin], y_bin.iloc[tr_bin], X.iloc[te_bin], y_bin.iloc[te_bin], [0, 1])
        result['feature_set'] = feature_set
        result['features'] = feature_names
        results.append(result)

        tr_multi, te_multi = split_groups(X, y_multi, groups)
        result = score_split(f"{feature_set}_multiclass_grouped_scenario_holdout",
                             X.iloc[tr_multi], y_multi.iloc[tr_multi], X.iloc[te_multi], y_multi.iloc[te_multi], observed_labels)
        result['feature_set'] = feature_set
        result['features'] = feature_names
        results.append(result)

        for channel in sorted(df.channel.unique()):
            train, test = df.channel != channel, df.channel == channel
            ytr, yte = (df.loc[train, "attack_label"].astype(int) > 0).astype(int), (df.loc[test, "attack_label"].astype(int) > 0).astype(int)
            if train.sum() == 0 or test.sum() == 0 or set(ytr.unique()) != {0, 1} or set(yte.unique()) != {0, 1}:
                raise ValueError(f"E3 leave-one-channel-out split {channel!r} lacks both binary classes")
            res = score_split(f"{feature_set}_leave_channel_out_{channel}", X.loc[train], ytr, X.loc[test], yte, [0, 1])
            res['feature_set'] = feature_set
            res['features'] = feature_names
            res["held_out_channel"] = channel
            results.append(res)
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign_dir", required=True)
    args = ap.parse_args()
    try:
        data = load_dataset(args.campaign_dir)
        metrics = {"scope": "PX4 SITL/Gazebo + MAVSDK/MAVLink transport with Layer-3 stress shaping; not RF-calibrated and not evidence of physical UAV attack impact",
                   "feature_sets": FEATURE_SETS,
                   "features_note": "Network-only, telemetry-only, and combined ablations; packet header-derived features do not include decoded MAVLink payload fields.",
                   "feature_semantics": {"altitude_relative_m": "live PX4 SITL MAVSDK relative altitude",
                                         "speed_m_s": "derived from consecutive live PX4 position reports",
                                         "distance_to_gcs_m": "live PX4 position relative to AFAR transmitter/GCS anchor"},
                   "attack_classes": {str(v): k for k, v in LABEL_MAP.items()},
                   "split_unit": "mission/channel/attack scenario; repetitions stay together",
                   "n_windows": int(len(data)), "results": evaluate(data)}
        out = Path(args.campaign_dir) / "evaluation_metrics.json"
        out.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        logger.info("Saved grouped evaluation metrics to %s", out)
    except Exception as exc:
        logger.exception("FAIL-CLOSED: %s", exc)
        sys.exit(1)

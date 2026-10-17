"""Normalize the Dryad Ericsson 5G NSA UAV dataset without mixing RATs."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

# Tower coordinates are the published LW1 anchor used in the dataset's supplied example.m.
LW1_LAT, LW1_LON = 35.727487047024084, -78.69594208319383


def _read_metric(path: Path, metric: str, prefix: str, value_name: str | None = None) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"Time and date", "Longitude", "Latitude", "Altitude", metric}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path} missing {sorted(missing)}")
    out = pd.DataFrame({"timestamp": pd.to_datetime(df["Time and date"], errors="coerce", utc=True),
                        f"{prefix}_longitude": pd.to_numeric(df.Longitude, errors="coerce"),
                        f"{prefix}_latitude": pd.to_numeric(df.Latitude, errors="coerce"),
                        f"{prefix}_altitude_source_m": pd.to_numeric(df.Altitude, errors="coerce"),
                        (value_name or prefix): pd.to_numeric(df[metric], errors="coerce")})
    return out.replace([np.inf, -np.inf], np.nan).dropna().sort_values("timestamp")


def _attach_nearest(master, other, value_col, tolerance_s=2.0):
    if other.empty:
        master[value_col] = np.nan
        return master
    right = other[["timestamp", value_col]].rename(columns={"timestamp": f"{value_col}_source_timestamp"})
    merged = pd.merge_asof(master.sort_values("timestamp"), right.sort_values(f"{value_col}_source_timestamp"),
                           left_on="timestamp", right_on=f"{value_col}_source_timestamp", direction="nearest",
                           tolerance=pd.Timedelta(seconds=tolerance_s))
    merged[f"{value_col}_match_abs_seconds"] = (merged["timestamp"] - merged[f"{value_col}_source_timestamp"]).dt.total_seconds().abs()
    return merged


def _read_categorical(path: Path, metric: str, value_name: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={metric: "string"})
    if "Time and date" not in df or metric not in df:
        raise ValueError(f"{path} must contain timestamp and {metric}")
    return pd.DataFrame({"timestamp": pd.to_datetime(df["Time and date"], errors="coerce", utc=True),
                         value_name: df[metric].astype("string")}).dropna().sort_values("timestamp")


def prepare(dataset_dir: Path, output_dir: Path) -> Path:
    root = dataset_dir.resolve()
    rows, inputs = [], []
    for orientation in ("yaw45", "yaw315"):
        folder = root / orientation
        if not folder.is_dir():
            raise FileNotFoundError(f"Missing orientation folder {folder}")
        # LTE is f1; NR is f2, as stated by the supplied MATLAB code and Dryad README.
        for rat, idx, rsrp_name, sinr_name, value_r, value_s in (
            ("LTE", "f1", "inputf1_rsrp_with_header.csv", "inputf1_sinr_with_header.csv", "LTE RSRP", "LTE SINR"),
            ("NR", "f2", "inputf2_rsrp_with_header.csv", "inputf2_sinr_with_header.csv", "NR RSRP", "NR SINR"),
        ):
            rsrp_path, sinr_path = folder / rsrp_name, folder / sinr_name
            if not rsrp_path.is_file() or not sinr_path.is_file():
                raise FileNotFoundError(f"Required {rat} KPI CSV missing under {folder}")
            base = _read_metric(rsrp_path, value_r, "rsrp", "rsrp_dbm")
            sinr = _read_metric(sinr_path, value_s, "sinr", "sinr_db")[["timestamp", "sinr_db"]]
            base = _attach_nearest(base, sinr, "sinr_db")
            cell_path = folder / f"input{idx}_cellid_with_header.csv"
            if cell_path.is_file():
                base = _attach_nearest(base, _read_categorical(cell_path, f"{rat} Cell ID", "cell_id")[["timestamp", "cell_id"]], "cell_id")
            source_paths = [rsrp_path, sinr_path] + ([cell_path] if cell_path.is_file() else [])
            if rat == "NR":
                for name, col, dest in (("inputf2_cqi_with_header.csv", "NR CQI", "cqi"),
                                        ("inputf2_mcs_with_header.csv", "NR MCS", "mcs")):
                    p = folder / name
                    if p.is_file():
                        aux = _read_metric(p, col, dest, dest)[["timestamp", dest]]
                        base = _attach_nearest(base, aux, dest)
                        source_paths.append(p)
            if rat == "NR":
                tp = folder / "input_throughput_with_header.csv"
                if tp.is_file():
                    t = _read_metric(tp, "Throughput", "throughput", "throughput_mbps")[["timestamp", "throughput_mbps"]]
                    base = _attach_nearest(base, t, "throughput_mbps", tolerance_s=2.0)
                    source_paths.append(tp)
            lat, lon = base["rsrp_latitude"].to_numpy(float), base["rsrp_longitude"].to_numpy(float)
            lat0 = np.radians(LW1_LAT); latr = np.radians(lat)
            dlat = latr-lat0; dlon=np.radians(lon-LW1_LON)
            a=np.sin(dlat/2)**2+np.cos(lat0)*np.cos(latr)*np.sin(dlon/2)**2
            base["horizontal_distance_to_lw1_m"] = 2*6371008.8*np.arcsin(np.sqrt(np.clip(a,0,1)))
            base["orientation"] = orientation; base["flight_group"] = orientation
            base["radio_access_technology"] = rat; base["source_rsrp_file"] = str(rsrp_path.resolve())
            base["tower_latitude"] = LW1_LAT; base["tower_longitude"] = LW1_LON
            # The source altitude field is retained as logged; no AGL conversion is assumed.
            base["altitude_source_m"] = base["rsrp_altitude_source_m"]
            base["rsrp_value_dbm"] = base["rsrp_dbm"]
            cols = ["timestamp", "orientation", "flight_group", "radio_access_technology", "rsrp_value_dbm",
                    "sinr_db", "sinr_db_match_abs_seconds", "cell_id", "cell_id_match_abs_seconds", "cqi", "cqi_match_abs_seconds",
                    "mcs", "mcs_match_abs_seconds", "throughput_mbps", "throughput_mbps_match_abs_seconds", "horizontal_distance_to_lw1_m",
                    "rsrp_latitude", "rsrp_longitude", "altitude_source_m", "tower_latitude", "tower_longitude", "source_rsrp_file"]
            for c in cols:
                if c not in base: base[c] = np.nan
            clean = base[cols].copy()
            rows.append(clean)
            for p in source_paths:
                inputs.append({"file": str(p.resolve()), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    result = pd.concat(rows, ignore_index=True).sort_values(["radio_access_technology", "orientation", "timestamp"])
    result["timestamp"] = result.timestamp.astype(str)
    out=output_dir.resolve(); out.mkdir(parents=True,exist_ok=True)
    target=out/"aerpaw_5g_kpis.csv"; result.to_csv(target,index=False)
    anchor_file = root / "example.m"
    manifest={"dataset_id":"Ericsson 5G NSA network RF and throughput measurements on AERPAW network",
              "doi":"10.5061/dryad.wh70rxx06", "source_citation":"Asokan et al. (2025), Dryad",
              "role":"independent empirical Layer-3 KPI relation check for LTE/NR; not MAVLink C2 calibration and not Layer-1 path-loss input",
              "source_domain":{"network":"Ericsson 5G NSA / Quectel modem", "flight_height_source":"30 m campaign label in Dryad README",
                               "orientations":["yaw45","yaw315"], "tower_anchor_source":"LW1 coordinates in bundled Dryad example.m"},
              "matching":"RSRP timestamp master; SINR/CQI/MCS/throughput nearest timestamp within 2 seconds where present",
              "altitude_semantics":"source field retained without conversion; not asserted AGL",
              "distance_semantics":"horizontal geodesic distance to LW1 anchor; not 3D distance",
              "anchor_source_file":str(anchor_file.resolve()),
              "anchor_source_sha256":hashlib.sha256(anchor_file.read_bytes()).hexdigest() if anchor_file.is_file() else None,
              "throughput_unit":"Mbps per Dryad README", "inputs":inputs,"rows":int(len(result)),"output":str(target)}
    (out/"aerpaw_5g_manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    return target


if __name__=="__main__":
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument("--dataset_dir",required=True); ap.add_argument("--output_dir",required=True)
    a=ap.parse_args(); print(prepare(Path(a.dataset_dir),Path(a.output_dir)))

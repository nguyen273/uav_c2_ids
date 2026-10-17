"""Normalize AERPAW Pawprints 4G KPIs with source and geometry provenance.

This is an LTE radio-KPI reference branch. The dataset does not provide C2
packet loss, RTT, jitter, or application goodput and is not wired into P5.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

# Base-station coordinates and antenna height from the Dryad README.
BS_LAT, BS_LON, BS_ANTENNA_HEIGHT_M = 35.727480, -78.695920, 12.0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def prepare(dataset_dir: Path, output_dir: Path, input_csv: Path | None = None) -> Path:
    root = dataset_dir.resolve()
    source = input_csv.resolve() if input_csv else root / 'Logs' / 'pawprints_4G.csv'
    if not source.is_file():
        raise FileNotFoundError(f'Pawprints 4G CSV not found: {source}')
    raw = pd.read_csv(source)
    required = {'timestamp', 'longitude', 'latitude', 'altitude', 'rsrp', 'rsrq', 'rssi',
                'earfcn', 'pci', 'is_connected', 'ta'}
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f'Pawprints 4G source missing columns: {missing}')
    data = raw.copy()
    for column in required - {'timestamp'}:
        data[column] = pd.to_numeric(data[column], errors='coerce')
    data['timestamp_ms'] = pd.to_numeric(data['timestamp'], errors='coerce')
    data['timestamp_utc'] = pd.to_datetime(data['timestamp_ms'], unit='ms', errors='coerce', utc=True)
    data = data.replace([np.inf, -np.inf], np.nan)
    data = data.dropna(subset=['timestamp_ms', 'timestamp_utc', 'longitude', 'latitude', 'rsrp', 'pci'])
    data = data[(data['latitude'].between(-90, 90)) & (data['longitude'].between(-180, 180))]

    lat0 = np.radians(BS_LAT)
    lat = np.radians(data['latitude'].to_numpy(float))
    dlat = lat - lat0
    dlon = np.radians(data['longitude'].to_numpy(float) - BS_LON)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat0) * np.cos(lat) * np.sin(dlon / 2) ** 2
    data['horizontal_distance_to_bs_m'] = 2 * 6_371_008.8 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    data['bs_latitude'] = BS_LAT
    data['bs_longitude'] = BS_LON
    data['bs_antenna_height_source_m'] = BS_ANTENNA_HEIGHT_M
    data['connected_record'] = data['is_connected'].eq(1)
    data['dataset_id'] = 'AERPAW Pawprints Aerial Android-based 4G semi-circle measurements'
    data['frequency_source'] = 'EARFCN retained; do not infer a single carrier without validating band/channel mapping'
    data['altitude_semantics'] = 'source-reported altitude retained; see source README; no conversion applied'

    preferred = ['timestamp_utc', 'timestamp_ms', 'longitude', 'latitude', 'altitude', 'horizontal_distance_to_bs_m',
                 'rsrp', 'rsrq', 'rssi', 'asu', 'earfcn', 'pci', 'is_connected', 'ta', 'ci', 'tac',
                 'bands', 'modes', 'mcc', 'mnc', 'bs_latitude', 'bs_longitude',
                 'bs_antenna_height_source_m', 'dataset_id', 'frequency_source', 'altitude_semantics']
    columns = [c for c in preferred if c in data.columns]
    data = data[columns].sort_values(['timestamp_ms', 'pci']).reset_index(drop=True)

    out = output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    target = out / 'pawprints4g_kpis.csv'
    data.to_csv(target, index=False)
    connected = data[data['is_connected'].eq(1)]
    manifest = {
        'dataset_id': 'AERPAW: Aerial Android-based 4G measurements for semi circle trajectories around a private base station',
        'doi': '10.5061/dryad.gmsbcc30x',
        'source_file': str(source), 'source_sha256': sha256(source),
        'role': 'external LTE radio KPI and geometry reference; not MAVLink C2 calibration',
        'source_domain': {'trajectory': 'semi-circle', 'campaign_altitude_label_m': 50,
                          'base_station_latitude': BS_LAT, 'base_station_longitude': BS_LON,
                          'base_station_antenna_height_m': BS_ANTENNA_HEIGHT_M,
                          'source_altitude_values_are_preserved_without_reinterpretation': True},
        'columns_present': list(raw.columns), 'rows_retained': int(len(data)),
        'connected_rows': int(len(connected)), 'pci_values': sorted(data['pci'].dropna().unique().tolist()),
        'earfcn_values': sorted(data['earfcn'].dropna().unique().tolist()),
        'measurement_columns': ['RSRP', 'RSRQ', 'RSSI', 'PCI', 'connected flag', 'timing advance'],
        'not_measured_or_not_provided': ['MAVLink packets', 'packet loss', 'RTT', 'jitter', 'C2 goodput', 'application throughput'],
        'distance_semantics': 'horizontal geodesic distance derived from source GPS and Dryad base-station coordinates',
        'output_file': str(target),
    }
    (out / 'pawprints4g_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset_dir', required=True)
    parser.add_argument('--input_csv')
    parser.add_argument('--output_dir', required=True)
    args = parser.parse_args()
    result = prepare(Path(args.dataset_dir), Path(args.output_dir), Path(args.input_csv) if args.input_csv else None)
    print(result)

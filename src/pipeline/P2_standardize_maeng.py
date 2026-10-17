import argparse
import json
import logging
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.interpolate import interp1d

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("P2_Standardize")

# AERPAW Lake Wheeler BS tower location (from AFAR main.m)
BS_LAT = 35.727451
BS_LON = -78.695974
BS_ALT = 12.0  # approximate tower height in meters
DEG_TO_M = 1.113195e5

def parse_iq_timestamp(filename):
    """Extract Unix timestamp from IQ filename."""
    match = re.search(r'results_(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{3})', filename)
    if not match:
        return None
    year, month, day, hour, minute, sec, ms = match.groups()
    eastern = ZoneInfo('America/New_York')
    dt = datetime(int(year), int(month), int(day),
                  int(hour), int(minute), int(sec),
                  int(ms) * 1000, tzinfo=eastern)
    return dt.timestamp()

def compute_power_from_iq(data_path, n_iq_samples=40800):
    """Compute an uncalibrated relative IQ RMS level, not dBm."""
    try:
        samples = np.fromfile(str(data_path), dtype=np.complex128)
        iq = samples[:n_iq_samples]
        if len(iq) == 0: return None
        power_linear = np.mean(np.abs(iq) ** 2)
        return float(10 * np.log10(power_linear + 1e-30))
    except Exception as e:
        logger.error(f"Error computing power for {data_path}: {e}")
        return None

def read_gps_binary(gps_data_path, gps_meta_path):
    """Read GPS binary file (rf64_le format) using metadata annotations."""
    with open(gps_meta_path, 'r') as f:
        meta = json.load(f)
    sections = {}
    for ann in meta.get('annotations', []):
        sections[ann.get('core:comment', '')] = (ann.get('core:sample_start', 0), ann.get('core:sample_count', 0))
    
    data = np.fromfile(str(gps_data_path), dtype=np.float64)
    df = pd.DataFrame({
        'longitude': data[sections.get('longitude', (0,0))[0]:sections.get('longitude', (0,0))[0]+sections.get('longitude', (0,0))[1]] if 'longitude' in sections else [],
        'latitude': data[sections.get('latitude', (0,0))[0]:sections.get('latitude', (0,0))[0]+sections.get('latitude', (0,0))[1]] if 'latitude' in sections else [],
        'altitude_m': data[sections.get('altitude', (0,0))[0]:sections.get('altitude', (0,0))[0]+sections.get('altitude', (0,0))[1]] if 'altitude' in sections else [],
        'timestamp': data[sections.get('timestamp', (0,0))[0]:sections.get('timestamp', (0,0))[0]+sections.get('timestamp', (0,0))[1]] if 'timestamp' in sections else [],
    })
    return df

def process_maeng_altitude(alt_dir, target_altitude):
    """Process Maeng IQ files and GPS for one altitude."""
    gps_dir, iq_dir = alt_dir / 'GPS_logs', alt_dir / 'IQ_samples'
    if not gps_dir.exists() or not iq_dir.exists():
        logger.warning(f"Missing GPS or IQ in {alt_dir}")
        return None

    gps_data_files = sorted(gps_dir.glob('*vehicleOut.sigmf-data'))
    gps_meta_files = sorted(gps_dir.glob('*vehicleOut.sigmf-meta'))
    if not gps_data_files: return None

    gps_df = read_gps_binary(gps_data_files[0], gps_meta_files[0])
    iq_data_files = sorted(iq_dir.glob('results_*.sigmf-data'))
    
    rows = []
    for data_file in iq_data_files:
        ts = parse_iq_timestamp(data_file.name)
        power_dB = compute_power_from_iq(data_file)
        if ts is not None and power_dB is not None:
            rows.append({'timestamp': ts, 'iq_power_dbfs_like': power_dB})

    if not rows: return None
    iq_df = pd.DataFrame(rows).sort_values('timestamp').reset_index(drop=True)

    gps_sorted = gps_df.sort_values('timestamp')
    ts_gps = gps_sorted['timestamp'].values
    iq_df = iq_df[(iq_df['timestamp'] >= ts_gps[0]) & (iq_df['timestamp'] <= ts_gps[-1])].copy()

    interp_lon = interp1d(ts_gps, gps_sorted['longitude'].values, fill_value='extrapolate')
    interp_lat = interp1d(ts_gps, gps_sorted['latitude'].values, fill_value='extrapolate')
    interp_alt = interp1d(ts_gps, gps_sorted['altitude_m'].values, fill_value='extrapolate')

    ts_iq = iq_df['timestamp'].values
    iq_df['longitude'] = interp_lon(ts_iq)
    iq_df['latitude'] = interp_lat(ts_iq)
    iq_df['altitude_m'] = interp_alt(ts_iq)

    dx = (iq_df['longitude'] - BS_LON) * DEG_TO_M
    dy = (iq_df['latitude'] - BS_LAT) * DEG_TO_M
    dz = iq_df['altitude_m'] - BS_ALT
    iq_df['distance_to_bs_m'] = np.sqrt(dx**2 + dy**2 + dz**2)
    iq_df['altitude_target_m'] = target_altitude
    iq_df['provenance'] = 'empirical'

    return iq_df

def process_maeng(input_dir, output_dir):
    """P2: Standardize Maeng dataset for Layer 1 propagation fit."""
    base = Path(input_dir)
    ALTITUDE_DIRS = {'30m': 30, '50m': 50, '70m': 70, '90m': 90, '110m': 110}
    
    all_frames = []
    for dir_name, target_alt in ALTITUDE_DIRS.items():
        alt_dir = base / dir_name
        if alt_dir.exists():
            df = process_maeng_altitude(alt_dir, target_alt)
            if df is not None:
                all_frames.append(df)
                
    if all_frames:
        combined = pd.concat(all_frames, ignore_index=True)
        # Preserve the measurement semantics: raw IQ level is not calibrated RSRP.
        combined['distance_m'] = combined['distance_to_bs_m']
        combined['frequency_ghz_assumption'] = 3.51
        combined['power_semantics'] = 'uncalibrated IQ RMS level; not dBm/RSRP'
        out_path = Path(output_dir) / 'maeng_standardized.csv'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        combined.to_csv(out_path, index=False)
        logger.warning(f"Saved exploratory IQ/GPS data to {out_path} ({len(combined)} rows); not valid for absolute path-loss calibration")
    else:
        logger.warning("No Maeng data processed.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--maeng_dir', type=str, default='../raw_data/maeng_rsrp/extracted')
    parser.add_argument('--output_dir', type=str, default='../data/processed')
    args = parser.parse_args()
    process_maeng(args.maeng_dir, args.output_dir)

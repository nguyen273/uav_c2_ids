"""Compare AADM development-DT and testbed metric distributions by flight.

This is an external AERPAW domain-gap/reference analysis. It is not validation
of the project's PX4/MAVLink C2 twin and does not imply flight-level pairing.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import wasserstein_distance


METRIC_ALIASES = {
    'power_dBm': ('power_dBm', 'power_dbm'),
    'snr_dB': ('snr_dB', 'snr_db'),
    'altitude_m': ('altitude_m',),
    'distance_to_bs_m': ('distance_to_bs_m', 'distance_to_bs'),
    'goodput_mbps': ('goodput_mbps', 'goodput_Mbps'),
}


def _normalize(path: Path, stage: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {'flight_id', 'bs_id'}
    if required - set(frame):
        raise ValueError(f'{path} missing required fields {sorted(required - set(frame))}')
    normalized = frame[['flight_id', 'bs_id']].copy()
    for target, aliases in METRIC_ALIASES.items():
        source = next((name for name in aliases if name in frame), None)
        normalized[target] = pd.to_numeric(frame[source], errors='coerce') if source else np.nan
    normalized['stage'] = stage
    normalized = normalized.replace([np.inf, -np.inf], np.nan)
    normalized['flight_id'] = normalized.flight_id.astype(str)
    normalized['bs_id'] = normalized.bs_id.astype('string')
    return normalized


def compare(development_csv: Path, testbed_csv: Path, output_dir: Path) -> Path:
    dev = _normalize(development_csv, 'development_dt')
    real = _normalize(testbed_csv, 'testbed')
    both = pd.concat([dev, real], ignore_index=True)
    # Equal flight-level weighting avoids treating dense timestamp rows as
    # independent experimental replicates.
    summaries = {}
    metrics = [m for m in METRIC_ALIASES if both[m].notna().any()]
    for bs in sorted(both.bs_id.dropna().unique().tolist()):
        summaries[str(bs)] = {}
        subset = both[both.bs_id == bs]
        for metric in metrics:
            flight_medians = (subset.dropna(subset=[metric])
                              .groupby(['stage', 'flight_id'])[metric].median().reset_index())
            d = flight_medians.loc[flight_medians.stage == 'development_dt', metric].to_numpy(float)
            t = flight_medians.loc[flight_medians.stage == 'testbed', metric].to_numpy(float)
            if not len(d) or not len(t):
                continue
            summaries[str(bs)][metric] = {
                'unit': {'power_dBm': 'dBm', 'snr_dB': 'dB', 'altitude_m': 'source altitude units',
                         'distance_to_bs_m': 'm', 'goodput_mbps': 'Mbps'}[metric],
                'aggregation': 'median within each flight, then compare flight-level values with equal flight weight',
                'development_flights': int(len(d)), 'testbed_flights': int(len(t)),
                'development_flight_median': float(np.median(d)),
                'testbed_flight_median': float(np.median(t)),
                'median_shift_testbed_minus_development': float(np.median(t) - np.median(d)),
                'wasserstein_distance_between_flight_medians': float(wasserstein_distance(d, t)),
                'interpretation': 'unpaired source-domain distribution comparison; not a paired error score',
            }
    result = {
        'evaluation_type': 'AADM_external_development_DT_vs_testbed_flight_level_domain_comparison',
        'development_source_csv': str(development_csv.resolve()),
        'testbed_source_csv': str(testbed_csv.resolve()),
        'flight_counts': {'development_rows': int(len(dev)), 'development_flights': int(dev.flight_id.nunique()),
                          'testbed_rows': int(len(real)), 'testbed_flights': int(real.flight_id.nunique())},
        'base_station_metrics': summaries,
        'limitations': [
            'Development and testbed flights are treated as independent source domains; no one-to-one flight pairing is assumed.',
            'This evaluates AERPAW AADM source DT versus testbed distributions, not the project PX4/MAVLink twin.',
            'Reported transfer rate is a data-mule application KPI, not C2 goodput; no C2 RTT/jitter/loss is inferred.',
            'Flight is the comparison unit; time rows are summarized within flight and are not independent replicates.',
        ],
    }
    out = output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    target = out / 'aadm_development_testbed_domain_comparison.json'
    target.write_text(json.dumps(result, indent=2), encoding='utf-8')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--development_csv', required=True)
    parser.add_argument('--testbed_csv', required=True)
    parser.add_argument('--output_dir', required=True)
    args = parser.parse_args()
    print(compare(Path(args.development_csv), Path(args.testbed_csv), Path(args.output_dir)))

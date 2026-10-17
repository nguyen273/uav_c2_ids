"""Exploratory, contiguous-time holdout checks for Pawprints 4G KPIs.

One semi-circle flight is not an independent-flight validation set. Scores
are exploratory within-flight checks, not evidence for C2 or a general LTE
propagation model.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _metrics(actual, predicted):
    error = np.asarray(predicted, dtype=float) - np.asarray(actual, dtype=float)
    return {'n': int(len(error)), 'rmse_db': float(np.sqrt(np.mean(error ** 2))),
            'mae_db': float(np.mean(np.abs(error))), 'bias_pred_minus_observed_db': float(np.mean(error))}


def evaluate(input_csv: Path, output_dir: Path, blocks: int = 5) -> Path:
    df = pd.read_csv(input_csv)
    required = {'timestamp_ms', 'horizontal_distance_to_bs_m', 'rsrp', 'pci', 'is_connected'}
    if required - set(df.columns):
        raise ValueError(f'Pawprints normalized input missing {sorted(required - set(df.columns))}')
    for col in required:
        if col != 'timestamp_ms':
            df[col] = pd.to_numeric(df[col], errors='coerce')
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=list(required))
    df = df[(df.is_connected == 1) & (df.horizontal_distance_to_bs_m > 1)].copy()
    df = df.sort_values('timestamp_ms').reset_index(drop=True)
    if len(df) < max(50, blocks * 10):
        raise ValueError(f'Need at least {max(50, blocks * 10)} connected observations; found {len(df)}')
    if blocks < 3 or blocks > 10:
        raise ValueError('blocks must be between 3 and 10')

    df['time_block'] = pd.qcut(np.arange(len(df)), q=blocks, labels=False, duplicates='drop')
    folds = []
    for held in sorted(df.time_block.unique()):
        train = df[df.time_block != held]
        test = df[df.time_block == held]
        if len(train) < 20 or len(test) < 5 or train.pci.nunique() == 0:
            continue
        # Cell reselection can create an apparent distance trend through
        # different PCI-specific offsets. Fit one shared distance slope with
        # PCI fixed intercepts, and refuse a fold containing an unseen PCI.
        pcis = sorted(train.pci.astype(int).unique().tolist())
        if not set(test.pci.astype(int).unique()).issubset(pcis):
            continue
        x_train = np.log10(train.horizontal_distance_to_bs_m.to_numpy(float))
        x_test = np.log10(test.horizontal_distance_to_bs_m.to_numpy(float))
        train_pci = train.pci.astype(int).to_numpy()
        test_pci = test.pci.astype(int).to_numpy()
        design_train = np.column_stack([x_train] + [(train_pci == pci).astype(float) for pci in pcis])
        design_test = np.column_stack([x_test] + [(test_pci == pci).astype(float) for pci in pcis])
        coefficients, *_ = np.linalg.lstsq(design_train, train.rsrp.to_numpy(float), rcond=None)
        prediction = design_test @ coefficients
        folds.append({
            'heldout_contiguous_time_block': int(held),
            'train_rows': int(len(train)), 'test_rows': int(len(test)),
            'training_pci_values': pcis,
            'model': 'shared_log10_distance_slope_with_PCI_fixed_intercepts',
            'rsrp_distance_trend_slope_db_per_decade': float(coefficients[0]),
            'heldout_metrics': _metrics(test.rsrp.to_numpy(float), prediction),
        })

    connected_summary = {}
    for pci, group in df.groupby('pci'):
        connected_summary[str(int(pci))] = {
            'rows': int(len(group)), 'rsrp_median_dbm': float(group.rsrp.median()),
            'rsrq_median_db': float(group.rsrq.median()) if 'rsrq' in group else None,
            'rssi_median_dbm': float(group.rssi.median()) if 'rssi' in group else None,
            'horizontal_distance_range_m': [float(group.horizontal_distance_to_bs_m.min()),
                                            float(group.horizontal_distance_to_bs_m.max())],
        }
    report = {
        'evaluation_type': 'Pawprints4G_single_flight_contiguous_time_blocked_RSRP_distance_trend',
        'source': 'AERPAW Pawprints Dryad DOI 10.5061/dryad.gmsbcc30x',
        'rows_used': int(len(df)), 'block_count_requested': blocks,
        'holdout_folds': folds,
        'connected_PCI_summary': connected_summary,
        'limitations': [
            'All folds come from one semi-circle flight; contiguous time blocks are not independent flight-level replicates.',
            'Distance trend controls for PCI-specific intercepts but remains exploratory within one flight; it is not a transferable absolute path-loss model.',
            'Pawprints does not provide MAVLink packet loss, RTT, jitter, or C2 goodput; this result is not used to set qdisc.',
        ],
    }
    out = output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    target = out / 'pawprints4g_rsrp_holdout.json'
    target.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input_csv', required=True)
    parser.add_argument('--output_dir', required=True)
    parser.add_argument('--blocks', type=int, default=5)
    args = parser.parse_args()
    print(evaluate(Path(args.input_csv), Path(args.output_dir), args.blocks))

"""Build a provenance-preserving AFAR trajectory library.

Every source trace is retained. Top-level source-directory IDs, transmitter
locations and a predeclared source-group split are recorded so locations from
one source directory do not cross train/holdout. These IDs are pipeline split
groups, not asserted campaign labels. These traces are mobility references, not
validated PX4 missions or calibrated flight-dynamics trajectories.
"""
import argparse
import csv
import hashlib
import json
import logging
import re
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('P4_AFAR_trajectory')
TX_ANCHORS = {
    'loc1': (35.72806709, -78.69730398),
    'loc2': (35.72911779, -78.69918128),
    'loc3': (35.72985129, -78.69711002),
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_trace(path, max_points=50):
    if max_points < 2:
        raise ValueError('max_points must be at least 2')
    rows = []
    with path.open(newline='', encoding='utf-8-sig') as stream:
        for row in csv.reader(stream):
            if len(row) < 4:
                continue
            try:
                lon, lat, alt, timestamp = map(float, row[:4])
            except (ValueError, TypeError):
                continue
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                continue
            rows.append((timestamp, lat, lon, alt))
    rows.sort(key=lambda item: item[0])
    rows = [row for index, row in enumerate(rows) if index == 0 or row[0] > rows[index - 1][0]]
    if len(rows) < 2:
        raise ValueError(f'Need at least two valid timestamped GPS rows in {path}')
    t0, base_alt = rows[0][0], rows[0][3]
    indices = (sorted(set(round(i * (len(rows) - 1) / (max_points - 1)) for i in range(max_points)))
               if len(rows) > max_points else list(range(len(rows))))
    return [{'time_s': rows[i][0] - t0, 'latitude': rows[i][1], 'longitude': rows[i][2],
             'altitude_relative_m': rows[i][3] - base_alt} for i in indices]


def create_mission_library(afar_dir, output_dir, max_points=50, holdout_source_groups=1):
    root, out = Path(afar_dir), Path(output_dir)
    if not root.is_dir():
        raise FileNotFoundError(f'AFAR directory not found: {root}')
    candidates = sorted(path for path in root.rglob('log.csv')
                        if not path.name.startswith('._') and '__MACOSX' not in path.parts)
    parsed = []
    for path in candidates:
        try:
            trace = read_trace(path, max_points=max_points)
            loc = next((part for part in path.parts if re.fullmatch(r'loc[123]', part)), None)
            source_group = next((part for part in path.parts if re.fullmatch(r'\d{3}', part)), None)
            if loc not in TX_ANCHORS or source_group is None:
                raise ValueError('Cannot resolve source directory ID and loc1/loc2/loc3')
            for point in trace:
                point['gcs_latitude'], point['gcs_longitude'] = TX_ANCHORS[loc]
            parsed.append((source_group, loc, path, trace))
        except (OSError, ValueError) as exc:
            LOG.warning('Skipping %s: %s', path, exc)
    if not parsed:
        raise RuntimeError('No valid AFAR trajectory traces were found')

    source_groups = sorted({item[0] for item in parsed})
    if not 0 <= holdout_source_groups < len(source_groups):
        raise ValueError(f'holdout_source_groups must be from 0 to {len(source_groups)-1}')
    holdouts = set(source_groups[-holdout_source_groups:]) if holdout_source_groups else set()
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        'source': 'AFAR 2023 SigMF testbed GPS log.csv',
        'trace_semantics': 'resampled recorded GPS path; mobility reference only, not a validated PX4 mission',
        'altitude_semantics': 'relative to first altitude in source trace; terrain datum and absolute AGL not inferred',
        'split_unit': 'source_group_id (three-digit top-level directory in the local archive); all locations from one group stay in one partition',
        'source_group_semantics': 'Pipeline-defined grouping key; not asserted to be an AFAR campaign identifier.',
        'holdout_source_group_ids': sorted(holdouts),
        'training_source_group_ids': [group for group in source_groups if group not in holdouts],
        'source_group_count': len(source_groups), 'trace_count': len(parsed), 'missions': [],
    }
    for idx, (source_group, loc, source, trace) in enumerate(parsed, start=1):
        mission_id = f'F{idx}_afar_trace_{idx}'
        trace_path = out / f'{mission_id}.csv'
        with trace_path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=trace[0].keys())
            writer.writeheader()
            writer.writerows(trace)
        manifest['missions'].append({
            'mission_id': mission_id, 'source_file': str(source.resolve()), 'source_sha256': sha256(source),
            'trace_file': trace_path.name, 'source_group_id': source_group, 'location_id': loc,
            'partition': 'holdout' if source_group in holdouts else 'train',
            'transmitter_anchor_source': 'AFAR source main.m loc1/loc2/loc3 coordinates',
            'samples': len(trace), 'duration_s': trace[-1]['time_s'],
        })
        LOG.info('Prepared %s (source_group=%s, %s, %d points)', mission_id, source_group,
                 manifest['missions'][-1]['partition'], len(trace))
    (out / 'missions_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--afar_dir', required=True)
    parser.add_argument('--output_dir', required=True)
    parser.add_argument('--max_points', type=int, default=50)
    parser.add_argument('--holdout_source_groups', type=int, default=1)
    args = parser.parse_args()
    create_mission_library(args.afar_dir, args.output_dir, args.max_points, args.holdout_source_groups)

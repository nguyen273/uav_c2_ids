#!/usr/bin/env python3
"""merge_manifests.py
Aggregates per-worker campaign manifests (campaign_worker*_of*.json) into a unified manifest.
Verifies completeness and exits with code 1 if any runs are failed or missing.

Usage:
    python3 merge_manifests.py --campaign_dir ../../data/campaign_results
"""

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("merge_manifests")


def merge_manifests(campaign_dir: Path) -> int:
    manifest_files = sorted(campaign_dir.glob("campaign_worker*_of*.json"))
    if not manifest_files:
        log.error("No worker manifest files found in %s", campaign_dir)
        return 1

    log.info("Found %d worker manifest(s) in %s", len(manifest_files), campaign_dir)

    all_results = {}
    failed_runs = []
    workers_info = []
    num_workers_set = set()

    for mf in manifest_files:
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("Failed to read manifest %s: %s", mf.name, exc)
            return 1

        w_id = data.get("worker_id")
        n_workers = data.get("num_workers")
        status = data.get("status")
        run_results = data.get("run_results", {})
        num_workers_set.add(n_workers)

        workers_info.append({
            "file": mf.name,
            "worker_id": w_id,
            "num_workers": n_workers,
            "status": status,
            "assigned": data.get("runs_assigned_to_this_worker"),
            "completed": sum(1 for s in run_results.values() if s in ("complete", "skipped_complete")),
            "failed": len(data.get("failed_run_ids", [])),
        })

        for run_id, res in run_results.items():
            if run_id in all_results:
                log.warning("Duplicate run_id '%s' found across manifests! Overwriting.", run_id)
            all_results[run_id] = res

        failed_runs.extend(data.get("failed_run_ids", []))

    expected_num_workers = next(iter(num_workers_set)) if len(num_workers_set) == 1 else None
    if len(num_workers_set) > 1:
        log.warning("Mismatched num_workers across manifests: %s", num_workers_set)

    found_worker_ids = {w["worker_id"] for w in workers_info if w["worker_id"] is not None}
    if expected_num_workers and found_worker_ids != set(range(expected_num_workers)):
        missing_ids = set(range(expected_num_workers)) - found_worker_ids
        log.warning("Missing worker IDs: %s (expected 0 to %d)", sorted(missing_ids), expected_num_workers - 1)

    total_runs = len(all_results)
    success_count = sum(1 for s in all_results.values() if s in ("complete", "skipped_complete"))
    fail_count = total_runs - success_count

    merged = {
        "status": "completed" if fail_count == 0 and (expected_num_workers is None or len(found_worker_ids) == expected_num_workers) else "incomplete_or_failed",
        "total_unique_runs": total_runs,
        "successful_runs": success_count,
        "failed_runs_count": fail_count,
        "failed_run_ids": sorted(list(set(failed_runs))),
        "workers": workers_info,
        "all_run_results": all_results,
    }

    out_file = campaign_dir / "campaign_manifest_merged.json"
    out_file.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    log.info("Merged manifest saved to: %s", out_file)

    log.info("================ SUMMARY ================")
    log.info("Total unique runs found : %d", total_runs)
    log.info("Successful runs         : %d", success_count)
    log.info("Failed runs             : %d", fail_count)
    for w in workers_info:
        log.info("  Worker %s/%s: %d completed, %d failed (status: %s)",
                 w["worker_id"], w["num_workers"], w["completed"], w["failed"], w["status"])

    if fail_count > 0:
        log.error("Campaign has %d failed run(s): %s", fail_count, merged["failed_run_ids"])
        return 1

    log.info("All runs verified successfully!")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Merge worker manifests and verify campaign completeness")
    parser.add_argument("--campaign_dir", type=Path, required=True,
                        help="Directory containing campaign run folders and worker manifests")
    args = parser.parse_args()

    sys.exit(merge_manifests(args.campaign_dir))


if __name__ == "__main__":
    main()

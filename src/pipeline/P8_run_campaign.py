"""Campaign orchestrator for the PX4 SITL cyber-physical twin.

Resilience features:
- Resume: already-completed run directories are skipped automatically.
- Soft-fail: a failed run is logged as 'failed_p7' and the campaign
  continues rather than aborting (fail-open per run, fail-closed per
  artifact validation is still enforced post-completion).
- Worker sharding: --worker_id / --num_workers slice the run matrix
  by index modulo so multiple VMs can work in parallel without
  coordination beyond a shared output directory.
- Auto-sync: set SYNC_TARGET=user@host:/path to rsync each completed
  run directory to a remote host immediately after it finishes.
"""
import argparse
import csv
import itertools
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("P8_Campaign")


def _sync_run(run_dir: Path, run_id: str) -> None:
    """Rsync a completed run directory to SYNC_TARGET if the env var is set.

    SYNC_TARGET format: ``user@host:/absolute/path`` or any rsync-compatible
    destination.  Failures are logged as warnings and do not abort the campaign.
    """
    sync_target = os.environ.get("SYNC_TARGET", "").strip()
    if not sync_target:
        return
    dest = f"{sync_target.rstrip('/')}/{run_id}/"
    cmd = ["rsync", "-avz", "--no-relative", str(run_dir) + "/", dest]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            log.info("Synced %s → %s", run_id, dest)
        else:
            log.warning("Sync failed for %s (rc=%d): %s", run_id, result.returncode, result.stderr.strip())
    except Exception as exc:  # noqa: BLE001
        log.warning("Sync error for %s: %s", run_id, exc)
    finally:
        for log_name in ("px4.log", "gz.log"):
            p = run_dir / log_name
            if p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass


def _sync_file(file_path: Path) -> None:
    sync_target = os.environ.get("SYNC_TARGET", "").strip()
    if not sync_target:
        return
    dest = f"{sync_target.rstrip('/')}/{file_path.name}"
    cmd = ["rsync", "-avz", str(file_path), dest]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        log.warning("Sync error for %s: %s", file_path.name, exc)


# Names of artifacts written by P7 for each run.
_REQUIRED_ARTIFACTS = ("c2_traffic.pcap", "flight_telemetry.csv", "attack_events.jsonl", "twin_scope.json")
# Subset that must be non-empty (attack_events.jsonl is allowed to be empty for benign runs).
_NON_EMPTY_ARTIFACTS = frozenset({"c2_traffic.pcap", "flight_telemetry.csv", "twin_scope.json"})


def _validate_run_quality(run_dir: Path, attack_id: str = "") -> tuple[bool, list[str], dict]:
    """Strictly validates telemetry continuity, clock skew, PCAP packet count and attack bounding."""
    issues = []
    stats = {}

    # 1. Telemetry validation
    telem_p = run_dir / "flight_telemetry.csv"
    if not telem_p.is_file() or telem_p.stat().st_size < 10 * 1024:
        issues.append("flight_telemetry.csv missing or too small (<10KB)")
    else:
        try:
            with open(telem_p, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if len(rows) < 500:
                issues.append(f"Telemetry rows too few: {len(rows)} < 500")
            else:
                t0_epoch = float(rows[0]["timestamp_s"])
                t1_epoch = float(rows[-1]["timestamp_s"])
                elapsed = float(rows[-1]["elapsed_s"])
                wp_cur = int(rows[-1]["mission_current"])
                wp_tot = int(rows[-1]["mission_total"])
                clock_skew = abs((t1_epoch - t0_epoch) - elapsed)
                stats["telem_rows"] = len(rows)
                stats["elapsed_s"] = elapsed
                stats["clock_skew_s"] = clock_skew
                stats["wp_end"] = f"{wp_cur}/{wp_tot}"

                if clock_skew > 5.0:
                    issues.append(f"Lệch đồng hồ epoch vs elapsed: {clock_skew:.1f}s > 5.0s (CLOCK JUMP DETECTED)")
                if wp_cur < wp_tot - 2:
                    issues.append(f"Chưa bay hết waypoint: dừng ở WP {wp_cur}/{wp_tot}")
        except Exception as e:
            issues.append(f"Lỗi đọc telemetry: {e}")

    # 2. PCAP validation
    pcap_p = run_dir / "c2_traffic.pcap"
    if not pcap_p.is_file() or pcap_p.stat().st_size < 100 * 1024:
        issues.append("c2_traffic.pcap missing or too small (<100KB)")
    else:
        try:
            pcap_sz_mb = pcap_p.stat().st_size / (1024 * 1024)
            stats["pcap_mb"] = round(pcap_sz_mb, 2)
            with open(pcap_p, "rb") as f:
                ghdr = f.read(24)
                if len(ghdr) >= 24:
                    magic = struct.unpack("I", ghdr[:4])[0]
                    endian = "<" if magic in (0xa1b2c3d4, 0xa1b23c4d) else ">"
                    pkts = 0
                    first_ts = None
                    last_ts = None
                    max_gap = 0.0
                    prev_ts = None
                    while True:
                        phdr = f.read(16)
                        if len(phdr) < 16:
                            break
                        sec, usec, incl_len, orig_len = struct.unpack(f"{endian}IIII", phdr)
                        f.seek(incl_len, 1)
                        ts = sec + usec / 1e6
                        if first_ts is None:
                            first_ts = ts
                        if prev_ts is not None:
                            gap = ts - prev_ts
                            if gap > max_gap:
                                max_gap = gap
                        prev_ts = ts
                        last_ts = ts
                        pkts += 1
                    stats["pcap_pkts"] = pkts
                    stats["max_gap_s"] = round(max_gap, 2)
                    if pkts < 3000:
                        issues.append(f"Số gói tin PCAP quá ít: {pkts} < 3000")
                    if max_gap > 10.0:
                        issues.append(f"Khoảng trống PCAP quá lớn: {max_gap:.1f}s > 10.0s")
        except Exception as e:
            issues.append(f"Lỗi phân tích PCAP: {e}")

    # 3. Attack events validation
    atk_p = run_dir / "attack_events.jsonl"
    if attack_id == "T4_DoS":
        if not atk_p.is_file() or atk_p.stat().st_size == 0:
            issues.append("Run T4_DoS nhưng attack_events.jsonl bị thiếu hoặc rỗng")
        else:
            try:
                events = [json.loads(l) for l in atk_p.read_text(encoding="utf-8").splitlines() if l.strip()]
                if not events:
                    issues.append("Run T4_DoS nhưng không có sự kiện tấn công nào trong JSONL")
                else:
                    ev = events[0]
                    s = ev.get("start_epoch_s")
                    e = ev.get("end_epoch_s")
                    if s and e:
                        dur = e - s
                        stats["attack_dur_s"] = round(dur, 2)
                        if dur > 70.0:
                            issues.append(f"Thời lượng DoS bị kéo dài: {dur:.1f}s > 70s (phải giới hạn ~50-55s)")
                        elif dur < 25.0:
                            issues.append(f"Thời lượng DoS quá ngắn: {dur:.1f}s < 25s")
                    else:
                        issues.append("Sự kiện tấn công thiếu start_epoch_s hoặc end_epoch_s")
            except Exception as e:
                issues.append(f"Lỗi kiểm tra attack_events: {e}")

    is_valid = len(issues) == 0
    return is_valid, issues, stats


def _print_quality_audit_card(run_id: str, is_valid: bool, issues: list[str], stats: dict):
    bar = "=" * 76
    status_str = "✓ ĐẠT CHUẨN HOÀN HẢO (PASS)" if is_valid else "✖ KHÔNG ĐẠT CHUẨN (FAILED)"
    print(f"\n{bar}")
    print(f"  [QUALITY GATE AUDIT] Run: {run_id}")
    print(f"  Kết quả kiểm tra: {status_str}")
    print(f"  • Telemetry: {stats.get('telem_rows', 0):,} dòng, {stats.get('elapsed_s', 0):.1f}s | Lệch đồng hồ: {stats.get('clock_skew_s', 0):.2f}s | WP: {stats.get('wp_end', 'N/A')}")
    print(f"  • PCAP:      {stats.get('pcap_mb', 0)} MB ({stats.get('pcap_pkts', 0):,} gói tin) | Khoảng trống lớn nhất: {stats.get('max_gap_s', 0)}s")
    if "attack_dur_s" in stats:
        print(f"  • Attack:    T4_DoS kết thúc trong {stats.get('attack_dur_s')}s (Chuẩn: ~50-55s)")
    if issues:
        print("  CÁC LỖI PHÁT HIỆN ĐƯỢC:")
        for iss in issues:
            print(f"    [!] {iss}")
    print(f"{bar}\n")


def _artifacts_complete(run_dir: Path, attack_id: str = "") -> bool:
    """Return True when all required artifacts exist and strictly meet quality thresholds."""
    for name in _REQUIRED_ARTIFACTS:
        p = run_dir / name
        if not p.is_file():
            return False
        if name in _NON_EMPTY_ARTIFACTS and p.stat().st_size == 0:
            return False

    is_valid, issues, stats = _validate_run_quality(run_dir, attack_id)
    _print_quality_audit_card(run_dir.name, is_valid, issues, stats)
    return is_valid


def run_single_experiment(run_id, mission, channel, traffic, frequency_ghz, seed, output_dir, base_dir,
                          mission_metadata, mobility_fit_path, mobility_speed_m_s):
    """Execute one SITL run.  Returns a status string for the campaign manifest.

    Possible return values:
      ``'skipped_complete'``          – run already done and artifacts intact.
      ``'failed_p7'``                 – P7 subprocess failed.
      ``'failed_artifact_validation'``– P7 exited 0 but required files missing.
      ``'complete'``                  – run succeeded.
    """
    run_dir = (Path(output_dir) / run_id).resolve()
    meta_path = run_dir / "metadata.json"

    # --- Resume / checkpoint: skip only when complete AND artifacts verified ---
    sync_target = os.environ.get("SYNC_TARGET", "").strip()
    force_runs = [r.strip() for r in os.environ.get("FORCE_RUNS", "").split(",") if r.strip()]
    is_forced = any(f in run_id for f in force_runs)

    if not is_forced and sync_target and sync_target.startswith("/") and Path(sync_target).is_dir():
        dest_meta = Path(sync_target) / run_id / "metadata.json"
        if not dest_meta.is_file():
            log.info("Run %s missing from SYNC_TARGET (%s); re-running to complete campaign", run_id, sync_target)
            is_forced = True

    if run_dir.exists() and meta_path.is_file() and not is_forced:
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
            if existing.get("status") == "complete":
                if _artifacts_complete(run_dir, traffic):
                    log.info("Skipping already-completed run (artifacts verified): %s", run_id)
                    return "skipped_complete"
                log.warning(
                    "Run %s is marked complete but artifacts are missing or empty; re-running",
                    run_id,
                )
            else:
                log.info("Re-attempting previously uncompleted run (%s): %s", existing.get("status"), run_id)
        except Exception:  # noqa: BLE001
            log.warning("Unreadable metadata for %s; re-running", run_id)

    # Clean leftover or dirty artifacts before executing
    if run_dir.exists():
        for f in run_dir.glob("*"):
            try:
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    shutil.rmtree(f)
            except Exception:
                pass

    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {"run_id": run_id, "mission_id": mission, "channel_id": channel,
                "attack_id": traffic, "reference_dataset_carrier_ghz": frequency_ghz,
                "simulated_rf_carrier_ghz": None, "seed": seed,
                "status": "running", "data_scope": "PX4 SITL + Gazebo + MAVSDK/MAVLink software cyber-physical twin; RF uncalibrated",
                "emulation_scope": "PX4/Gazebo flight dynamics and live MAVLink mission over Containernet/TCLink with declared Layer-3 stress profile",
                "layer1_mode": "dataset32_empirical_path_loss_distance_slope_not_absolute_power_calibration",
                "layer1_artifact": "layer1_dataset32_pathloss_fit.json",
                "layer1_dataset": "AERPAW Dataset-32; Dryad DOI 10.5061/dryad.2ngf1vj4r",
                "mobility_source": "AFAR recorded GPS trace",
                "mobility_source_group_id": mission_metadata["source_group_id"],
                "mobility_partition": mission_metadata["partition"],
                "mobility_altitude_offset_m": 15.0,
                "mobility_model_artifact": str(mobility_fit_path.resolve()),
                "mobility_model_speed_m_s": mobility_speed_m_s,
                "mobility_model_parameters": mission_metadata.get("mobility_model_parameters"),
                "software_versions": {"python": sys.version.split()[0]}}
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    p7 = Path(__file__).parent / "P7_sitl_c2_twin.py"
    cmd = [sys.executable, str(p7), "--mission", mission, "--channel", channel,
           "--traffic", traffic, "--seed", str(seed), "--frequency_ghz", str(frequency_ghz),
           "--output_dir", str(run_dir), "--base_dir", str(Path(base_dir).resolve())]

    # --- Soft-fail with retry: allow 1 retry on transient startup glitch ---
    p7_success = False
    for attempt in range(2):
        try:
            subprocess.run(cmd, check=True)
            p7_success = True
            break
        except Exception as exc:
            subprocess.run(["docker", "rm", "-f", "mn.gcs", "mn.uav", "mn.attacker"], capture_output=True)
            subprocess.run(["mn", "-c"], capture_output=True)
            if attempt == 0:
                log.warning("Run %s transient failure on attempt 1 (retrying once): %s", run_id, exc)
                time.sleep(3)
                continue
            metadata.update(status="failed_p7", error=str(exc))
            meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            log.warning("Run %s failed in P7 after retry (will continue campaign): %s", run_id, exc)
            return "failed_p7"

    if not _artifacts_complete(run_dir, traffic):
        metadata.update(status="failed_artifact_validation")
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        log.warning("Run %s artifact validation failed (artifacts missing, empty, or truncated)", run_id)
        return "failed_artifact_validation"

    metadata.update(status="complete", artifacts=list(_REQUIRED_ARTIFACTS))
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    log.info("Completed %s", run_id)

    # --- Auto-sync to host machine if SYNC_TARGET is configured ---
    _sync_run(run_dir, run_id)
    return "complete"


def _resolve_frequencies(base_dir, frequencies):
    fit_path = Path(base_dir) / "data" / "model_fits" / "layer1_dataset32_pathloss_fit.json"
    if not fit_path.is_file():
        raise FileNotFoundError(f"Missing Dataset-32 Layer-1 fit: {fit_path}; run P2_prepare_dataset32.py and P3_layer1_dataset32.py")
    fit = json.loads(fit_path.read_text(encoding="utf-8"))
    if fit.get("calibration_status") != "dataset32_empirical_path_loss_distance_slope_flight_grouped_holdout":
        raise ValueError("P8 requires the current Dataset-32 flight-grouped Layer-1 artifact")
    supported = [float(fit["frequency_ghz"])]
    selected = supported if frequencies is None else [float(f) for f in frequencies]
    invalid = [f for f in selected if min(abs(f - s) for s in supported) >= 0.005]
    if invalid:
        raise ValueError(f"Requested carrier(s) {invalid} outside measured Layer-1 fit support {supported}; no frequency extrapolation allowed")
    return selected


def _resolve_mobility_speed(base_dir, manifest):
    path = Path(base_dir) / "data" / "model_fits" / "layer2_afar_mobility_calibration.json"
    if not path.is_file():
        raise FileNotFoundError(f"Missing Layer-2 mobility fit: {path}; run P14_calibrate_layer2_mobility.py")
    model = json.loads(path.read_text(encoding="utf-8"))
    if model.get("calibration_status") != "afar_train_source_group_mobility_profile_with_heldout_distribution_evaluation":
        raise ValueError("Unsupported Layer-2 AFAR calibration artifact")
    if sorted(map(str, manifest.get("training_source_group_ids", []))) != sorted(model.get("training_source_group_ids", [])):
        raise ValueError("Layer-2 fit training source groups do not match the current AFAR manifest")
    speed = float(model.get("mission_speed_m_s", float("nan")))
    if not 0.5 <= speed <= 20.0:
        raise ValueError(f"Layer-2 fitted mission speed {speed} m/s is outside the supported [0.5, 20] range")
    parameters = model.get("gauss_markov_speed_model")
    if not isinstance(parameters, dict):
        raise ValueError("Layer-2 mobility fit is missing its Gauss-Markov speed parameters")
    return path, speed, parameters


def run_campaign(output_dir, base_dir, repetitions=5, missions=None, channels=None, traffics=None,
                 frequencies=None, partition="train", worker_id=0, num_workers=1):
    """Run (or resume) the SITL campaign, optionally as one shard of a parallel multi-VM job.

    Args:
        worker_id:   Zero-based index of this worker (VM).  Runs whose global
                     index modulo ``num_workers`` equals ``worker_id`` are
                     assigned to this worker.  Each worker_id must be launched
                     exactly once; a duplicate worker_id in the same output_dir
                     will overwrite the per-worker manifest and race on the same
                     run directories without any locking.
        num_workers: Total number of parallel workers.  Set to 1 (default) for
                     single-VM execution.  All workers must use **identical**
                     mission/channel/traffic/frequency/repetitions arguments so
                     the global run list is partitioned consistently.

    Returns:
        List of run_ids that failed or errored (empty list = full success).
    """
    import platform

    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    if num_workers < 1:
        raise ValueError("num_workers must be >= 1")
    if not (0 <= worker_id < num_workers):
        raise ValueError(f"worker_id must be in [0, num_workers-1]; got {worker_id} with num_workers={num_workers}")

    manifest_path = Path(base_dir) / "data" / "missions" / "missions_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing AFAR split manifest: {manifest_path}; run P4_layer2_mobility.py first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mission_index = {m["mission_id"]: m for m in manifest.get("missions", [])}
    if partition not in {"train", "holdout", "all"}:
        raise ValueError("partition must be train, holdout, or all")
    if missions is None:
        if partition == "train":
            # Preserve the predeclared compact training matrix across three
            # distinct source groups; do not silently multiply default runs.
            preferred = ["F1_afar_trace_1", "F4_afar_trace_4", "F7_afar_trace_7"]
            missions = [m for m in preferred if m in mission_index
                        and mission_index[m]["partition"] == "train"]
        else:
            missions = [m["mission_id"] for m in manifest["missions"]
                        if partition == "all" or m["partition"] == partition]
    if not missions:
        raise ValueError(f"No missions available for AFAR partition {partition!r}")
    unknown = set(missions) - set(mission_index)
    if unknown:
        raise ValueError(f"Unknown mission IDs not present in AFAR manifest: {sorted(unknown)}")
    mismatched = [m for m in missions if partition != "all" and mission_index[m]["partition"] != partition]
    if mismatched:
        raise ValueError(f"Mission(s) {mismatched} violate requested {partition!r} partition; use --partition all only for an explicitly mixed analysis")
    channels = channels or ["C1_LOS", "C2_Moderate", "C3_NLOS", "C4_Dynamic"]
    # Replay and FDI are excluded until implemented as packet-level MAVLink attacks.
    traffics = traffics or ["T0_Benign", "T4_DoS"]
    frequencies = _resolve_frequencies(base_dir, frequencies)
    mobility_fit_path, mobility_speed_m_s, mobility_parameters = _resolve_mobility_speed(base_dir, manifest)
    for mission_metadata in mission_index.values():
        mission_metadata["mobility_model_parameters"] = mobility_parameters

    # Build the full ordered run list, then slice by worker shard.
    all_combos = list(itertools.product(
        missions, channels, traffics, frequencies, range(1, repetitions + 1)
    ))
    total = len(all_combos)
    my_combos = [(i, combo) for i, combo in enumerate(all_combos) if i % num_workers == worker_id]

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Per-worker campaign manifest — written before the first run and updated
    # after every run.  If a duplicate worker_id is launched against the same
    # output_dir, this file will be overwritten, which makes the conflict
    # visible in the output directory.
    host_info = {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
    }
    manifest_out = out_dir / f"campaign_worker{worker_id}_of{num_workers}.json"
    existing_results = {}
    if manifest_out.is_file():
        try:
            prev = json.loads(manifest_out.read_text(encoding="utf-8"))
            existing_results = prev.get("run_results", {})
        except Exception:
            pass

    worker_manifest: dict = {
        "worker_id": worker_id,
        "num_workers": num_workers,
        "total_runs_in_matrix": total,
        "runs_assigned_to_this_worker": len(my_combos),
        "partition": partition,
        "missions": missions,
        "channels": channels,
        "traffics": traffics,
        "frequencies": frequencies,
        "repetitions": repetitions,
        "host_info": host_info,
        "status": "running",
        "run_results": existing_results,
        "failed_run_ids": [],
        "note": (
            "All workers must use identical mission/channel/traffic/frequency/repetitions "
            "arguments. Each worker_id must be launched exactly once. Duplicate worker_ids "
            "or mismatched arguments produce overlapping or incomplete run coverage and are "
            "not detected automatically."
        ),
    }
    manifest_out.write_text(json.dumps(worker_manifest, indent=2), encoding="utf-8")
    _sync_file(manifest_out)

    if num_workers > 1:
        log.info(
            "Worker %d/%d on host '%s': assigned %d/%d runs",
            worker_id, num_workers, host_info["hostname"], len(my_combos), total,
        )
        log.warning(
            "Multi-worker mode: ensure all %d workers use IDENTICAL campaign arguments "
            "and each worker_id is launched exactly once. Resource contention "
            "(CPU/RAM/disk) from multiple SITL/Gazebo instances on the same host can "
            "affect run stability and timing; prefer one worker per physical VM. "
            "Record worker count and host config when reporting results.",
            num_workers,
        )
    else:
        log.info("SITL campaign: %d runs (single worker, host '%s')", total, host_info["hostname"])

    failed_runs: list = []
    for global_index, (mission, channel, traffic, freq, seed) in my_combos:
        # Run IDs use the global 1-based index so they are stable and unique
        # across all workers regardless of how many VMs are in use.
        run_id = f"run_{global_index + 1:04d}_{mission}_{channel}_{traffic}_ref{int(freq * 1000)}_s{seed}"
        result_status: str
        try:
            result_status = run_single_experiment(
                run_id, mission, channel, traffic, freq, seed,
                output_dir, base_dir,
                mission_index[mission], mobility_fit_path, mobility_speed_m_s,
            ) or "complete"
        except Exception as exc:  # noqa: BLE001  (unexpected: OOM, import error, etc.)
            log.error("Unexpected error in run %s: %s", run_id, exc, exc_info=True)
            result_status = "error_unexpected"

        worker_manifest["run_results"][run_id] = result_status
        if result_status not in ("complete", "skipped_complete"):
            failed_runs.append(run_id)
        # Flush manifest after every run so progress is visible even if the
        # process is killed mid-campaign.
        worker_manifest["failed_run_ids"] = failed_runs
        manifest_out.write_text(json.dumps(worker_manifest, indent=2), encoding="utf-8")
        _sync_file(manifest_out)

    final_status = "failed" if failed_runs else "complete"
    worker_manifest.update(status=final_status, failed_run_count=len(failed_runs),
                           failed_run_ids=failed_runs)
    manifest_out.write_text(json.dumps(worker_manifest, indent=2), encoding="utf-8")
    _sync_file(manifest_out)

    if failed_runs:
        log.warning(
            "Worker %d/%d finished with %d failed run(s). "
            "See %s for details.",
            worker_id, num_workers, len(failed_runs), manifest_out.name,
        )
    else:
        log.info(
            "Worker %d/%d completed successfully (%d runs).",
            worker_id, num_workers, len(my_combos),
        )

    return failed_runs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Campaign orchestrator for the PX4 SITL cyber-physical twin.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Parallel VM example (3 VMs sharing an NFS/Samba/VMware Shared Folder output directory):
  VM 1:  sudo -E python3 P8_run_campaign.py --worker_id 0 --num_workers 3 --output_dir /shared/results
  VM 2:  sudo -E python3 P8_run_campaign.py --worker_id 1 --num_workers 3 --output_dir /shared/results
  VM 3:  sudo -E python3 P8_run_campaign.py --worker_id 2 --num_workers 3 --output_dir /shared/results

IMPORTANT: All workers must use identical --mission/--channel/--traffic/--frequency_ghz/
--repetitions arguments. Each --worker_id must be launched exactly once. A per-worker
manifest (campaign_workerN_ofM.json) is written to --output_dir for audit. Duplicate
worker_ids are NOT detected automatically.

Auto-sync to host machine after each completed run:
  export SYNC_TARGET=user@192.168.x.x:/path/to/host/results
  sudo -E python3 P8_run_campaign.py ...
"""
    )
    parser.add_argument("--output_dir", default="../data/campaign_results",
                        help="Directory for run outputs; use a shared folder for multi-VM mode")
    parser.add_argument("--base_dir", default=None)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--mission", action="append", dest="missions")
    parser.add_argument("--channel", action="append", dest="channels")
    parser.add_argument("--traffic", action="append", dest="traffics")
    parser.add_argument("--frequency_ghz", type=float, action="append", dest="frequencies")
    parser.add_argument("--partition", choices=["train", "holdout", "all"], default="train",
                        help="Select AFAR source-group partition; explicit mission IDs must match it")
    parser.add_argument("--worker_id", type=int, default=0,
                        help="Zero-based index of this worker VM (default: 0)")
    parser.add_argument("--num_workers", type=int, default=1,
                        help="Total number of parallel worker VMs (default: 1 = single machine)")
    parser.add_argument("--sync_target", default=None,
                        help="Sync destination (rsync/local path) for auto-sync after each run")
    args = parser.parse_args()
    if args.sync_target:
        os.environ["SYNC_TARGET"] = args.sync_target
    root = Path(args.base_dir).resolve() if args.base_dir else Path(__file__).parent.parent.parent.resolve()
    failed = run_campaign(
        args.output_dir, root, args.repetitions,
        args.missions, args.channels, args.traffics, args.frequencies,
        args.partition, args.worker_id, args.num_workers,
    )
    if failed:
        log.error(
            "Campaign exiting with errors: %d failed run(s). "
            "Check campaign_worker%d_of%d.json in --output_dir. "
            "P9/P10 will skip runs not marked 'complete'.",
            len(failed), args.worker_id, args.num_workers,
        )
        sys.exit(1)

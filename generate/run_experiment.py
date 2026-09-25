#!/usr/bin/env python3
"""Resumable PX4 SITL data collector. Run inside the WSL distro with PX4 installed."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from shutil import which

import yaml
from mavsdk import System

ROOT = Path(__file__).resolve().parents[1]
CURRENT_DIR = Path(__file__).resolve().parent
for p in [ROOT, CURRENT_DIR]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    from generate.collectors.gazebo_ground_truth import collect_topic
    from generate.collectors.mavsdk_collector import collect_telemetry, wait_for_connection
    from generate.collectors.packet_capture import start_capture, stop_process_group
    from generate.attacks.base import AttackContext
    from generate.attacks.manager import AttackManager
    from generate.attacks.proxy import LoopbackMavlinkProxy
    from generate.attacks.registry import get_controller_factory, validate_controller
    from generate.missions.mission_runner import execute_mission
except ImportError:
    from collectors.gazebo_ground_truth import collect_topic
    from collectors.mavsdk_collector import collect_telemetry, wait_for_connection
    from collectors.packet_capture import start_capture, stop_process_group
    from attacks.base import AttackContext
    from attacks.manager import AttackManager
    from attacks.proxy import LoopbackMavlinkProxy
    from attacks.registry import get_controller_factory, validate_controller
    from missions.mission_runner import execute_mission

_local_cfg = CURRENT_DIR / "config" / "scenario.yaml"
DEFAULT_CONFIG = _local_cfg if _local_cfg.exists() else ROOT / "config" / "scenario.yaml"
DATA_ROOT = ROOT / "data" / "raw" if (ROOT / "data").exists() else CURRENT_DIR / "data" / "raw"
MANIFEST = DATA_ROOT / "manifest.csv"
FIELDS = [
    "run_id", "scenario_id", "seed", "attack_type", "attack_variant", "impact",
    "severity", "status",
    "started_at", "completed_at", "attack_start", "attack_end", "attack_phase",
    "capture_vantage", "capture_filter", "capture_interface", "ground_truth_path",
    "pcap_path", "telemetry_path", "event_path", "error"
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_manifest() -> dict[str, dict[str, str]]:
    if not MANIFEST.exists():
        return {}
    with MANIFEST.open(newline="", encoding="utf-8") as f:
        return {r["run_id"]: r for r in csv.DictReader(f)}


def write_manifest(rows: dict[str, dict[str, str]]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows[k] for k in sorted(rows))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, MANIFEST)


def build_jobs(cfg: dict, benign_only: bool) -> list[dict]:
    jobs = []
    for scenario in cfg["campaign"]["missions"]:
        for seed in cfg["campaign"]["benign_seeds"]:
            jobs.append(dict(scenario_id=scenario, seed=seed, attack_type="BENIGN", severity="NONE"))
    if not benign_only:
        for attack in cfg["campaign"]["attack_types"]:
            for scenario in cfg["campaign"]["missions"]:
                for severity in cfg["campaign"]["severities"]:
                    for seed in cfg["campaign"]["attack_seeds"]:
                        variants = cfg["campaign"].get("attack_variants", {}).get(attack, [attack])
                        for variant in variants:
                            impact = {
                                "GCS_STATE_SPOOF": "FAKELANDING_GCS_STATE_SPOOF",
                                "GCS_POSITION_DRIFT": "GCS_POSITION_INTEGRITY_LOSS",
                            }.get(variant, "")
                            jobs.append(dict(scenario_id=scenario, seed=seed, attack_type=attack,
                                             attack_variant=variant, impact=impact, severity=severity))
    for index, job in enumerate(jobs, 1):
        job["run_id"] = f"RUN_{index:05d}"
    return jobs


async def run_one(job: dict, cfg: dict, px4_dir: Path, start_command: str) -> None:
    # 1. Seed pseudo-random generator for reproducibility
    seed = int(job.get("seed", 1000))
    random.seed(seed)
    env = os.environ.copy()
    env["PX4_SIM_SEED"] = str(seed)

    run_dir = DATA_ROOT / job["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    capture_binary = "dumpcap" if which("dumpcap") else "tcpdump"
    paths = {
        "pcap_path": run_dir / ("c2.pcapng" if capture_binary == "dumpcap" else "c2.pcap"),
        "telemetry_path": run_dir / "telemetry.jsonl",
        "event_path": run_dir / "events.jsonl",
        "ground_truth_path": run_dir / "gazebo_ground_truth.jsonl",
    }
    # Archive stale partial files from prior failed/incomplete attempts
    for path in paths.values():
        if path.exists():
            attempts_dir = run_dir / "attempts"
            attempts_dir.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            try:
                path.rename(attempts_dir / f"{path.stem}_{stamp}{path.suffix}")
            except Exception:
                try:
                    path.unlink()
                except Exception:
                    pass
    stop = asyncio.Event()
    px4 = capture = None
    proxy = None
    collector = ground_truth_collector = None
    attack_manager = None
    telemetry_cache = {}
    ground_truth_stop = asyncio.Event()

    row = {k: str(v) for k, v in job.items()}
    row.update(
        status="running", started_at=utc_now(), completed_at="",
        attack_start="", attack_end="", attack_phase="", error="",
        attack_variant=str(job.get("attack_variant", "")),
        impact=str(job.get("impact", "")),
        capture_vantage=str(cfg["campaign"].get("capture", {}).get("vantage", "gcs_side")),
        capture_filter="", capture_interface="",
        **{k: str(v) for k, v in paths.items()}
    )
    rows = read_manifest()
    rows[job["run_id"]] = row
    write_manifest(rows)

    try:
        if job.get("attack_type") != "BENIGN":
            if not cfg["campaign"].get("attack_policy", {}).get("ground_truth_topic"):
                raise RuntimeError("Attack runs require an independently configured Gazebo ground-truth topic")
        # Start PX4 SITL
        px4 = subprocess.Popen(start_command, cwd=px4_dir, shell=True, env=env, start_new_session=True)
        await asyncio.sleep(4)

        # Start MAVLink relay proxy if this attack run uses it
        proxy_cfg = cfg["campaign"].get("attack_policy", {}).get("proxy", {})
        is_proxy_run = job.get("attack_type") in proxy_cfg.get("enabled_for", [])
        if is_proxy_run:
            proxy = LoopbackMavlinkProxy(
                gcs_port=int(proxy_cfg.get("gcs_bind_port", 14545)),
                px4_port=int(proxy_cfg.get("px4_port", 14540)),
                host=str(proxy_cfg.get("gcs_bind_host", "127.0.0.1")),
                px4_host=str(proxy_cfg.get("px4_host", "127.0.0.1")),
                px4_target_port=int(proxy_cfg.get("px4_target_port", 14580)),
                gcs_relay_port=int(proxy_cfg.get("gcs_relay_port", 14546)),
                seed=seed,
            )
            await proxy.start()

        # Connect MAVSDK and wait until vehicle is ready
        drone = System()
        if is_proxy_run:
            mav_addr = f"udp://:{int(proxy_cfg.get('gcs_bind_port', 14545))}"
        else:
            mav_addr = cfg["campaign"]["mavsdk_connection"]
            if mav_addr.startswith("udpin://"):
                mav_addr = "udp://" + mav_addr[len("udpin://"):]
        await asyncio.wait_for(drone.connect(system_address=mav_addr), timeout=15.0)
        await wait_for_connection(drone, int(cfg["campaign"]["sitl_ready_timeout_s"]))

        # Set stream frequencies
        rate_hz = float(cfg["campaign"]["telemetry_rate_hz"])
        await drone.telemetry.set_rate_position(rate_hz)
        await drone.telemetry.set_rate_velocity_ned(rate_hz)
        await drone.telemetry.set_rate_attitude_euler(rate_hz)
        await drone.telemetry.set_rate_battery(rate_hz)
        await drone.telemetry.set_rate_in_air(rate_hz)

        # Configure the single capture vantage before starting collectors.
        capture_cfg = cfg["campaign"].get("capture", {})
        capture_filter = capture_cfg.get("filter", cfg["campaign"].get("capture_filter", "udp"))
        if capture_cfg.get("endpoint_filter", False):
            campaign = cfg["campaign"]
            is_proxy_run = job.get("attack_type") in campaign.get("attack_policy", {}).get("proxy", {}).get("enabled_for", [])
            endpoint_port = (campaign["attack_policy"]["proxy"]["gcs_bind_port"] if is_proxy_run
                             else campaign.get("capture", {}).get("direct_gcs_port", 14540))
            capture_filter = f"udp and port {int(endpoint_port)}"

        ground_truth_topic = cfg["campaign"].get("attack_policy", {}).get("ground_truth_topic", "")
        if ground_truth_topic:
            ground_truth_first_sample = asyncio.Event()
            ground_truth_collector = asyncio.create_task(collect_topic(
                ground_truth_topic, paths["ground_truth_path"], ground_truth_stop,
                first_sample=ground_truth_first_sample))
            try:
                await asyncio.wait_for(ground_truth_first_sample.wait(), timeout=15.0)
            except asyncio.TimeoutError as exc:
                if ground_truth_collector.done():
                    await ground_truth_collector
                raise RuntimeError("Gazebo ground-truth topic produced no sample before mission start") from exc
        else:
            row["ground_truth_path"] = ""

        # Warm up every telemetry stream before opening the output file, then
        # start telemetry sampling immediately after packet capture begins.
        telemetry_ready = asyncio.Event()
        telemetry_start = asyncio.Event()
        collector = asyncio.create_task(collect_telemetry(
            drone, paths["telemetry_path"], stop, rate_hz, telemetry_cache,
            ready=telemetry_ready, start=telemetry_start))
        try:
            await asyncio.wait_for(telemetry_ready.wait(), timeout=6.0)
        except asyncio.TimeoutError as exc:
            if collector.done():
                await collector
            raise RuntimeError("MAVSDK telemetry did not warm up before mission start") from exc

        capture = start_capture(paths["pcap_path"], capture_filter,
                                capture_cfg.get("interface", "any"))
        row["capture_filter"] = capture_filter
        row["capture_interface"] = capture_cfg.get("interface", "any")
        rows = read_manifest()
        rows[job["run_id"]] = row
        write_manifest(rows)
        telemetry_start.set()

        # Execute mission and log events
        with paths["event_path"].open("a", encoding="utf-8", buffering=1) as events:
            events.write(json.dumps({"timestamp_epoch_ns": time.time_ns(), "event": "run_start", **job}) + "\n")
            mission_started = time.monotonic()

            if job.get("attack_type") != "BENIGN":
                variant = job.get("attack_variant", job["attack_type"])

                async def emit_attack_event(name: str, **values):
                    timestamp_ns = time.time_ns()
                    line = json.dumps({"timestamp_epoch_ns": timestamp_ns,
                                       "event": name, **values}) + "\n"
                    if not events.closed:
                        events.write(line)
                        events.flush()
                    else:
                        with paths["event_path"].open("a", encoding="utf-8") as f:
                            f.write(line)
                    timestamp = datetime.fromtimestamp(timestamp_ns / 1e9, timezone.utc).isoformat()
                    if name == "attack_started":
                        row["attack_start"] = timestamp
                        row["attack_phase"] = str(values.get("trigger_event", ""))
                        if values.get("trigger_occurrence") is not None:
                            row["attack_phase"] += f"#{values['trigger_occurrence']}"
                        phase_location = [f"{key}={values[key]}" for key in ("loop", "waypoint") if key in values]
                        if phase_location:
                            row["attack_phase"] += ":" + ",".join(phase_location)
                    elif name == "attack_stopped":
                        row["attack_end"] = timestamp
                    rows = read_manifest()
                    rows[job["run_id"]] = row
                    write_manifest(rows)

                policy = cfg["campaign"]["attack_policy"]
                profiles = cfg["campaign"].get("severity_profiles", {})
                attack_parameters = profiles.get(f"{job['attack_type']}_{variant}",
                                                 profiles.get(job["attack_type"], {})).get(job["severity"], {})
                context = AttackContext(
                    run_id=job["run_id"], attack_type=job["attack_type"],
                    attack_variant=variant, severity=job["severity"],
                    parameters=attack_parameters,
                    emit_event=emit_attack_event,
                    metadata={"proxy": proxy} if proxy is not None else {},
                )
                attack_manager = AttackManager(
                    context, get_controller_factory(job["attack_type"], variant),
                    duration_s=policy["duration_s"],
                    eligible_events=cfg["campaign"]["missions"][job["scenario_id"]].get(
                        "attack_trigger_events", policy["eligible_events"]), seed=seed,
                    require_full_duration=policy.get("require_full_duration", True),
                    start_after_event=policy.get("start_after_event", "takeoff_stabilized"),
                    min_before_land_s=policy.get("min_before_land_s", 10),
                    max_trigger_occurrence=cfg["campaign"]["missions"][job["scenario_id"]].get(
                        "attack_trigger_max_occurrence", 1),
                )

            await execute_mission(drone, cfg, cfg["campaign"]["missions"][job["scenario_id"]],
                                  events, telemetry_cache, attack_manager)

            remaining = float(cfg["campaign"]["duration_s"]) - (time.monotonic() - mission_started)
            if remaining > 0:
                events.write(json.dumps({
                    "timestamp_epoch_ns": time.time_ns(),
                    "event": "post_landing_capture",
                    "duration_s": round(remaining, 2)
                }) + "\n")
                await asyncio.sleep(remaining)
            events.write(json.dumps({"timestamp_epoch_ns": time.time_ns(), "event": "run_complete"}) + "\n")

        row["status"] = "completed"
    except BaseException as exc:
        row["status"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)) else "failed"
        row["error"] = f"{type(exc).__name__}: {exc}"
        with paths["event_path"].open("a", encoding="utf-8") as events:
            events.write(json.dumps({
                "timestamp_epoch_ns": time.time_ns(),
                "event": "run_error",
                "error": row["error"],
                "traceback": traceback.format_exc()
            }) + "\n")
        raise
    finally:
        if attack_manager is not None:
            try:
                await attack_manager.close()
            except Exception as exc:
                row["status"] = "failed"
                row["error"] = (row.get("error", "") + f"; attack cleanup: {exc}").strip("; ")
        if proxy is not None:
            try:
                await proxy.close()
            except Exception as exc:
                row["status"] = "failed"
                row["error"] = (row.get("error", "") + f"; proxy cleanup: {exc}").strip("; ")
        stop.set()
        ground_truth_stop.set()
        if collector:
            try:
                await asyncio.wait_for(collector, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                collector.cancel()
                await asyncio.gather(collector, return_exceptions=True)
            except Exception as exc:
                row["status"] = "failed"
                row["error"] = (row.get("error", "") + f"; telemetry collector: {exc}").strip("; ")
        if ground_truth_collector:
            try:
                await asyncio.wait_for(ground_truth_collector, timeout=5)
            except asyncio.TimeoutError:
                ground_truth_collector.cancel()
                await asyncio.gather(ground_truth_collector, return_exceptions=True)
                row["status"] = "failed"
                row["error"] = (row.get("error", "") + "; ground-truth collector did not stop cleanly").strip("; ")
            except Exception as exc:
                row["status"] = "failed"
                row["error"] = (row.get("error", "") + f"; ground-truth collector: {exc}").strip("; ")
        await stop_process_group(capture, interrupt_timeout=5)
        await stop_process_group(px4, interrupt_timeout=8)
        row["completed_at"] = utc_now() if row["status"] == "completed" else ""
        rows = read_manifest()
        rows[job["run_id"]] = row
        write_manifest(rows)


def main() -> int:
    global DATA_ROOT, MANIFEST
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--px4-dir", type=Path, default=Path(os.environ.get("PX4_DIR", "")))
    parser.add_argument("--include-attacks", action="store_true",
                        help="Include configured Phase 2 attack runs; requires validated proxy and ground-truth configuration.")
    parser.add_argument("--fail-fast", action="store_true",
                        help="Stop batch on first error instead of continuing to next run.")
    parser.add_argument("--only-run", metavar="RUN_ID",
                        help="Run exactly one benign job (e.g. RUN_00001) for a pilot.")
    parser.add_argument("--output-root", type=Path, default=DATA_ROOT,
                        help="Data directory for this invocation; use a separate path for pilots.")
    args = parser.parse_args()

    if not args.config.is_file():
        parser.error(f"config not found: {args.config}")
    if not args.px4_dir.is_dir():
        parser.error("Set PX4_DIR or pass --px4-dir to the PX4-Autopilot directory inside WSL")
    if not which("dumpcap") and not which("tcpdump"):
        parser.error("Install dumpcap or tcpdump inside WSL")

    with args.config.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if args.include_attacks:
        campaign = cfg.get("campaign", {})
        policy = campaign.get("attack_policy", {})
        proxy = policy.get("proxy", {})
        if not policy.get("ground_truth_topic"):
            parser.error("Attack campaign requires attack_policy.ground_truth_topic; discover it with `gz topic -l` first")
        if not proxy.get("topology_validated", False):
            parser.error("Attack campaign requires attack_policy.proxy.topology_validated=true after confirming both MAVLink UDP legs traverse the relay")
        if not proxy.get("runtime_integrated", False):
            parser.error("The loopback relay has not yet been wired into this PX4 launch profile; keep attack generation disabled until PX4 UDP output and MAVSDK are both routed through it")
        for attack in campaign.get("attack_types", []):
            variants = campaign.get("attack_variants", {}).get(attack, [attack])
            for variant in variants:
                try:
                    validate_controller(attack, variant)
                except Exception as exc:
                    parser.error(str(exc))
        if "SHADOW_GCS_MITM" in campaign.get("attack_types", []):
            parser.error("SHADOW_GCS_MITM is registered but remains disabled until a scenario-defined bounded command and PX4 mode mapping are implemented")

    DATA_ROOT = args.output_root.resolve()
    MANIFEST = DATA_ROOT / "manifest.csv"

    start_command = os.environ.get("PX4_START_CMD", cfg["campaign"]["sitl_start_command"])
    jobs = build_jobs(cfg, benign_only=not args.include_attacks)
    if args.only_run:
        jobs = [job for job in jobs if job["run_id"] == args.only_run]
        if not jobs:
            parser.error(f"Unknown run id: {args.only_run}")
    rows = read_manifest()

    for job in jobs:
        existing = rows.get(job["run_id"])
        if existing and existing.get("status") == "completed":
            print(f"[skip] {job['run_id']} already completed")
            continue
        if existing:
            print(f"[resume] restarting incomplete {job['run_id']} from a clean SITL instance")
        print(f"[run] {job['run_id']} {job['scenario_id']} seed={job['seed']} {job['attack_type']}")
        try:
            asyncio.run(run_one(job, cfg, args.px4_dir.resolve(), start_command))
        except KeyboardInterrupt:
            print("Interrupted by user. Completed runs are checkpointed; run will resume cleanly.")
            return 130
        except Exception as exc:
            print(f"[error] {job['run_id']}: {exc}", file=sys.stderr)
            if args.fail_fast:
                return 1
            print(f"[continue] continuing to next run after failure in {job['run_id']}")
        finally:
            time.sleep(2)

    print(f"Campaign complete: {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

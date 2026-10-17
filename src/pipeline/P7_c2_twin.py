"""Reproducible network-level C2 emulation driven by AFAR GPS traces.

This is deliberately not described as PX4/Gazebo or real MAVLink SITL: it
emits timestamped JSON/UDP C2-like datagrams and replays the recorded GPS
trace into telemetry CSV while applying Mininet/TCLink impairments.
"""
import argparse
import csv
import json
import logging
import math
import os
import re
import shlex
import subprocess
import sys
import time
import numpy as np
from pathlib import Path

try:
    from mininet.net import Containernet
    from mininet.node import Controller
    from mininet.link import TCLink
except ImportError:
    Containernet = Controller = TCLink = None

from P5_layer3_link import LinkImpairmentModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("P7_NetworkTwin")

ATTACKS = {"T0_Benign", "T1_Replay", "T2_FDI", "T4_DoS"}


def distance_m(lat1, lon1, alt1, lat2, lon2, alt2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    d2 = 2*r*math.asin(min(1.0, math.sqrt(a)))
    return math.hypot(d2, alt2-alt1)


def load_trace(base_dir, mission_id):
    path = Path(base_dir) / "data" / "missions" / f"{mission_id}.csv"
    if not path.is_file():
        raise FileNotFoundError(f"Trajectory trace not found: {path}")
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if len(rows) < 2:
        raise ValueError(f"Trajectory must contain at least two samples: {path}")
    trace = [{k: float(row[k]) for k in ("time_s", "latitude", "longitude", "altitude_relative_m", "gcs_latitude", "gcs_longitude")} for row in rows]
    if any(not all(math.isfinite(v) for v in row.values()) for row in trace):
        raise ValueError("Trajectory contains non-finite coordinates/timestamps")
    if any(trace[i]["time_s"] <= trace[i-1]["time_s"] for i in range(1, len(trace))):
        raise ValueError("Trajectory timestamps must be strictly increasing")
    return trace


def py_background(node, code, log_name):
    command = f"python3 -c {shlex.quote(code)} >/tmp/{log_name} 2>&1 & echo $!"
    lines = node.cmd(command).strip().splitlines()
    if not lines or not lines[-1].strip().isdigit():
        raise RuntimeError(f"Failed to start process in {node.name}: {node.cmd('tail -20 /tmp/' + log_name)}")
    return int(lines[-1].strip())


def stop_process(node, pid):
    if pid:
        node.cmd(f"kill -TERM {int(pid)} 2>/dev/null || true")


def start_c2_twin(mission_id, channel_id, traffic_id, seed, frequency_ghz, output_dir, base_dir):
    if Containernet is None:
        raise RuntimeError("Containernet/Mininet is unavailable; use Linux with the configured testbed")
    if traffic_id not in ATTACKS:
        raise ValueError(f"Unsupported attack {traffic_id!r}; supported emulations: {sorted(ATTACKS)}. MITM is not implemented.")
    if not re.fullmatch(r"F[1-3]_afar_trace_[1-3]", mission_id):
        raise ValueError(f"Invalid mission trace id: {mission_id!r}")
    if channel_id not in LinkImpairmentModel.PROFILES:
        raise ValueError(f"Unsupported channel stress scenario: {channel_id!r}")
    root, out = Path(base_dir).resolve(), Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    trace = load_trace(root, mission_id)
    link_model = LinkImpairmentModel(root / "data" / "model_fits" / "layer1_dataset32_pathloss_fit.json", frequency_ghz=frequency_ghz, seed=seed)
    dimage = "uav-cas-node:latest"
    image_check = subprocess.run(["docker", "image", "inspect", "--format={{.Id}}", dimage],
                                 capture_output=True, text=True)
    if image_check.returncode != 0:
        raise RuntimeError(f"Required container image {dimage!r} is not available; build it from uav-cas/Docker/Dockerfile.node")
    image_id = image_check.stdout.strip()
    net = Containernet(controller=Controller, link=TCLink)
    net.addController("c0")
    gcs = net.addDocker("gcs", ip="10.0.0.10", dimage=dimage,
                        volumes=[f"{out}:/output"])
    uav = net.addDocker("uav", ip="10.0.0.20", dimage=dimage)
    attacker = net.addDocker("attacker", ip="10.0.0.30", dimage=dimage)
    switch = net.addSwitch("s1")
    net.addLink(gcs, switch, cls=TCLink, bw=100)
    uav_link = net.addLink(uav, switch, cls=TCLink, bw=100)
    net.addLink(attacker, switch, cls=TCLink, bw=100)
    pcap_host = out / "c2_traffic.pcap"
    telemetry_path = out / "flight_telemetry.csv"
    events_path = out / "attack_events.jsonl"
    tcpdump_pid = gcs_pid = attack_pid = None
    started_net = False
    events = []
    try:
        net.start()
        started_net = True
        gcs.cmd("mkdir -p /output")
        # Capture at the GCS endpoint; an ordinary switch-attached monitor
        # would miss unicast traffic without an explicitly configured mirror.
        tcpdump_lines = gcs.cmd("tcpdump -U -n -i any -w /output/c2_traffic.pcap 'udp port 14550' >/tmp/tcpdump.log 2>&1 & echo $!").strip().splitlines()
        if not tcpdump_lines or not tcpdump_lines[-1].isdigit():
            raise RuntimeError("Could not start tcpdump in GCS container")
        tcpdump_pid = int(tcpdump_lines[-1])
        time.sleep(0.5)

        # These are explicit C2-like UDP datagrams, not MAVLink packets.
        duration = trace[-1]["time_s"]
        benign_code = ("import json,socket,time; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                       f"end=time.monotonic()+{duration!r}; i=0; "
                       "exec('while time.monotonic()<end:\\n s.sendto(json.dumps({\"kind\":\"heartbeat\",\"seq\":i}).encode(),(\"10.0.0.20\",14550)); i+=1; time.sleep(0.2)')")
        gcs_pid = py_background(gcs, benign_code, "gcs_heartbeat.log")
        if traffic_id != "T0_Benign":
            if traffic_id == "T4_DoS":
                attack_code = ("import socket,time; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                               f"end=time.monotonic()+{duration!r}; payload=b'DOS_TEST'; "
                               "exec('while time.monotonic()<end:\\n s.sendto(payload,(\"10.0.0.20\",14550)); time.sleep(0.005)')")
            elif traffic_id == "T1_Replay":
                stale = json.dumps({"kind": "heartbeat", "seq": 0, "replayed": True}).encode().decode()
                attack_code = ("import socket,time; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                               f"end=time.monotonic()+{duration!r}; payload={stale!r}.encode(); "
                               "exec('while time.monotonic()<end:\\n s.sendto(payload,(\"10.0.0.20\",14550)); time.sleep(0.1)')")
            else:  # T2_FDI: injected false C2-like state values, not MAVLink FDI.
                payload = json.dumps({"kind": "telemetry_injection", "latitude": 0.0, "longitude": 0.0,
                                      "altitude_m": -999.0, "seed": int(seed)}).encode().decode()
                attack_code = ("import socket,time; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); "
                               f"end=time.monotonic()+{duration!r}; payload={payload!r}.encode(); "
                               "exec('while time.monotonic()<end:\\n s.sendto(payload,(\"10.0.0.20\",14550)); time.sleep(0.1)')")
            attack_pid = py_background(attacker, attack_code, "attack.log")
            events.append({"attack_id": traffic_id, "start_epoch_s": time.time(), "end_epoch_s": None,
                           "src_ip": "10.0.0.30", "label": 1,
                           "semantics": {"T1_Replay": "repeated stale C2-like UDP datagram", "T2_FDI": "false C2-like UDP state injection", "T4_DoS": "bounded UDP flood"}[traffic_id]})

        start_epoch = time.time()
        columns = ["timestamp_s", "elapsed_s", "latitude", "longitude", "altitude_relative_m", "speed_m_s", "distance_to_gcs_m"]
        with telemetry_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            gcs_lat, gcs_lon = trace[0]["gcs_latitude"], trace[0]["gcs_longitude"]
            previous = None
            times = np.asarray([p["time_s"] for p in trace], dtype=float)
            tick_times = list(np.arange(0.0, times[-1], 1.0))
            if not tick_times or tick_times[-1] < times[-1]:
                tick_times.append(float(times[-1]))
            wall_start = time.monotonic()
            for elapsed in tick_times:
                point = {"time_s": elapsed,
                         "latitude": float(np.interp(elapsed, times, [p["latitude"] for p in trace])),
                         "longitude": float(np.interp(elapsed, times, [p["longitude"] for p in trace])),
                         "altitude_relative_m": float(np.interp(elapsed, times, [p["altitude_relative_m"] for p in trace]))}
                target_wall = wall_start + elapsed
                if time.monotonic() < target_wall:
                    time.sleep(target_wall - time.monotonic())
                dist = distance_m(gcs_lat, gcs_lon, 0.0,
                                  point["latitude"], point["longitude"], point["altitude_relative_m"])
                if previous is None:
                    speed = 0.0
                else:
                    dt = elapsed - previous["time_s"]
                    speed = distance_m(previous["latitude"], previous["longitude"], previous["altitude_relative_m"],
                                       point["latitude"], point["longitude"], point["altitude_relative_m"]) / dt
                delay, loss, rate = link_model.get_tc_params(dist, channel_id)
                # Host-facing intf controls UAV egress; switch-facing intf controls
                # the corresponding downlink egress toward the UAV.
                for intf in (uav_link.intf1, uav_link.intf2):
                    intf.config(delay=f"{delay:.3f}ms", loss=loss, bw=rate)
                writer.writerow({"timestamp_s": start_epoch + elapsed, "elapsed_s": elapsed,
                                 "latitude": point["latitude"], "longitude": point["longitude"],
                                 "altitude_relative_m": point["altitude_relative_m"], "speed_m_s": speed,
                                 "distance_to_gcs_m": dist})
                previous = point
        if attack_pid:
            stop_process(attacker, attack_pid)
            attack_pid = None
            events[-1]["end_epoch_s"] = time.time()
        if gcs_pid:
            stop_process(gcs, gcs_pid)
            gcs_pid = None
        events_path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        (out / "twin_scope.json").write_text(json.dumps({
            "emulation_type": "Mininet network-level C2-like UDP emulation",
            "not_simulated": ["PX4 SITL", "Gazebo", "MAVLink protocol", "RF waveform"],
            "mission_id": mission_id, "channel_scenario": channel_id, "attack_id": traffic_id,
            "channel_scenario_semantics": "named impairment stress profile; LOS/NLOS state not physically validated",
            "seed": int(seed), "frequency_ghz": float(frequency_ghz),
            "container_image": dimage, "container_image_id": image_id,
            "link_parameters": "declared stress-test profile scaled by relative fitted path-loss exponent",
            "attack_events_file": events_path.name}, indent=2), encoding="utf-8")
        logger.info("Finished network-level emulation for %s; outputs at %s", mission_id, out)
    finally:
        if attack_pid:
            stop_process(attacker, attack_pid)
        if gcs_pid:
            stop_process(gcs, gcs_pid)
        if tcpdump_pid:
            gcs.cmd(f"kill -INT {tcpdump_pid} 2>/dev/null || true")
            time.sleep(0.2)
        if started_net:
            net.stop()
    if not pcap_host.is_file() or pcap_host.stat().st_size == 0:
        raise RuntimeError("PCAP capture missing/empty; monitor output volume or traffic capture failed")


if __name__ == "__main__":
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        logger.error("FAIL-CLOSED: P7 requires root on Linux with Docker and Containernet")
        sys.exit(1)
    ap = argparse.ArgumentParser()
    ap.add_argument("--mission", required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--traffic", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--frequency_ghz", type=float, required=True)
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--base_dir", required=True)
    args = ap.parse_args()
    try:
        start_c2_twin(args.mission, args.channel, args.traffic, args.seed, args.frequency_ghz, args.output_dir, args.base_dir)
    except Exception as exc:
        logger.exception("FAIL-CLOSED: %s", exc)
        sys.exit(1)

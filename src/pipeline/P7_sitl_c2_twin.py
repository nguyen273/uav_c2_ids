"""PX4 SITL + MAVLink + Containernet cyber-physical experiment runner.

This couples a recorded AFAR route to PX4/Gazebo flight dynamics, sends the
mission over routed MAVLink through a shaped network link, and applies the
declared Layer-3 stress profile using live SITL telemetry. It is a software
cyber-physical twin prototype; it is not an RF/channel-sounding twin.
"""
import argparse
import csv
import json
import logging
import math
import os
import re
import random
import shlex
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

try:
    from mininet.net import Containernet
    from mininet.node import Controller
    from mininet.link import TCLink
except ImportError:
    Containernet = Controller = TCLink = None

from P5_layer3_link import LinkImpairmentModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("P7_SITL_Twin")
EARTH_RADIUS_M = 6_371_000.0


def read_route(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    required = {"time_s", "latitude", "longitude", "altitude_relative_m", "gcs_latitude", "gcs_longitude"}
    if len(rows) < 2 or not required.issubset(rows[0]):
        raise ValueError(f"P4 route {path} must contain >=2 rows and {sorted(required)}")
    route = [{key: float(row[key]) for key in required} for row in rows]
    if any(not all(math.isfinite(v) for v in row.values()) for row in route):
        raise ValueError("Route contains non-finite data")
    if any(route[i]["time_s"] <= route[i-1]["time_s"] for i in range(1, len(route))):
        raise ValueError("Route timestamps must be strictly increasing")
    if any(not (-90 <= p["latitude"] <= 90 and -180 <= p["longitude"] <= 180) for p in route):
        raise ValueError("Route has invalid latitude/longitude")
    return route


def node_bg(node, command, log_path):
    raw = node.cmd(f"sh -c {shlex.quote(command)} >{shlex.quote(log_path)} 2>&1 & echo PID:$!")
    match = re.search(r"PID:(\d+)", raw)
    if not match:
        numbers = re.findall(r"\b\d+\b", raw)
        if not numbers:
            raise RuntimeError(f"Could not launch process on {node.name}: {raw!r}")
        return int(numbers[-1])
    return int(match.group(1))


def read_latest_telemetry(path):
    try:
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        row = rows[-1] if rows else None
        if not row:
            return None
        for k in ("latitude", "longitude", "altitude_relative_m"):
            val = row.get(k)
            if val is None or val == "" or str(val).lower() == "nan":
                return None
        lat, lon, alt = (float(row[k]) for k in ("latitude", "longitude", "altitude_relative_m"))
        if not all(map(math.isfinite, (lat, lon, alt))):
            return None
        wp_cur = int(row.get("mission_current") or 0)
        wp_tot = int(row.get("mission_total") or 0)
        elapsed = float(row.get("elapsed_s") or 0.0)
        mode = row.get("flight_mode") or "MISSION"
        return lat, lon, alt, wp_cur, wp_tot, elapsed, mode
    except Exception:
        return None


def _sample_waypoint_speeds(model_path, route, seed):
    model = json.loads(model_path.read_text(encoding="utf-8"))
    if model.get("calibration_status") != "afar_train_source_group_mobility_profile_with_heldout_distribution_evaluation":
        raise ValueError("Unsupported AFAR Layer-2 mobility calibration artifact")
    params = model.get("gauss_markov_speed_model", {})
    alpha = float(params.get("alpha_1s", float("nan")))
    mean = float(params.get("mean_speed_m_s", float("nan")))
    sigma = float(params.get("innovation_sigma_m_s", float("nan")))
    turn_rate_p95 = float(params.get("turn_rate_abs_p95_deg_s", float("nan")))
    if not (math.isfinite(alpha) and 0.0 <= alpha < 1.0 and math.isfinite(mean)
            and 0.5 <= mean <= 20.0 and math.isfinite(sigma) and sigma >= 0.0
            and math.isfinite(turn_rate_p95) and turn_rate_p95 >= 0.0):
        raise ValueError(f"Invalid fitted Gauss-Markov speed parameters: {params}")
    rng = random.Random(int(seed))
    profile = [mean]
    for previous, current in zip(route[:-1], route[1:]):
        lat0 = math.radians(previous["latitude"])
        lat1 = math.radians(current["latitude"])
        dlat = lat1 - lat0
        dlon = math.radians(current["longitude"] - previous["longitude"])
        haversine = math.sin(dlat / 2) ** 2 + math.cos(lat0) * math.cos(lat1) * math.sin(dlon / 2) ** 2
        segment_m = 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(haversine)))
        dt_s = max(0.1, segment_m / mean)
        phi = alpha ** dt_s
        if alpha > 0.0 and alpha < 1.0:
            stationary_variance = sigma ** 2 / (1.0 - alpha ** 2)
            innovation_sigma = math.sqrt(max(0.0, stationary_variance * (1.0 - phi ** 2)))
        else:
            innovation_sigma = sigma * math.sqrt(dt_s)
        speed = mean + phi * (profile[-1] - mean) + rng.gauss(0.0, innovation_sigma)
        speed = min(20.0, max(0.5, speed))
        profile.append(speed)
    altitude_profile = model.get("altitude_profile_normalized_progress_m")
    if not isinstance(altitude_profile, list) or len(altitude_profile) < 2 or not all(
            math.isfinite(float(value)) for value in altitude_profile):
        raise ValueError("Layer-2 fit is missing a valid normalized-progress altitude profile")
    return profile, {"alpha_1s": alpha, "mean_speed_m_s": mean,
                     "innovation_sigma_m_s": sigma,
                     "turn_rate_abs_p95_deg_s": turn_rate_p95}, altitude_profile


def _apply_turn_rate_speed_cap(route, speeds, turn_rate_cap_deg_s):
    """Reduce speed near sharp waypoint turns using the train-derived cap."""
    result = list(speeds)
    points = [(math.radians(p["latitude"]), math.radians(p["longitude"])) for p in route]
    xy = [(EARTH_RADIUS_M * lon * math.cos(points[0][0]), EARTH_RADIUS_M * lat)
          for lat, lon in points]
    bearings, lengths = [], []
    for (x0, y0), (x1, y1) in zip(xy[:-1], xy[1:]):
        dx, dy = x1 - x0, y1 - y0
        bearings.append(math.atan2(dx, dy))
        lengths.append(math.hypot(dx, dy))
    cap_radians = math.radians(turn_rate_cap_deg_s)
    for idx in range(1, len(route) - 1):
        delta = abs((bearings[idx] - bearings[idx - 1] + math.pi) % (2 * math.pi) - math.pi)
        mean_segment_length = 0.5 * (lengths[idx - 1] + lengths[idx])
        if delta > 1e-6 and mean_segment_length > 1e-3 and cap_radians > 0.0:
            curvature_rad_per_m = delta / mean_segment_length
            result[idx] = max(0.5, min(result[idx], cap_radians / curvature_rad_per_m))
    return result


def _cleanup_containernet():
    """Remove any stale Containernet docker containers and clean Mininet topology."""
    subprocess.run(["docker", "rm", "-f", "mn.gcs", "mn.uav", "mn.attacker"],
                   capture_output=True, text=True)
    subprocess.run(["mn", "-c"], capture_output=True, text=True)


def run_sitl(mission_id, channel_id, traffic_id, seed, frequency_ghz, output_dir, base_dir):
    _cleanup_containernet()
    if Containernet is None:
        raise RuntimeError("Containernet/Mininet unavailable; run in configured Linux environment as root")
    if traffic_id not in {"T0_Benign", "T4_DoS"}:
        raise ValueError("SITL currently supports T0_Benign and T4_DoS only; T1_Replay/T2_FDI are not implemented as MAVLink attacks")
    if not re.fullmatch(r"F\d+_afar_trace_\d+", mission_id):
        raise ValueError(f"Invalid mission id {mission_id!r}")
    root, out = Path(base_dir).resolve(), Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    route_path = root / "data" / "missions" / f"{mission_id}.csv"
    route = read_route(route_path)
    mobility_fit_path = root / "data" / "model_fits" / "layer2_afar_mobility_calibration.json"
    speed_profile, mobility_parameters, fitted_altitude_profile = _sample_waypoint_speeds(
        mobility_fit_path, route, seed)
    speed_profile = _apply_turn_rate_speed_cap(
        route, speed_profile, mobility_parameters["turn_rate_abs_p95_deg_s"])
    altitude_profile = np.interp(np.linspace(0.0, 1.0, len(route)),
                                 np.linspace(0.0, 1.0, len(fitted_altitude_profile)),
                                 fitted_altitude_profile).tolist()
    layer1_model = root / "data" / "model_fits" / "layer1_dataset32_pathloss_fit.json"
    link_model = LinkImpairmentModel(layer1_model, frequency_ghz, seed)
    image = os.environ.get("UAV_CAS_SITL_IMAGE", "uav-cas-sitl:latest")
    inspect = subprocess.run(["docker", "image", "inspect", "--format={{.Id}}", image], capture_output=True, text=True)
    if inspect.returncode:
        raise RuntimeError(f"Required SITL image {image!r} missing. Build the image with PX4, Gazebo, mavlink-router, tcpdump, hping3, Python and MAVSDK installed.")
    image_id = inspect.stdout.strip()
    net = Containernet(controller=None, link=TCLink)
    gcs = net.addDocker("gcs", ip="10.0.0.10", dimage=image,
                        volumes=[f"{out}:/output", f"{root}:/workspace:ro"])
    uav = net.addDocker("uav", ip="10.0.0.20", dimage=image,
                        volumes=[f"{out}:/output"])
    attacker = net.addDocker("attacker", ip="10.0.0.30", dimage=image)
    switch = net.addSwitch("s1", failMode="standalone")
    net.addLink(gcs, switch, cls=TCLink, bw=100)
    uav_link = net.addLink(uav, switch, cls=TCLink, bw=100)
    net.addLink(attacker, switch, cls=TCLink, bw=100)
    started = False
    px4_pid = router_pid = capture_pid = gcs_pid = attack_pid = gz_pid = None
    telemetry = out / "flight_telemetry.csv"
    pcap = out / "c2_traffic.pcap"
    events = []
    try:
        net.start()
        started = True
        for node in (gcs, uav, attacker):
            node.cmd("mkdir -p /output")
        # Fail early with an actionable message instead of pretending this image contains PX4.
        for binary in ("make", "mavlink-routerd", "tcpdump"):
            if uav.cmd(f"command -v {binary}").strip() == "":
                raise RuntimeError(f"SITL image {image} lacks required executable: {binary}")
        px4_check = uav.cmd("test -f /PX4-Autopilot/Makefile && echo px4_ok")
        if "px4_ok" not in px4_check:
            raise RuntimeError(f"SITL image must provide /PX4-Autopilot source and a built gz_x500 target (output: {px4_check!r})")
        mavsdk_check = gcs.cmd("python3 -c 'import mavsdk; print(\"mavsdk_ok\")'")
        if "mavsdk_ok" not in mavsdk_check:
            raise RuntimeError(f"MAVSDK Python package is unavailable in GCS image (output: {mavsdk_check!r})")
        # Router ingests PX4's GCS stream on 14550 and forwards it to the SDK
        # listener on 14540 (MAVSDK's documented offboard API port).
        router_cfg = out / "mavlink-router.conf"
        router_cfg.write_text("""[General]\nTcpServerPort=5760\n[UdpEndpoint px4]\nMode=Server\nAddress=127.0.0.1\nPort=14550\n[UdpEndpoint gcs]\nMode=Normal\nAddress=10.0.0.10\nPort=14540\n""", encoding="utf-8")
        uav.cmd(f"cp /output/mavlink-router.conf /tmp/mavlink-router.conf")
        router_pid = node_bg(uav, "mavlink-routerd -c /tmp/mavlink-router.conf", "/tmp/mavlink-router.log")
        home = route[0]
        # Align Gazebo world origin and PX4 GZBridge parameters with the route GPS origin
        set_origin_script = out / "set_origin.py"
        set_origin_script.write_text(f"""import re, os
# 1. Update Gazebo world spherical_coordinates
for p in ['/PX4-Autopilot/Tools/simulation/gz/worlds/default.sdf']:
    if os.path.exists(p):
        try:
            c = open(p).read()
            coord = '''    <spherical_coordinates>
      <surface_model>EARTH_WGS84</surface_model>
      <world_frame_orientation>ENU</world_frame_orientation>
      <latitude_deg>{home['latitude']}</latitude_deg>
      <longitude_deg>{home['longitude']}</longitude_deg>
      <elevation>0</elevation>
      <heading_deg>0</heading_deg>
    </spherical_coordinates>'''
            if '<spherical_coordinates>' in c:
                c = re.sub(r'<spherical_coordinates>.*?</spherical_coordinates>', coord, c, flags=re.DOTALL)
            else:
                c = c.replace('</world>', coord + '\\n  </world>')
            open(p, 'w').write(c)
        except Exception as e:
            print("SDF patch err:", e)

# 2. Patch px4-rc.simulator to bind PX4_HOME_LAT/LON to SIM_GZ_HOME_LAT/LON and disable simulation failsafes
patch = '''
if [ -n "${{PX4_HOME_LAT}}" ]; then
	param set SIM_GZ_HOME_LAT ${{PX4_HOME_LAT}}
fi
if [ -n "${{PX4_HOME_LON}}" ]; then
	param set SIM_GZ_HOME_LON ${{PX4_HOME_LON}}
fi
if [ -n "${{PX4_HOME_ALT}}" ]; then
	param set SIM_GZ_HOME_ALT ${{PX4_HOME_ALT}}
fi
param set CBRK_SUPPLY_CHK 894281
param set COM_LOW_BAT_ACT 0
param set COM_ARM_MAG_STR 0
param set MIS_DIST_1WP 0
param set COM_DISARM_PRFLT -1
param set NAV_DLL_ACT 0
param set NAV_RCL_ACT 0
'''
for rc in [
    '/PX4-Autopilot/build/px4_sitl_default/etc/init.d-posix/px4-rc.simulator',
    '/PX4-Autopilot/ROMFS/px4fmu_common/init.d-posix/px4-rc.simulator'
]:
    if os.path.exists(rc):
        try:
            content = open(rc).read()
            if 'SIM_GZ_HOME_LAT' not in content:
                content = content.replace('#!/bin/sh', '#!/bin/sh\\n' + patch)
            if 'sleep 3; gz_bridge' not in content and 'gz_bridge start' in content:
                content = content.replace('gz_bridge start', 'sleep 3; gz_bridge start')
            open(rc, 'w').write(content)
        except Exception as e:
            print("RC patch err:", e)

print("ORIGIN_SET_OK")
""", encoding="utf-8")
        res = uav.cmd("python3 /output/set_origin.py").strip()
        log.info("Gazebo origin update result: %s", res)

        uav.cmd("killall -9 gz sim px4 ruby 2>/dev/null || true")
        uav.cmd("rm -rf /root/.gz /tmp/gz* /dev/shm/fastrtps* /dev/shm/gz* 2>/dev/null || true")

        px4_cmd = ("cd /PX4-Autopilot && "
                   f"export PX4_HOME_LAT={home['latitude']} && export PX4_HOME_LON={home['longitude']} && export PX4_HOME_ALT=0 && "
                   "export GZ_SIM_RESOURCE_PATH=/PX4-Autopilot/Tools/simulation/gz/models:/PX4-Autopilot/Tools/simulation/gz/worlds && "
                   "export HEADLESS=1 && tail -f /dev/null | make px4_sitl gz_x500")
        px4_pid = node_bg(uav, px4_cmd, "/tmp/px4.log")
        capture_pid = node_bg(gcs, "tcpdump -U -n -i any -w /output/c2_traffic.pcap 'udp port 14540 or udp port 14550'", "/tmp/tcpdump.log")
        attack_started = False
        attack_ended = False
        route_arg = f"/workspace/data/missions/{mission_id}.csv"
        script = "/workspace/src/pipeline/scripts/gcs_mavsdk_mission.py"
        launch = (f"python3 {shlex.quote(script)} --route {shlex.quote(route_arg)} "
                  "--out /output/flight_telemetry.csv --system-address udp://:14540 "
                  f"--speed-profile-m-s {shlex.quote(','.join(f'{value:.6f}' for value in speed_profile))} "
                  f"--altitude-profile-relative-m {shlex.quote(','.join(f'{value:.6f}' for value in altitude_profile))} "
                  f"--gcs-latitude {route[0]['gcs_latitude']} --gcs-longitude {route[0]['gcs_longitude']} "
                  "; rc=$?; echo $rc >/output/gcs_exit_code")
        gcs_pid = node_bg(gcs, launch, "/tmp/gcs.log")

        # Drive Mininet qdisc from live PX4 telemetry. Until the first record arrives,
        # hold the declared C1 profile; never substitute the AFAR source trace as telemetry.
        started_at = time.monotonic()
        last_row = None
        last_update = 0.0
        last_progress_print = 0.0
        timeout = max(300.0, float(route[-1]["time_s"]) * 5 + 180.0)
        while time.monotonic() - started_at < timeout:
            telemetry_data = read_latest_telemetry(telemetry)
            if telemetry_data is not None:
                lat, lon, alt, wp_cur, wp_tot, elapsed, mode = telemetry_data
                row = (lat, lon, alt)
                if row != last_row and (time.monotonic() - last_update) >= 0.5:
                    ref = route[0]
                    dlat = math.radians(lat - ref["gcs_latitude"])
                    dlon = math.radians(lon - ref["gcs_longitude"])
                    horizontal = 6371000.0 * math.hypot(dlat, math.cos(math.radians(ref["gcs_latitude"])) * dlon)
                    distance = math.hypot(horizontal, alt)
                    delay, loss, rate = link_model.get_tc_params(distance, channel_id)
                    for interface in (uav_link.intf1, uav_link.intf2):
                        interface.config(delay=f"{delay:.3f}ms", loss=loss, bw=rate)
                    last_row, last_update = row, time.monotonic()

                # Inject T4_DoS once airborne (alt > 1.0m or wp_cur >= 1) so pre-flight handshake succeeds
                if traffic_id == "T4_DoS" and not attack_started and (alt > 1.0 or wp_cur >= 1):
                    attack_pid = node_bg(attacker, "hping3 --udp -c 5000 -i u10000 -p 14540 10.0.0.10", "/tmp/dos.log")
                    events.append({"attack_id": traffic_id, "start_epoch_s": time.time(), "end_epoch_s": None,
                                   "src_ip": "10.0.0.30", "label": 1,
                                   "semantics": "bounded UDP flood against the GCS MAVLink UDP endpoint; no MAVLink payload mutation"})
                    attack_started = True
                    log.info("► [ATTACK INJECTED] T4_DoS flood (5000 pkts @ 10ms) bắt đầu gửi tới 10.0.0.10:14540")

                # Track when hping3 finishes flood (usually ~50-53s) and record exact end_epoch_s immediately
                if attack_started and not attack_ended and attack_pid:
                    status = attacker.cmd(f"kill -0 {attack_pid} 2>/dev/null && echo ok").strip()
                    if "ok" not in status:
                        end_t = time.time()
                        events[-1]["end_epoch_s"] = end_t
                        attack_ended = True
                        duration = end_t - events[-1]["start_epoch_s"]
                        log.info("✓ [ATTACK FINISHED] Tiến trình hping3 đã hoàn tất trong %.2fs. Đã chốt end_epoch_s!", duration)

                if (time.monotonic() - last_progress_print) >= 10.0:
                    wp_pct = (wp_cur / wp_tot * 100) if wp_tot > 0 else 0
                    atk_label = "ATTACK_FLOODING" if (attack_started and not attack_ended) else ("ATTACK_STOPPED" if attack_ended else "BENIGN")
                    log.info("✈ [%s|%s|%s] Tiến độ bay: WP %d/%d (%.0f%%) | Cao độ: %.1fm | Đã bay: %.0fs | Tấn công: %s",
                             mission_id, channel_id, traffic_id, wp_cur, wp_tot, wp_pct, alt, elapsed, atk_label)
                    last_progress_print = time.monotonic()
            exit_code = out / "gcs_exit_code"
            if exit_code.exists():
                code = exit_code.read_text(encoding="utf-8").strip()
                if code != "0":
                    raise RuntimeError(f"GCS MAVSDK mission exited {code}: {gcs.cmd('cat /tmp/gcs.log')}")
                break
            if (time.monotonic() - started_at) > 20.0 and last_row is None:
                px4_status = uav.cmd(f"kill -0 {px4_pid} 2>/dev/null && echo ok").strip()
                if "ok" not in px4_status:
                    raise RuntimeError(f"PX4 died early: {uav.cmd('tail -n 25 /tmp/px4.log')}")
            if last_row is not None and (time.monotonic() - last_update) > 30.0:
                px4_status = uav.cmd(f"kill -0 {px4_pid} 2>/dev/null && echo ok").strip()
                if "ok" not in px4_status:
                    raise RuntimeError(f"PX4 died mid-flight: {uav.cmd('tail -n 25 /tmp/px4.log')}")
                gcs_status = gcs.cmd(f"kill -0 {gcs_pid} 2>/dev/null && echo ok").strip()
                if "ok" not in gcs_status:
                    raise RuntimeError(f"GCS died mid-flight: {gcs.cmd('tail -n 25 /tmp/gcs.log')}")
                if (time.monotonic() - last_update) > 60.0:
                    raise RuntimeError(f"Telemetry stream stalled mid-flight for > 60s (last mode: {mode}, alt: {alt:.1f}m)")
            time.sleep(0.25)
        else:
            raise TimeoutError(f"SITL mission exceeded {timeout:.0f}s; inspect /tmp/px4.log and /tmp/gcs.log")
        if last_row is None:
            raise RuntimeError("MAVSDK mission completed without live telemetry; no twin coupling occurred")
        if events and events[-1]["end_epoch_s"] is None:
            events[-1]["end_epoch_s"] = time.time()
            dur = events[-1]["end_epoch_s"] - events[-1]["start_epoch_s"]
            log.warning("T4_DoS attack event đóng lúc kết thúc mission (thời lượng = %.2fs)", dur)
        (out / "attack_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")ing="utf-8")
        scope = {
            "twin_type": "software cyber-physical Digital Twin prototype",
            "flight_dynamics": "PX4 SITL + Gazebo gz_x500",
            "control_plane": "MAVSDK mission over MAVLink routed through Containernet/TCLink",
            "mobility_model": {"type": "AFAR train-source-group empirical kinematic profile",
                               "gauss_markov_parameters": mobility_parameters,
                               "temporal_scaling": "1-second fitted AR(1) coefficient converted to an estimated inter-waypoint travel-time step using segment distance / fitted mean speed",
                               "waypoint_speed_profile_m_s": speed_profile,
                               "waypoint_altitude_profile_relative_m": altitude_profile,
                               "coupled_parameters": ["seeded Gauss-Markov speed sequence", "train p95 turn-rate speed cap at route corners", "train-median altitude profile over normalized mission progress"],
                               "diagnostic_only_features": ["vertical-rate distribution", "spatial occupancy distribution"]},
            "mobility_input": {"mission_id": mission_id, "path": route_path.name,
                               "horizontal_geometry_source": "AFAR recorded GPS trace; resampled route",
                               "altitude_mapping": "train-fitted median relative-altitude profile + 15m MAVSDK mission offset; PX4 origin at first GPS point"},
            "channel_model": {"scenario": channel_id,
                              "reference_dataset_carrier_ghz": frequency_ghz,
                              "simulated_rf_carrier_ghz": None,
                              "layer1_model_type": link_model.model_type,
                              "layer1_dataset": "AERPAW Dataset-32, Dryad DOI 10.5061/dryad.2ngf1vj4r",
                              "semantics": "Dataset-32 is the Layer-1 empirical fit reference and gates the 3.4 GHz campaign; qdisc values are declared stress assumptions and are not scaled as RF predictions because SITL has no matched RF/altitude link budget"},
            "attack_id": traffic_id, "seed": seed, "container_image": image, "container_image_id": image_id,
            "live_coupling": "live telemetry is logged and used for distance auditing; selected stress profile is applied to the qdisc without distance-to-RF scaling",
            "not_modeled": ["RF waveform/antenna propagation", "validated RF-to-PER mapping", "physical UAV hardware"],
            "artifacts": ["flight_telemetry.csv", "c2_traffic.pcap", "attack_events.jsonl", "twin_scope.json"]}
        (out / "twin_scope.json").write_text(json.dumps(scope, indent=2), encoding="utf-8")
        log.info("SITL cyber-physical run complete: %s", out)
    finally:
        if attack_pid:
            attacker.cmd(f"kill -TERM {attack_pid} 2>/dev/null || true")
            if events and events[-1]["end_epoch_s"] is None:
                events[-1]["end_epoch_s"] = time.time()
                (out / "attack_events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        if gcs_pid:
            gcs.cmd(f"kill -TERM {gcs_pid} 2>/dev/null || true")
        if capture_pid:
            gcs.cmd(f"kill -INT {capture_pid} 2>/dev/null || true")
            time.sleep(0.5)
        if px4_pid:
            uav.cmd(f"kill -TERM {px4_pid} 2>/dev/null || true")
        if gz_pid:
            uav.cmd(f"kill -TERM {gz_pid} 2>/dev/null || true")
        uav.cmd("killall -9 gz sim px4 2>/dev/null || true")
        if router_pid:
            uav.cmd(f"kill -TERM {router_pid} 2>/dev/null || true")
        # Collect small snippets of logs for auditability without filling disk.
        if out.exists() and started:
            for name, node in (("gz.log", uav), ("mavlink-router.log", uav), ("gcs.log", gcs), ("tcpdump.log", gcs)):
                try:
                    (out / name).write_text(node.cmd(f"tail -n 200 /tmp/{name.replace('.log','')}.log"), encoding="utf-8")
                except Exception:
                    pass
            try:
                (out / "px4.log").write_text(uav.cmd("tail -n 200 /tmp/px4.log"), encoding="utf-8")
            except Exception:
                pass
            # Wipe /tmp and internal ulogs to keep container & VM disk completely fresh
            uav.cmd("rm -f /tmp/*.log /tmp/mavlink-router.conf; rm -rf /PX4-Autopilot/build/px4_sitl_default/rootfs/log/* 2>/dev/null || true")
        if started:
            try:
                net.stop()
            except Exception:
                pass
        _cleanup_containernet()


def main():
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise SystemExit("P7 SITL requires Linux root, Docker, Containernet and a compatible SITL image")
    p = argparse.ArgumentParser()
    p.add_argument("--mission", required=True)
    p.add_argument("--channel", required=True)
    p.add_argument("--traffic", required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--frequency_ghz", type=float, required=True)
    p.add_argument("--output_dir", required=True)
    p.add_argument("--base_dir", required=True)
    a = p.parse_args()
    try:
        run_sitl(a.mission, a.channel, a.traffic, a.seed, a.frequency_ghz,
                 a.output_dir, a.base_dir)
    except Exception as e:
        log.exception("SITL twin failed: %s", e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

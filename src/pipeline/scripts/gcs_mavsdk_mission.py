"""Upload an AFAR-derived route to PX4 SITL and record live MAVSDK telemetry.

The route is a sampled recorded path, not a claim that AFAR logger timestamps or
altitudes are a validated PX4 flight plan. Altitude is translated to a safe
relative-altitude profile by adding --altitude_offset_m.
"""
import argparse
import asyncio
import csv
import math
import time
from pathlib import Path

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan


def read_route(path, altitude_offset_m, altitude_profile_relative_m=None):
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    required = {"time_s", "latitude", "longitude", "altitude_relative_m"}
    if len(rows) < 2 or not required.issubset(rows[0]):
        raise ValueError(f"Route CSV must contain >=2 rows and columns {sorted(required)}")
    points = []
    if altitude_profile_relative_m is not None and len(altitude_profile_relative_m) != len(rows):
        raise ValueError("Fitted altitude profile must have one value per route waypoint")
    for index, row in enumerate(rows):
        lat, lon = float(row["latitude"]), float(row["longitude"])
        relative_altitude = (float(altitude_profile_relative_m[index]) if altitude_profile_relative_m is not None
                             else float(row["altitude_relative_m"]))
        alt = relative_altitude + altitude_offset_m
        if not all(map(math.isfinite, (lat, lon, alt))) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Route contains invalid/non-finite coordinates")
        if alt < 3:
            raise ValueError("Translated route altitude is below 3 m relative to home")
        points.append((lat, lon, alt))
    duration_s = float(rows[-1]["time_s"]) if "time_s" in rows[-1] else 600.0
    return points, duration_s


async def wait_until(stream, predicate, label, timeout_s):
    async def _wait():
        async for value in stream:
            if predicate(value):
                return value
    try:
        return await asyncio.wait_for(_wait(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"Timed out waiting for {label} after {timeout_s}s") from exc


async def run(args):
    altitude_profile = ([float(value) for value in args.altitude_profile_relative_m.split(",")]
                        if args.altitude_profile_relative_m else None)
    points, route_duration = read_route(args.route, args.altitude_offset_m, altitude_profile)
    if args.speed_profile_m_s:
        speed_profile = [float(value) for value in args.speed_profile_m_s.split(",")]
        if len(speed_profile) != len(points) or not all(math.isfinite(v) and 0.5 <= v <= 20.0 for v in speed_profile):
            raise ValueError("The calibrated speed profile must have one finite 0.5–20 m/s speed per route waypoint")
    else:
        if not math.isfinite(args.speed_m_s) or not 0.5 <= args.speed_m_s <= 20.0:
            raise ValueError("Mission speed must be finite and between 0.5 and 20 m/s")
        speed_profile = [args.speed_m_s] * len(points)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    drone = System()
    await drone.connect(system_address=args.system_address)
    await wait_until(drone.core.connection_state(), lambda x: x.is_connected, "MAVLink connection", 45.0)
    await wait_until(drone.telemetry.health(), lambda x: x.is_global_position_ok and x.is_home_position_ok,
                     "global/home position estimate", 60.0)

    for setter in (drone.param.set_param_float, drone.param.set_param_int):
        try:
            await setter("MIS_DIST_1WP", 0.0 if setter == drone.param.set_param_float else 0)
            break
        except Exception:
            pass

    try:
        await drone.param.set_param_float("SIM_GZ_HOME_LAT", float(points[0][0]))
        await drone.param.set_param_float("SIM_GZ_HOME_LON", float(points[0][1]))
    except Exception:
        pass

    nan = float("nan")
    va_none = getattr(getattr(MissionItem, "VehicleAction", None), "NONE", None)
    items = []
    for index, (lat, lon, alt) in enumerate(points):
        kwargs = dict(
            latitude_deg=lat, longitude_deg=lon, relative_altitude_m=alt,
            speed_m_s=speed_profile[index], is_fly_through=True,
            gimbal_pitch_deg=nan, gimbal_yaw_deg=nan,
            camera_action=MissionItem.CameraAction.NONE,
            loiter_time_s=nan, camera_photo_interval_s=nan,
            acceptance_radius_m=nan, yaw_deg=nan, camera_photo_distance_m=nan,
        )
        if va_none is not None:
            kwargs["vehicle_action"] = va_none
        try:
            items.append(MissionItem(**kwargs))
        except TypeError:
            kwargs.pop("vehicle_action", None)
            items.append(MissionItem(**kwargs))
    await drone.mission.clear_mission()
    await drone.mission.upload_mission(MissionPlan(items))
    await asyncio.sleep(2.0)

    for attempt in range(5):
        try:
            await drone.action.arm()
            await drone.mission.start_mission()
            break
        except Exception:
            if attempt == 4:
                raise
            await asyncio.sleep(2.0)

    start = time.monotonic()
    epoch_start = time.time()
    stop_logging = asyncio.Event()
    fields = ["timestamp_s", "timestamp_epoch_s", "elapsed_s", "latitude", "longitude",
              "altitude_relative_m", "speed_m_s", "distance_to_gcs_m", "flight_mode",
              "mission_current", "mission_total", "is_in_air"]
    async def log_telemetry():
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            f.flush()
            position_stream = drone.telemetry.position().__aiter__()
            mode_stream = drone.telemetry.flight_mode().__aiter__()
            position_q = asyncio.create_task(anext(position_stream))
            mode_q = asyncio.create_task(anext(mode_stream))
            progress_stream = drone.mission.mission_progress().__aiter__()
            in_air_stream = drone.telemetry.in_air().__aiter__()
            progress_q = asyncio.create_task(anext(progress_stream))
            in_air_q = asyncio.create_task(anext(in_air_stream))
            last_mode = "UNKNOWN"
            mission_current, mission_total = 0, len(points)
            is_in_air = False
            previous = None
            try:
                while not stop_logging.is_set():
                    done, _ = await asyncio.wait((position_q, mode_q, progress_q, in_air_q), timeout=1.0,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if mode_q in done:
                        try:
                            last_mode = str(mode_q.result())
                        except StopAsyncIteration:
                            pass
                        mode_q = asyncio.create_task(anext(mode_stream))
                    if progress_q in done:
                        try:
                            progress = progress_q.result()
                            mission_current, mission_total = int(progress.current), int(progress.total)
                        except StopAsyncIteration:
                            pass
                        progress_q = asyncio.create_task(anext(progress_stream))
                    if in_air_q in done:
                        try:
                            is_in_air = bool(in_air_q.result())
                        except StopAsyncIteration:
                            pass
                        in_air_q = asyncio.create_task(anext(in_air_stream))
                    if position_q in done:
                        try:
                            position = position_q.result()
                        except StopAsyncIteration:
                            break
                        elapsed = time.monotonic() - start
                        now = epoch_start + elapsed
                        horizontal = math.hypot(math.radians(position.latitude_deg - args.gcs_latitude),
                                                math.cos(math.radians(args.gcs_latitude)) * math.radians(position.longitude_deg - args.gcs_longitude)) * 6371000.0
                        distance = math.hypot(horizontal, position.relative_altitude_m)
                        if previous is None or elapsed <= previous[0]:
                            speed = 0.0
                        else:
                            dlat = math.radians(position.latitude_deg - previous[1])
                            dlon = math.radians(position.longitude_deg - previous[2])
                            horizontal_step = 2 * 6371000.0 * math.asin(min(1.0, math.sqrt(
                                math.sin(dlat / 2) ** 2 + math.cos(math.radians(previous[1])) *
                                math.cos(math.radians(position.latitude_deg)) * math.sin(dlon / 2) ** 2)))
                            speed = math.hypot(horizontal_step, position.relative_altitude_m - previous[3]) / (elapsed - previous[0])
                        previous = (elapsed, position.latitude_deg, position.longitude_deg, position.relative_altitude_m)
                        writer.writerow({"timestamp_s": now, "timestamp_epoch_s": now, "elapsed_s": elapsed,
                                         "latitude": position.latitude_deg, "longitude": position.longitude_deg,
                                         "altitude_relative_m": position.relative_altitude_m,
                                         "speed_m_s": speed, "distance_to_gcs_m": distance,
                                         "flight_mode": last_mode, "mission_current": mission_current,
                                         "mission_total": mission_total, "is_in_air": int(is_in_air)})
                        f.flush()
                        position_q = asyncio.create_task(anext(position_stream))
            finally:
                for task in (position_q, mode_q, progress_q, in_air_q):
                    task.cancel()
    logger_task = asyncio.create_task(log_telemetry())
    try:
        async def mission_done():
            async for progress in drone.mission.mission_progress():
                if progress.total > 0 and progress.current >= progress.total:
                    return

        async def watch_premature_disarm():
            await asyncio.sleep(20.0)
            async for armed in drone.telemetry.armed():
                if not armed:
                    if done_task.done():
                        return
                    raise RuntimeError("Drone disarmed prematurely during flight")

        mission_timeout = max(1800.0, route_duration * 3.0, args.timeout_s * 3.0)
        done_task = asyncio.create_task(mission_done())
        abort_task = asyncio.create_task(watch_premature_disarm())
        done, pending = await asyncio.wait([done_task, abort_task], timeout=mission_timeout, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if not done:
            raise TimeoutError(f"Mission timed out after {mission_timeout:.1f}s before completing all waypoints")
        for t in done:
            t.result()
    finally:
        stop_logging.set()
        try:
            await asyncio.wait_for(logger_task, timeout=5)
        except asyncio.TimeoutError:
            logger_task.cancel()
    # Attempt RTL after mission completion; do not block or fail if SITL landing is delayed.
    try:
        await asyncio.wait_for(drone.action.return_to_launch(), timeout=10.0)
        await wait_until(drone.telemetry.in_air(), lambda in_air: not in_air, "landing after RTL", 20.0)
    except Exception as exc:
        print(f"Post-mission RTL/landing: {exc} (non-fatal, mission waypoints completed)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--route", required=True, help="P4 mission CSV")
    p.add_argument("--out", required=True, help="Live telemetry CSV")
    p.add_argument("--system-address", default="udp://:14550")
    p.add_argument("--altitude-offset-m", type=float, default=15.0)
    p.add_argument("--speed-m-s", type=float, default=5.0)
    p.add_argument("--speed-profile-m-s", default=None,
                   help="Comma-separated train-calibrated per-waypoint speed profile")
    p.add_argument("--altitude-profile-relative-m", default=None,
                   help="Comma-separated train-calibrated relative altitude profile, one value per waypoint")
    p.add_argument("--gcs-latitude", type=float, required=True)
    p.add_argument("--gcs-longitude", type=float, required=True)
    p.add_argument("--timeout-s", type=float, default=600.0)
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

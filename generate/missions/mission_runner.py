"""Mission execution and event hooks for Phase 1/2 campaigns."""
from __future__ import annotations

import asyncio
import json
import math
import time

from mavsdk import System

from ..collectors.mavsdk_collector import read_home


def local_xy_to_global(home: dict, north_m: float, east_m: float) -> tuple[float, float]:
    lat = home["latitude_deg"] + north_m / 111_111.0
    lon = home["longitude_deg"] + east_m / (111_111.0 * math.cos(math.radians(home["latitude_deg"])))
    return lat, lon

async def get_home_position(drone: System, fallback_home: dict) -> dict:
    """Retrieve actual home ground coordinates and AMSL altitude from telemetry, fallback to config."""
    try:
        return await read_home(drone, timeout_s=5.0)
    except Exception:
        return fallback_home

async def execute_mission(drone: System, cfg: dict, mission: dict, events, telemetry_cache: dict,
                          attack_manager=None) -> None:
    def cached_value(name: str):
        sample = telemetry_cache.get(name)
        if isinstance(sample, dict) and "value" in sample:
            return sample["value"]
        return sample

    async def event(name: str, **values):
        events.write(json.dumps({"timestamp_epoch_ns": time.time_ns(), "event": name, **values}) + "\n")
        events.flush()
        if attack_manager is not None:
            await attack_manager.observe(name, **values)

    # 1. Takeoff with altitude verification
    takeoff_alt = 10.0
    await drone.action.set_takeoff_altitude(takeoff_alt)

    # Allow PX4 preflight and health checks to settle before arming
    armed = False
    for attempt in range(15):
        try:
            await drone.action.arm()
            armed = True
            break
        except Exception:
            await asyncio.sleep(1.0)
    if not armed:
        await drone.action.arm()
    await event("arm")

    taken_off = False
    for attempt in range(6):
        try:
            await drone.action.takeoff()
            taken_off = True
            break
        except Exception:
            await asyncio.sleep(1.0)
    if not taken_off:
        await drone.action.takeoff()
    await event("takeoff")

    # Monitor actual takeoff altitude instead of hardcoded sleep
    t_start = time.monotonic()
    takeoff_stable = False
    while time.monotonic() - t_start < 30:
        pos = cached_value("position")
        if pos and isinstance(pos, dict):
            rel_alt = pos.get("relative_altitude_m")
            if rel_alt is not None and rel_alt >= (takeoff_alt - 1.0):
                takeoff_stable = True
                break
        await asyncio.sleep(0.5)
    if not takeoff_stable:
        await event("takeoff_timeout", timeout_s=30)
        raise TimeoutError("Vehicle did not reach the takeoff altitude")
    await event("takeoff_stabilized")

    # 2. Query ground home position
    fallback_home = cfg["campaign"].get("home", {"latitude_deg": 47.397742, "longitude_deg": 8.545594, "altitude_m": 488.0})
    home = await get_home_position(drone, fallback_home)
    await event("home_position", **home)

    # 3. Speed configuration
    speed = mission.get("speed_m_s")
    if speed:
        await drone.action.set_maximum_speed(float(speed))

    # 4. Waypoint navigation with non-blocking checks and auto-yaw
    waypoints = mission.get("waypoints", [])
    loops = int(mission.get("loops", 1))
    wp_hover_s = float(mission.get("waypoint_hover_s", 0))
    wp_timeout_s = float(mission.get("waypoint_timeout_s", 50))

    if waypoints:
        for loop_idx in range(1, loops + 1):
            for wp_idx, (north, east, rel_alt) in enumerate(waypoints, 1):
                lat, lon = local_xy_to_global(home, float(north), float(east))
                target_amsl = home["altitude_m"] + float(rel_alt)

                # Use float('nan') so PX4 auto-yaws towards the waypoint naturally
                await drone.action.goto_location(lat, lon, target_amsl, float("nan"))
                await event("waypoint_start", loop=loop_idx, waypoint=wp_idx,
                            north_m=north, east_m=east, relative_altitude_m=rel_alt)

                deadline = time.monotonic() + wp_timeout_s
                reached = False
                while time.monotonic() < deadline:
                    pos = cached_value("position")
                    if pos and isinstance(pos, dict):
                        plat = pos.get("latitude_deg")
                        plon = pos.get("longitude_deg")
                        palt = pos.get("relative_altitude_m")
                        if plat is not None and plon is not None and palt is not None:
                            dlat = math.radians(plat - lat)
                            dlon = math.radians(plon - lon)
                            hav = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat)) * math.cos(math.radians(plat)) * math.sin(dlon / 2) ** 2
                            h_err = 6_371_000 * 2 * math.asin(math.sqrt(min(1.0, hav)))
                            alt_err = abs(palt - float(rel_alt))
                            # Tightened altitude threshold to 1.0m
                            if h_err < 3.0 and alt_err < 1.0:
                                reached = True
                                break
                    await asyncio.sleep(0.2)

                if reached:
                    await event("waypoint_reached", loop=loop_idx, waypoint=wp_idx)
                else:
                    await event("waypoint_timeout", loop=loop_idx, waypoint=wp_idx)

                if wp_hover_s > 0:
                    await asyncio.sleep(wp_hover_s)

    # 5. Mission hover if defined
    hover = int(mission.get("hover_s", 0))
    if hover > 0:
        await event("hover_start", duration_s=hover)
        await asyncio.sleep(hover)
        await event("hover_end")

    # 6. Retry landing once; never report landed without confirmation.
    if attack_manager is not None:
        await attack_manager.verify_before_land()
    land_command_mono = time.monotonic_ns()
    await drone.action.land()
    await event("land")

    async def wait_landed(timeout_s: float, not_before_mono_ns: int):
        deadline = time.monotonic() + timeout_s
        landed_since = None
        while time.monotonic() < deadline:
            sample = telemetry_cache.get("in_air", {})
            received = sample.get("received_mono_ns", 0) if isinstance(sample, dict) else 0
            value = sample.get("value") if isinstance(sample, dict) and "value" in sample else sample
            if received >= not_before_mono_ns and value is False:
                landed_since = landed_since or time.monotonic()
                if time.monotonic() - landed_since >= 1.0:
                    return
            else:
                landed_since = None
            await asyncio.sleep(0.2)
        raise asyncio.TimeoutError

    try:
        await wait_landed(35.0, land_command_mono)
        await event("landed")
        await event("mission_end", vehicle_landed=True, outcome="landed")
    except asyncio.TimeoutError:
        await event("landing_timeout", action="retry_land")
        try:
            retry_land_mono = time.monotonic_ns()
            await drone.action.land()
            await wait_landed(20.0, retry_land_mono)
            await event("landed", after_retry=True)
            await event("mission_end", vehicle_landed=True, outcome="landed_after_retry")
        except asyncio.TimeoutError:
            await event("landing_unconfirmed", action="disarm_fallback")
            try:
                await drone.action.disarm()
            except Exception as exc:
                await event("disarm_failed", error=str(exc))
            await event("mission_end", vehicle_landed=False, outcome="landing_unconfirmed")
            raise TimeoutError("Vehicle landing could not be confirmed after retry")

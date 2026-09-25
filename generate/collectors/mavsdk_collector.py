"""MAVSDK persistent telemetry streams and sampled JSONL output."""
from __future__ import annotations

import asyncio
from enum import Enum
import json
import math
import time
from pathlib import Path

from mavsdk import System

TELEMETRY_STREAMS = ("position", "velocity_ned", "attitude_euler", "battery", "flight_mode", "in_air")


def telemetry_value(value):
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, Enum):
        return value.name
    enum_name = getattr(value, "name", None)
    if isinstance(enum_name, str):
        return enum_name
    if isinstance(value, (str, int, bool)):
        return value
    if hasattr(value, "ListFields"):
        return {field.name: telemetry_value(item) for field, item in value.ListFields()}
    if hasattr(value, "__dataclass_fields__"):
        return {name: telemetry_value(getattr(value, name)) for name in value.__dataclass_fields__}
    if hasattr(value, "__dict__"):
        return {key: telemetry_value(item) for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)


async def wait_for_connection(drone: System, timeout_s: int) -> None:
    async def wait():
        async for state in drone.core.connection_state():
            if state.is_connected:
                return
    await asyncio.wait_for(wait(), timeout=timeout_s)


async def read_home(drone: System, timeout_s: float = 30.0) -> dict:
    async def read_once():
        stream = drone.telemetry.home()
        try:
            async for value in stream:
                return value
        finally:
            close = getattr(stream, "aclose", None)
            if close:
                await close()
    home = await asyncio.wait_for(read_once(), timeout=timeout_s)
    return {"latitude_deg": home.latitude_deg, "longitude_deg": home.longitude_deg,
            "altitude_m": home.absolute_altitude_m}


async def _consume_stream(name: str, factory, cache: dict, stop: asyncio.Event) -> None:
    while not stop.is_set():
        stream = None
        try:
            stream = factory()
            async for value in stream:
                cache[name] = {"value": telemetry_value(value), "received_mono_ns": time.monotonic_ns()}
                if stop.is_set():
                    break
            if not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            old = cache.get(name, {})
            cache[name] = {"value": old.get("value"), "received_mono_ns": old.get("received_mono_ns"),
                           "error": str(exc)}
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        finally:
            close = getattr(stream, "aclose", None)
            if close:
                try:
                    await close()
                except Exception:
                    pass


async def _wait_initial_samples(cache: dict, stop: asyncio.Event,
                                minimum_warmup_s: float = 0.5,
                                timeout_s: float = 5.0) -> None:
    required = set(TELEMETRY_STREAMS)
    started = time.monotonic()
    deadline = started + timeout_s
    while time.monotonic() < deadline:
        if stop.is_set():
            raise asyncio.CancelledError
        ready = all(
            name in cache and cache[name].get("received_mono_ns") is not None
            and cache[name].get("value") is not None
            for name in required
        )
        if ready and time.monotonic() - started >= minimum_warmup_s:
            return
        await asyncio.sleep(0.05)
    missing = [name for name in required if cache.get(name, {}).get("value") is None]
    raise TimeoutError(f"Telemetry warm-up timed out; missing initial streams: {', '.join(sorted(missing))}")


async def collect_telemetry(drone: System, path: Path, stop: asyncio.Event, rate_hz: float,
                            cache: dict, ready: asyncio.Event | None = None,
                            start: asyncio.Event | None = None) -> None:
    factories = {
        "position": drone.telemetry.position,
        "velocity_ned": drone.telemetry.velocity_ned,
        "attitude_euler": drone.telemetry.attitude_euler,
        "battery": drone.telemetry.battery,
        "flight_mode": drone.telemetry.flight_mode,
        "in_air": drone.telemetry.in_air,
    }
    tasks = [asyncio.create_task(_consume_stream(name, factory, cache, stop))
             for name, factory in factories.items()]
    try:
        await _wait_initial_samples(cache, stop)
        if ready is not None:
            ready.set()
        if start is not None:
            await start.wait()
        with path.open("a", encoding="utf-8", buffering=1) as f:
            while not stop.is_set():
                now = time.monotonic_ns()
                row = {"timestamp_epoch_ns": time.time_ns(), "timestamp_mono_ns": now}
                for name in TELEMETRY_STREAMS:
                    sample = cache.get(name, {})
                    row[name] = sample.get("value")
                    received = sample.get("received_mono_ns")
                    row[f"{name}_age_ms"] = (now - received) / 1e6 if received is not None else None
                f.write(json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0 / rate_hz)
                except asyncio.TimeoutError:
                    pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

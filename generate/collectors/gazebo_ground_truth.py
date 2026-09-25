"""Capture raw Gazebo Transport topic output as independent evaluation truth.

The Gazebo CLI text schema differs by version. This collector deliberately
preserves raw topic lines plus host timestamps; normalization belongs to Phase 3.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path


async def collect_topic(topic: str, path: Path, stop: asyncio.Event,
                        first_sample: asyncio.Event | None = None,
                        executable: str = "gz") -> None:
    if not topic:
        raise ValueError("Set campaign.attack_policy.ground_truth_topic after `gz topic -l` discovery")
    proc = await asyncio.create_subprocess_exec(
        executable, "topic", "-e", "-t", topic,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        with path.open("a", encoding="utf-8", buffering=1) as output:
            while not stop.is_set():
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=0.5)
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        raise RuntimeError(f"gz topic exited with status {proc.returncode}")
                    continue
                if not line:
                    if proc.returncode is None:
                        continue
                    raise RuntimeError(f"gz topic ended with status {proc.returncode}")
                output.write(json.dumps({"received_epoch_ns": time.time_ns(),
                                         "received_mono_ns": time.monotonic_ns(),
                                         "topic": topic,
                                         "raw": line.decode("utf-8", errors="replace").rstrip("\r\n")},
                                        separators=(",", ":")) + "\n")
                if first_sample is not None:
                    first_sample.set()
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        await asyncio.sleep(0.1)

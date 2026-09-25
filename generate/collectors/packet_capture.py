"""Packet capture and isolated process-group cleanup."""
from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
from shutil import which
from pathlib import Path


def shutil_which(command: str) -> str | None:
    return which(command)


def start_capture(out: Path, capture_filter: str, interface: str = "any") -> subprocess.Popen:
    log_file = out.with_name("capture.log").open("w", encoding="utf-8")
    if shutil_which("dumpcap"):
        cmd = ["dumpcap", "-i", interface, "-w", str(out)]
        if capture_filter:
            cmd.extend(["-f", capture_filter])
    elif shutil_which("tcpdump"):
        # tcpdump writes classic pcap; the caller uses a matching .pcap extension.
        cmd = ["tcpdump", "-i", interface, "-nn", "-U", "-s", "0", "-w", str(out)]
        if capture_filter:
            cmd.extend(shlex.split(capture_filter))
    else:
        log_file.close()
        raise RuntimeError("Install dumpcap (recommended for pcapng) or tcpdump in WSL")
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_file,
                                start_new_session=True)
    finally:
        log_file.close()


async def stop_process_group(proc: subprocess.Popen | None, interrupt_timeout: float = 12.0) -> None:
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=interrupt_timeout)
    except asyncio.TimeoutError:
        pass
    # The process-group leader can exit before child processes. Escalate against
    # this run's isolated session, never by broad process-name matching.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    await asyncio.sleep(1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if proc.poll() is None:
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            pass
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

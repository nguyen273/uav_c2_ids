"""MAVLink datagram injectors for an explicitly loopback SITL relay."""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque

from .base import BaseAttack, AttackNotImplemented
from .proxy import encode_message, mavlink_messages


class ProxyAttack(BaseAttack):
    def __init__(self, context):
        super().__init__(context)
        self.proxy = context.metadata.get("proxy")
        if self.proxy is None:
            raise AttackNotImplemented("Attack requires the validated loopback MAVLink proxy")
        self.task = None

    async def start(self):
        self.previous_transform = self.proxy.transform
        self.proxy.transform = self.transform
        self.started = True

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.proxy and self.started:
            self.proxy.transform = self.previous_transform
        self.started = False

    def transform(self, packet: bytes, direction: str):
        return packet


class FDIInjector(ProxyAttack):
    """Alter downlink-only GCS telemetry; never changes PX4/uORB state."""
    def __init__(self, context, variant: str):
        super().__init__(context)
        self.variant = variant
        self.started_at = time.monotonic()
        self.modified = 0
        self.seen = 0
        self.rng = random.Random(context.run_id)

    def transform(self, packet: bytes, direction: str):
        if direction != "px4_to_gcs":
            return packet
        try:
            messages = mavlink_messages(packet)
            if not messages:
                return packet
            out = []
            for msg in messages:
                kind = msg.get_type()
                self.seen += 1
                if self.variant == "GCS_STATE_SPOOF" and kind == "EXTENDED_SYS_STATE":
                    pct = min(100.0, max(0.0, float(self.context.parameters.get("modified_message_pct", 100))))
                    if self.rng.random() * 100.0 >= pct:
                        out.append(encode_message(msg))
                        continue
                    # Landed-state enum 1 is ON_GROUND. This affects GCS-visible state only.
                    msg.landed_state = 1
                    self.modified += 1
                elif self.variant == "GCS_POSITION_DRIFT" and kind == "GLOBAL_POSITION_INT":
                    p = self.context.parameters
                    ramp = max(0.0, float(p.get("drift_rate_m_s", 0.05))) * (time.monotonic() - self.started_at)
                    err = min(float(p.get("max_error_m", 0.5)), ramp)
                    # Apply northward display drift to latitude only (1 deg ~= 111.1 km).
                    msg.lat = int(msg.lat + (err / 111_111.0) * 1e7)
                    self.modified += 1
                out.append(encode_message(msg))
            return b"".join(out) if out else packet
        except Exception:
            # Preserve valid original traffic if a frame cannot be parsed/re-encoded.
            return packet


class FloodInjector(ProxyAttack):
    """Repeat the latest observed downlink datagram at the configured extra rate."""
    def async_init(self):
        self.latest = None
        self.history = deque(maxlen=256)

    async def start(self):
        self.async_init()
        await super().start()
        self.task = asyncio.create_task(self._flood())

    def transform(self, packet: bytes, direction: str):
        if direction == "px4_to_gcs":
            self.latest = bytes(packet)
            self.history.append(bytes(packet))
        return packet

    async def _flood(self):
        rate = max(0.1, float(self.context.parameters.get("extra_packets_per_s", 50)))
        interval = 1.0 / rate
        while True:
            await asyncio.sleep(interval)
            if self.latest:
                await self.proxy.inject(self.latest, "px4_to_gcs")


class ReplayInjector(FloodInjector):
    """Replay captured downlink datagrams; never replays uplink commands."""
    async def _flood(self):
        rate = max(0.1, float(self.context.parameters.get("replay_packets_per_s", 5)))
        interval = 1.0 / rate
        replay_idx = 0
        while True:
            await asyncio.sleep(interval)
            if self.history:
                history = tuple(self.history)
                await self.proxy.inject(history[replay_idx % len(history)], "px4_to_gcs")
                replay_idx += 1


class DegradationInjector(ProxyAttack):
    """Deterministic delay/loss applied only while the controller is active."""
    def __init__(self, context):
        super().__init__(context)
        self.rng = random.Random(context.run_id)
        self.delay_s = max(0.0, float(context.parameters.get("delay_ms", 0))) / 1000.0
        self.loss_pct = min(100.0, max(0.0, float(context.parameters.get("loss_pct", 0))))

    def transform(self, packet: bytes, direction: str):
        if self.rng.random() * 100 < self.loss_pct:
            return None
        if self.delay_s:
            self.proxy.schedule_delay(self.delay_s)
        return packet


class AnomalyInjector(ProxyAttack):
    """Inject invalid MAVLink frames by corrupting checksum bytes, preserving size."""
    def __init__(self, context):
        super().__init__(context)
        self.rng = random.Random(context.run_id)
        self.rate = min(100.0, max(0.0, float(context.parameters.get("modified_packet_pct", 1))))

    def transform(self, packet: bytes, direction: str):
        if not packet or self.rng.random() * 100 >= self.rate:
            return packet
        data = bytearray(packet)
        # Corrupt checksum of the first complete MAVLink 1 or 2 frame.
        start = next((i for i, b in enumerate(data) if b in (0xFE, 0xFD)), None)
        if start is None:
            return packet
        v1 = data[start] == 0xFE
        header = 6 if v1 else 10
        payload_len = data[start + 1]
        checksum = start + header + payload_len
        if len(data) > checksum:
            data[checksum] ^= 0x01
            return bytes(data)
        return packet


class ShadowGCSInjector(ProxyAttack):
    """Transparent MITM relay placeholder; active control commands are not guessed."""
    async def start(self):
        raise AttackNotImplemented(
            "SHADOW_GCS_MITM requires a scenario-defined, bounded command and validated PX4 mode mapping"
        )


def factory(cls, *args):
    return lambda context: cls(context, *args)

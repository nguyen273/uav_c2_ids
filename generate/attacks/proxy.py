"""Loopback-only UDP relay used by SITL attack controllers.

The relay binds two local UDP ports: GCS-facing and PX4-facing. PX4 must be
configured to send its MAVLink UDP output to ``px4_bind_port``; MAVSDK must
connect to ``gcs_bind_port``. It refuses non-loopback addresses by design.
"""
from __future__ import annotations

import asyncio
import random
import socket
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class Datagram:
    data: bytes
    addr: tuple[str, int]
    direction: str


class _Endpoint(asyncio.DatagramProtocol):
    def __init__(self, relay: "LoopbackMavlinkProxy", direction: str):
        self.relay = relay
        self.direction = direction
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        self.relay.transports[self.direction] = transport

    def datagram_received(self, data: bytes, addr):
        asyncio.create_task(self.relay.forward(data, addr, self.direction))

    def error_received(self, exc):
        self.relay.errors.append(str(exc))


class LoopbackMavlinkProxy:
    def __init__(self, gcs_port: int, px4_port: int, *, host: str = "127.0.0.1",
                 px4_host: str = "127.0.0.1", px4_target_port: int,
                 gcs_relay_port: int = 14546,
                 transform: Callable[[bytes, str], bytes | None] | None = None,
                 delay_ms: float = 0, loss_pct: float = 0, seed: int = 0):
        if host not in {"127.0.0.1", "::1", "localhost"} or px4_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("SITL attack proxy only permits loopback hosts")
        self.host, self.px4_host = host, px4_host
        self.gcs_port = int(gcs_port)
        self.px4_port = int(px4_port)
        self.px4_target_port = int(px4_target_port)
        self.gcs_relay_port = int(gcs_relay_port)
        self.transform = transform or (lambda packet, direction: packet)
        self.delay_s = max(0.0, float(delay_ms) / 1000.0)
        self.loss_pct = min(100.0, max(0.0, float(loss_pct)))
        self.random = random.Random(seed)
        self.transports: dict[str, asyncio.DatagramTransport] = {}
        self.tasks: set[asyncio.Task] = set()
        self.errors: list[str] = []
        self.gcs_peer: tuple[str, int] = (self.host, self.gcs_port)
        self.scheduled_delay_s = 0.0

    async def start(self):
        loop = asyncio.get_running_loop()
        for direction, port in (("gcs_to_px4", self.gcs_relay_port), ("px4_to_gcs", self.px4_port)):
            await loop.create_datagram_endpoint(lambda d=direction: _Endpoint(self, d), local_addr=(self.host, port))

    async def forward(self, packet: bytes, addr, direction: str):
        if direction == "gcs_to_px4":
            self.gcs_peer = (addr[0], addr[1])
            target, outbound = (self.px4_host, self.px4_target_port), "px4_to_gcs"
        else:
            target, outbound = self.gcs_peer, "gcs_to_px4"
        if target is None:
            return
        if self.random.random() * 100.0 < self.loss_pct:
            return
        modified = self.transform(bytes(packet), direction)
        if modified is None:
            return
        delay_s = self.scheduled_delay_s or self.delay_s
        self.scheduled_delay_s = 0.0
        if delay_s:
            await asyncio.sleep(delay_s)
        transport = self.transports.get(outbound)
        if transport:
            transport.sendto(modified, target)

    def schedule_delay(self, delay_s: float):
        self.scheduled_delay_s = max(self.scheduled_delay_s, max(0.0, float(delay_s)))

    async def inject(self, packet: bytes, direction: str):
        if direction != "px4_to_gcs" or self.gcs_peer is None:
            return
        # Send via the GCS-facing socket so the receiver sees the configured proxy endpoint.
        transport = self.transports.get("gcs_to_px4")
        if transport:
            transport.sendto(bytes(packet), self.gcs_peer)

    async def close(self):
        for task in tuple(self.tasks):
            task.cancel()
        for transport in self.transports.values():
            transport.close()
        self.transports.clear()


def mavlink_messages(packet: bytes):
    """Parse all MAVLink messages in a UDP payload, preserving message order."""
    from pymavlink.dialects.v20 import common as mavlink2
    parser = mavlink2.MAVLink(None)
    parser.robust_parsing = True
    result = []
    for byte in packet:
        msg = parser.parse_char(bytes((byte,)))
        if msg is not None and not isinstance(msg, str):
            result.append(msg)
    return result


def encode_message(msg) -> bytes:
    from pymavlink.dialects.v20 import common as mavlink2
    encoder = mavlink2.MAVLink(None)
    encoder.srcSystem = getattr(msg, "_header", None).srcSystem if getattr(msg, "_header", None) else 1
    encoder.srcComponent = getattr(msg, "_header", None).srcComponent if getattr(msg, "_header", None) else 1
    return msg.pack(encoder)

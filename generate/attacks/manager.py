"""Event-driven attack lifecycle; refuses incomplete attack implementations."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Callable

from .base import AttackContext, AttackNotImplemented, BaseAttack


class AttackManager:
    def __init__(self, context: AttackContext, controller_factory: Callable,
                 duration_s: float, eligible_events: list[str], seed: int,
                 require_full_duration: bool = True, start_after_event: str = "takeoff_stabilized",
                 min_before_land_s: float = 10.0, max_trigger_occurrence: int = 1):
        self.context = context
        self.controller_factory = controller_factory
        self.duration_s = float(duration_s)
        self.eligible_events = set(eligible_events)
        self.require_full_duration = require_full_duration
        self.start_after_event = start_after_event
        self.min_before_land_s = float(min_before_land_s)
        self._start_gate_seen = False
        self.random = random.Random(seed)
        self.target_event = self.random.choice(sorted(self.eligible_events)) if self.eligible_events else None
        self.target_occurrence = self.random.randint(1, max(1, int(max_trigger_occurrence)))
        self.event_counts: dict[str, int] = {}
        self.controller: BaseAttack | None = None
        self.timer_task: asyncio.Task | None = None
        self.selected_event: str | None = None
        self.start_mono: float | None = None
        self.end_mono: float | None = None
        self.started_once = False
        self._lock = asyncio.Lock()

    async def observe(self, event_name: str, **values) -> None:
        if event_name == self.start_after_event:
            self._start_gate_seen = True
        if not self._start_gate_seen:
            return
        if event_name in self.eligible_events:
            self.event_counts[event_name] = self.event_counts.get(event_name, 0) + 1
        if (event_name != self.target_event or
                self.event_counts.get(event_name) != self.target_occurrence or
                self.controller is not None or self.started_once):
            return
        async with self._lock:
            if self.controller is not None:
                return
            candidate = self.controller_factory(self.context)
            if not isinstance(candidate, BaseAttack):
                raise AttackNotImplemented("Controller factory did not return a BaseAttack")
            self.controller = candidate
            self.selected_event = event_name
            self.start_mono = time.monotonic()
            self.started_once = True
            try:
                await candidate.start()
            except BaseException:
                self.controller = None
                try:
                    await candidate.stop()
                except Exception:
                    pass
                await self.context.emit_event("attack_error", phase="start")
                raise
            candidate.started = True
            await self.context.emit_event("attack_started", attack_type=self.context.attack_type,
                                          attack_variant=self.context.attack_variant,
                                          severity=self.context.severity,
                                          trigger_event=event_name,
                                          trigger_occurrence=self.target_occurrence, **values)
            self.timer_task = asyncio.create_task(self._duration_timer())

    async def _duration_timer(self) -> None:
        await asyncio.sleep(self.duration_s)
        await self.stop(reason="duration_elapsed")

    async def stop(self, reason: str, *, before_land: bool = False) -> dict:
        async with self._lock:
            if self.controller is None:
                return {"active": False, "full_duration": False}
            controller = self.controller
            self.controller = None
            if self.timer_task and self.timer_task is not asyncio.current_task():
                self.timer_task.cancel()
            stop_error = None
            try:
                await controller.stop()
            except Exception as exc:
                stop_error = exc
            finally:
                self.end_mono = time.monotonic()
            elapsed = max(0.0, self.end_mono - (self.start_mono or self.end_mono))
            full_duration = elapsed + 0.05 >= self.duration_s
            await self.context.emit_event("attack_stopped", attack_type=self.context.attack_type,
                                          attack_variant=self.context.attack_variant,
                                          severity=self.context.severity, reason=reason,
                                          actual_duration_s=round(elapsed, 3),
                                          full_duration=full_duration,
                                          cleanup_error=str(stop_error) if stop_error else "")
            if stop_error:
                raise stop_error
            if before_land and self.require_full_duration and not full_duration:
                raise RuntimeError("Mission reached landing before the configured attack duration completed")
            return {"active": False, "actual_duration_s": elapsed,
                    "full_duration": full_duration, "trigger_event": self.selected_event}

    async def verify_before_land(self) -> None:
        if self.start_mono is None and self.require_full_duration:
            raise RuntimeError("No eligible mission event started the required attack")
        if self.controller is not None:
            await self.stop(reason="pre_land", before_land=True)
        if self.require_full_duration and self.start_mono is not None:
            if self.end_mono is None or time.monotonic() - self.end_mono < self.min_before_land_s:
                raise RuntimeError("Attack did not finish at least the configured margin before landing")

    async def close(self) -> None:
        if self.timer_task and not self.timer_task.done():
            self.timer_task.cancel()
            await asyncio.gather(self.timer_task, return_exceptions=True)
        await self.stop(reason="run_cleanup")

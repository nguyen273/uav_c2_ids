"""Contracts shared by Phase 2 attack controllers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AttackContext:
    run_id: str
    attack_type: str
    attack_variant: str
    severity: str
    parameters: dict[str, Any]
    emit_event: Any
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseAttack(ABC):
    """A controller must own and clean up only resources created for its run."""

    def __init__(self, context: AttackContext):
        self.context = context
        self.started = False

    @abstractmethod
    async def start(self) -> None:
        """Start injection and return after the controller is active."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop injection and release per-run resources idempotently."""


class AttackNotImplemented(RuntimeError):
    pass

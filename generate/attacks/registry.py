"""Explicit registry for implemented SITL datagram controllers."""
from .base import AttackNotImplemented
from .injectors import (
    AnomalyInjector,
    DegradationInjector,
    FDIInjector,
    FloodInjector,
    ReplayInjector,
    ShadowGCSInjector,
)

CONTROLLERS = {
    ("FLOOD", "FLOOD"): FloodInjector,
    ("REPLAY", "REPLAY"): ReplayInjector,
    ("FDI", "GCS_STATE_SPOOF"): lambda ctx: FDIInjector(ctx, "GCS_STATE_SPOOF"),
    ("FDI", "GCS_POSITION_DRIFT"): lambda ctx: FDIInjector(ctx, "GCS_POSITION_DRIFT"),
    ("C2_DEGRADATION", "C2_DEGRADATION"): DegradationInjector,
    ("MAVLINK_ANOMALY", "MAVLINK_ANOMALY"): AnomalyInjector,
    # Controller intentionally fails closed until an exact, bounded command is configured.
    ("SHADOW_GCS_MITM", "SHADOW_GCS_MITM"): ShadowGCSInjector,
}


def get_controller_factory(attack_type: str, variant: str):
    key = (attack_type, variant)
    try:
        return CONTROLLERS[key]
    except KeyError as exc:
        raise AttackNotImplemented(f"No controller registered for {key}") from exc


def validate_controller(attack_type: str, variant: str) -> None:
    """Validate selection without creating a controller or touching a vehicle."""
    get_controller_factory(attack_type, variant)

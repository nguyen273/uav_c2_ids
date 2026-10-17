"""Scenario-level network impairment adapter.

Requires Dataset-32 as the selected Layer 1 fit source, but does not transfer
its 60 m AGL propagation slope into the differently configured SITL route.
Delay/loss/rate remain declared stress assumptions, not measurements or an
RF-to-network calibration.
"""
import json
import logging
from pathlib import Path
import random

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("P5_Link_Scenario")


class LinkImpairmentModel:
    # Explicit scenario values in ms, percent, Mbps. Not radio-calibrated.
    PROFILES = {
        "C1_LOS": (8.0, 0.2, 20.0),
        "C2_Moderate": (30.0, 2.0, 8.0),
        "C3_NLOS": (100.0, 10.0, 2.0),
        "C4_Dynamic": (8.0, 0.2, 20.0),
    }

    def __init__(self, model_file, frequency_ghz, seed=42):
        path = Path(model_file)
        if not path.is_file():
            raise FileNotFoundError(f"Dataset-32 Layer 1 model file not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        kind = data.get("model_type")
        if kind != "dataset32_flight_fixed_effects_log_distance_path_loss_slope":
            raise ValueError(f"Unsupported Layer-1 model type: {kind!r}")
        if abs(float(data.get("frequency_ghz", -1)) - float(frequency_ghz)) >= 0.005:
            raise ValueError(f"Dataset-32 Layer-1 fit supports 3.4 GHz only, not {frequency_ghz} GHz")
        self.frequency_ghz = float(frequency_ghz)
        self.model_type = kind
        self.n = float(data["path_loss_exponent_n"])
        if not np.isfinite(self.n) or not 0 <= self.n <= 6:
            raise ValueError(f"Invalid propagation exponent n={self.n}")
        self.rng = random.Random(int(seed))
        logger.warning("Dataset-32 slope is not transferred to SITL qdisc: target route lacks matched RF/altitude/link-budget conditions; Layer-3 profiles remain stress assumptions")

    def get_tc_params(self, distance_m, channel_state="C1_LOS"):
        if channel_state not in self.PROFILES:
            raise ValueError(f"Unknown channel scenario: {channel_state}")
        d = float(distance_m)
        if not np.isfinite(d) or d < 0:
            raise ValueError(f"Invalid UAV-to-GCS distance: {distance_m!r}")
        # Dataset-32 is measured at ~60 m AGL with LW1 and a specific sounder.
        # SITL currently has no matched RF link budget/altitude, so using its
        # fitted slope to change qdisc delay/loss/rate would imply unsupported
        # RF-to-network transfer. Distance is retained for interface/audit only.
        delay0, loss0, rate0 = self.PROFILES[channel_state]
        if channel_state == "C4_Dynamic" and self.rng.random() < 0.5:
            delay0, loss0, rate0 = self.PROFILES["C3_NLOS"]
        return float(delay0), float(loss0), float(rate0)

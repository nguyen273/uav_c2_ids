"""Build packet, flow, and telemetry-aligned time-window views from SITL runs.

Packets are captured MAVLink UDP transport traffic. Event labels are derived
from declared attack intervals and source IP; the current DoS is a UDP flood,
not MAVLink payload mutation or validated flight-control compromise.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd
import numpy as np

try:
    from scapy.all import rdpcap, IP, UDP, TCP
except ImportError:
    rdpcap = IP = UDP = TCP = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("P9_Schema")
ATTACK_CLASS = {"T1_Replay": 1, "T2_FDI": 2, "T4_DoS": 3}


def read_events(path):
    events = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            event = json.loads(line)
            start, end = float(event["start_epoch_s"]), float(event["end_epoch_s"])
            if not start < end or not event.get("src_ip"):
                raise ValueError(f"Invalid attack event at {path}:{line_no}")
            if event.get("label") == 1 and event.get("attack_id") not in ATTACK_CLASS:
                raise ValueError(f"Unknown attack class in {path}:{line_no}: {event.get('attack_id')!r}")
            events.append(event)
    return events


def extract_pcap(pcap_path, events):
    if rdpcap is None:
        raise RuntimeError("Scapy is required to parse PCAP")
    packets = rdpcap(str(pcap_path))
    records = []
    for pkt in packets:
        if IP not in pkt:
            continue
        ts, src, dst = float(pkt.time), pkt[IP].src, pkt[IP].dst
        active = [e for e in events if e.get("label") == 1 and e["src_ip"] == src and e["start_epoch_s"] <= ts <= e["end_epoch_s"]]
        attack_class = ATTACK_CLASS[active[0]["attack_id"]] if active else 0
        records.append({"timestamp_s": ts, "src_ip": src, "dst_ip": dst,
                        "ip_protocol": int(pkt[IP].proto), "length_bytes": len(pkt),
                        "src_port": int(pkt[UDP].sport if UDP in pkt else pkt[TCP].sport if TCP in pkt else 0),
                        "dst_port": int(pkt[UDP].dport if UDP in pkt else pkt[TCP].dport if TCP in pkt else 0),
                        "attack_label": int(attack_class > 0), "attack_class": int(attack_class)})
    if not records:
        raise ValueError(f"No IP packets found in {pcap_path}")
    df = pd.DataFrame(records).sort_values("timestamp_s")
    df["datetime"] = pd.to_datetime(df["timestamp_s"], unit="s", utc=True)
    flow_cols = ["src_ip", "dst_ip", "ip_protocol", "src_port", "dst_port"]
    df["flow_key"] = df[flow_cols].astype(str).agg("|".join, axis=1)
    df["interarrival_ms"] = df.groupby(flow_cols, sort=False)["timestamp_s"].diff() * 1000.0
    return df.set_index("datetime")


def load_telemetry(path):
    if not path.is_file():
        raise FileNotFoundError(f"Missing telemetry: {path}")
    df = pd.read_csv(path)
    required = {"timestamp_s", "latitude", "longitude", "altitude_relative_m", "speed_m_s", "distance_to_gcs_m"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Telemetry missing columns: {sorted(missing)}")
    df["datetime"] = pd.to_datetime(df["timestamp_s"], unit="s", utc=True)
    return df.sort_values("datetime").set_index("datetime")


def build_views(run_dir):
    run = Path(run_dir)
    meta = json.loads((run / "metadata.json").read_text(encoding="utf-8"))
    if meta.get("status") != "complete":
        raise ValueError(f"Run {run.name} is not marked complete")
    events = read_events(run / "attack_events.jsonl")
    packets = extract_pcap(run / "c2_traffic.pcap", events)
    telemetry = load_telemetry(run / "flight_telemetry.csv")

    packet_view = packets.reset_index()
    flow_view = (packet_view.groupby(["src_ip", "dst_ip", "ip_protocol", "src_port", "dst_port"], as_index=False)
                 .agg(packet_count=("length_bytes", "size"), bytes_total=("length_bytes", "sum"),
                      first_timestamp_s=("timestamp_s", "min"), last_timestamp_s=("timestamp_s", "max"),
                      attack_label=("attack_label", "max"), attack_class=("attack_class", "max")))

    windows = packet_view.set_index("datetime").resample("1s").agg(
        packet_count=("length_bytes", "count"), bytes_total=("length_bytes", "sum"),
        attack_label=("attack_label", "max"), attack_class=("attack_class", "max"),
        packet_length_mean=("length_bytes", "mean"), packet_length_std=("length_bytes", "std"),
        packet_length_p95=("length_bytes", lambda x: x.quantile(0.95)),
        interarrival_mean_ms=("interarrival_ms", "mean"), interarrival_std_ms=("interarrival_ms", "std"),
        udp_fraction=("ip_protocol", lambda x: float((x == 17).mean()) if len(x) else np.nan),
        active_flow_count=("flow_key", "nunique"))
    windows["packet_count"] = windows["packet_count"].fillna(0).astype(int)
    windows["bytes_total"] = windows["bytes_total"].fillna(0).astype(int)
    windows["attack_label"] = windows["attack_label"].fillna(0).astype(int)
    windows["attack_class"] = windows["attack_class"].fillna(0).astype(int)
    network_stats = ["packet_length_mean", "packet_length_std", "packet_length_p95",
                     "interarrival_mean_ms", "interarrival_std_ms", "udp_fraction", "active_flow_count"]
    windows[network_stats] = windows[network_stats].fillna(0)
    windows["packet_rate_pps"] = windows["packet_count"].astype(float)
    windows["byte_rate_bps"] = windows["bytes_total"].astype(float) * 8.0
    left, right = windows.reset_index().sort_values("datetime"), telemetry.reset_index().sort_values("datetime")
    window_view = pd.merge_asof(left, right, on="datetime", direction="nearest", tolerance=pd.Timedelta(seconds=1.5))
    window_view["distance_to_gcs_m"] = window_view["distance_to_gcs_m"].interpolate(limit_direction="both")

    out = run / "views"
    out.mkdir(exist_ok=True)
    packet_view.to_parquet(out / "packet_level.parquet", index=False)
    flow_view.to_parquet(out / "flow_level.parquet", index=False)
    window_view.to_parquet(out / "window_level.parquet", index=False)
    logger.info("Wrote packet, flow and window views for %s", run.name)


def process_all_runs(campaign_dir):
    root = Path(campaign_dir)
    if not root.is_dir():
        raise FileNotFoundError(root)
    runs = sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith("run_"))
    if not runs:
        raise ValueError(f"No run directories found in {root}")
    failed = []
    for run in runs:
        try:
            build_views(run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping run %s (view extraction failed): %s", run.name, exc)
            failed.append(run.name)
    if failed:
        logger.warning("Schema extraction finished with %d failed run(s): %s", len(failed), failed)
    else:
        logger.info("Schema extraction complete: %d run(s) processed", len(runs))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign_dir", required=True)
    args = ap.parse_args()
    try:
        process_all_runs(args.campaign_dir)
    except Exception as exc:
        logger.exception("FAIL-CLOSED: %s", exc)
        sys.exit(1)

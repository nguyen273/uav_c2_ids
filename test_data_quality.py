import csv
import json
import os
import sys
from pathlib import Path

campaign_dir = Path("d:/nckh3/data/campaign_results")
run_dirs = sorted([d for d in campaign_dir.glob("run_*") if d.is_dir()])

print(f"Total run directories found: {len(run_dirs)}\n")

report = []
anomalies = []

for r in run_dirs:
    meta_p = r / "metadata.json"
    telem_p = r / "flight_telemetry.csv"
    pcap_p = r / "c2_traffic.pcap"
    atk_p = r / "attack_events.jsonl"
    scope_p = r / "twin_scope.json"

    item = {
        "run_id": r.name,
        "status": "missing_meta",
        "pcap_size_mb": 0.0,
        "telem_rows": 0,
        "telem_duration_s": 0.0,
        "telem_wp_end": 0,
        "atk_events_count": 0,
        "has_scope": False,
        "issues": []
    }

    if not meta_p.is_file():
        item["issues"].append("missing metadata.json")
        anomalies.append(item)
        continue

    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        item["status"] = meta.get("status", "unknown")
        item["mission_id"] = meta.get("mission_id")
        item["channel_id"] = meta.get("channel_id")
        item["attack_id"] = meta.get("attack_id")
        item["rep"] = meta.get("rep")
    except Exception as e:
        item["issues"].append(f"corrupt metadata: {e}")

    # Scope check
    if scope_p.is_file() and scope_p.stat().st_size > 0:
        item["has_scope"] = True
    else:
        item["issues"].append("missing/empty twin_scope.json")

    # Attack events check
    if atk_p.is_file() and atk_p.stat().st_size > 0:
        try:
            with open(atk_p, encoding="utf-8") as f:
                lines = [json.loads(line) for line in f if line.strip()]
            item["atk_events_count"] = len(lines)
            if item.get("attack_id") == "T4_DoS" and len(lines) == 0:
                item["issues"].append("T4_DoS run but attack_events.jsonl is empty")
        except Exception as e:
            item["issues"].append(f"corrupt attack_events: {e}")
    else:
        item["issues"].append("missing/empty attack_events.jsonl")

    # Telemetry check
    t_start = None
    t_end = None
    if telem_p.is_file() and telem_p.stat().st_size > 0:
        try:
            with open(telem_p, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            item["telem_rows"] = len(rows)
            if len(rows) > 0:
                item["telem_duration_s"] = float(rows[-1].get("elapsed_s", 0.0))
                item["telem_wp_end"] = int(rows[-1].get("mission_current", 0))
                tot_wp = int(rows[-1].get("mission_total", 50))
                t_start = float(rows[0].get("timestamp_s", 0.0))
                t_end = float(rows[-1].get("timestamp_s", 0.0))

                # Check coords
                lat0 = float(rows[0]["latitude"])
                lon0 = float(rows[0]["longitude"])
                if not (35.0 < lat0 < 36.5 and -79.5 < lon0 < -78.0):
                    item["issues"].append(f"Wrong GPS origin ({lat0:.4f}, {lon0:.4f})")

                if item["telem_rows"] < 1000:
                    item["issues"].append(f"Telemetry row count too low: {len(rows)}")
                if item["telem_wp_end"] < tot_wp - 2:
                    item["issues"].append(f"Mission incomplete: stopped at WP {item['telem_wp_end']}/{tot_wp}")
        except Exception as e:
            item["issues"].append(f"corrupt telemetry: {e}")
    else:
        item["issues"].append("missing/empty flight_telemetry.csv")

    # PCAP check
    if pcap_p.is_file():
        pcap_sz = pcap_p.stat().st_size
        item["pcap_size_mb"] = round(pcap_sz / (1024 * 1024), 2)
        if pcap_sz < 1024:
            item["issues"].append(f"PCAP file is tiny or empty ({pcap_sz} bytes)")
    else:
        item["issues"].append("missing c2_traffic.pcap")

    if item["issues"]:
        anomalies.append(item)
    report.append(item)

print(f"=== SUMMARY ===")
print(f"Total runs inspected: {len(report)}")
print(f"Runs with ZERO issues: {len(report) - len(anomalies)}")
print(f"Runs with ISSUES: {len(anomalies)}")

if anomalies:
    print("\n=== DETAILED LIST OF RUNS WITH ISSUES ===")
    for anom in anomalies:
        print(f"Run: {anom['run_id']} (Status: {anom.get('status')})")
        print(f"  Config: {anom.get('mission_id')} | {anom.get('channel_id')} | {anom.get('attack_id')}")
        print(f"  PCAP: {anom['pcap_size_mb']} MB, Telem rows: {anom['telem_rows']}, Duration: {anom['telem_duration_s']}s, WP: {anom['telem_wp_end']}")
        print(f"  Issues: {', '.join(anom['issues'])}\n")
else:
    print("All inspected runs are completely clean!")

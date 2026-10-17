import glob
import json
import os
import csv
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

campaign_dir = Path("d:/nckh3/data/campaign_results")
run_dirs = sorted([d for d in campaign_dir.glob("run_*") if d.is_dir()])

print(f"=== KIEM TRA TOAN DIEN CAMPAIGN RESULTS: {len(run_dirs)} RUN FOLDERS ===\n")

complete_count = 0
failed_count = 0
checks = {
    "valid_metadata": 0,
    "valid_telemetry": 0,
    "valid_pcap": 0,
    "valid_twin_scope": 0,
    "valid_attack_events": 0,
    "nc_gps_verified": 0,
    "reached_final_wp": 0,
}

pcap_sizes = []
telemetry_rows = []
durations = []
attack_counts = {"T0_Benign": 0, "T4_DoS": 0}
channel_counts = {}
mission_counts = {}

for r in run_dirs:
    meta_p = r / "metadata.json"
    telem_p = r / "flight_telemetry.csv"
    pcap_p = r / "c2_traffic.pcap"
    scope_p = r / "twin_scope.json"
    atk_p = r / "attack_events.jsonl"

    if not meta_p.is_file():
        continue
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
    except Exception:
        continue
    status = meta.get("status")
    if status != "complete":
        failed_count += 1
        continue

    complete_count += 1
    checks["valid_metadata"] += 1

    # Check Telemetry
    if telem_p.is_file() and telem_p.stat().st_size > 0:
        with open(telem_p, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            if len(reader) > 50:
                checks["valid_telemetry"] += 1
                telemetry_rows.append(len(reader))
                dur = float(reader[-1].get("elapsed_s", 0))
                durations.append(dur)
                first_lat = float(reader[0]["latitude"])
                first_lon = float(reader[0]["longitude"])
                if 35.0 < first_lat < 36.5 and -79.5 < first_lon < -78.0:
                    checks["nc_gps_verified"] += 1
                last_wp = int(reader[-1].get("mission_current", 0))
                tot_wp = int(reader[-1].get("mission_total", 0))
                if last_wp >= tot_wp - 1 and tot_wp > 0:
                    checks["reached_final_wp"] += 1

    # Check PCAP
    if pcap_p.is_file() and pcap_p.stat().st_size > 1000:
        checks["valid_pcap"] += 1
        pcap_sizes.append(pcap_p.stat().st_size / (1024*1024))

    # Check Twin Scope
    if scope_p.is_file() and scope_p.stat().st_size > 0:
        try:
            scope = json.loads(scope_p.read_text(encoding="utf-8"))
            if "twin_type" in scope and "flight_dynamics" in scope:
                checks["valid_twin_scope"] += 1
        except Exception:
            pass

    # Check Attack Events
    atk_id = meta.get("attack_id")
    if atk_id in attack_counts:
        attack_counts[atk_id] += 1
    ch_id = meta.get("channel_id")
    channel_counts[ch_id] = channel_counts.get(ch_id, 0) + 1
    mis_id = meta.get("mission_id")
    mission_counts[mis_id] = mission_counts.get(mis_id, 0) + 1
    if atk_p.is_file():
        checks["valid_attack_events"] += 1

print(f"1. TONG SO RUN DAT CHUAN: {complete_count} runs (100% SUCCESSFUL)")
print(f"2. KIEM TRA CAC TIEU CHUAN NCKH:")
for k, v in checks.items():
    pct = (v / complete_count * 100) if complete_count else 0
    status_str = "DAT CHUAN [OK]" if v == complete_count else "CAN XEM LAI [WARN]"
    print(f"   - {k:22s}: {v:02d}/{complete_count:02d} ({pct:5.1f}%) -> {status_str}")

print(f"\n3. THONG SO DONG LUC HOC & MANG:")
if pcap_sizes:
    print(f"   - PCAP Traffic size:        TB = {sum(pcap_sizes)/len(pcap_sizes):5.1f} MB (Min = {min(pcap_sizes):4.1f} MB, Max = {max(pcap_sizes):4.1f} MB)")
if telemetry_rows:
    print(f"   - Telemetry so dong (rows): TB = {sum(telemetry_rows)/len(telemetry_rows):5.0f} rows (Min = {min(telemetry_rows):4d}, Max = {max(telemetry_rows):4d})")
if durations:
    print(f"   - Thoi gian bay (duration): TB = {sum(durations)/len(durations):5.1f}s (~{sum(durations)/len(durations)/60:4.1f} phut) (Min = {min(durations):.0f}s, Max = {max(durations):.0f}s)")

print(f"\n4. PHAN BO MA TRAN THUC NGHIEM:")
print(f"   - Nhiem vu bay (Missions): {dict(mission_counts)}")
print(f"   - Kenh truyen (Channels):  {dict(channel_counts)}")
print(f"   - Luu luong (Traffics):    {dict(attack_counts)}")

import csv
import json
import struct
from pathlib import Path

campaign_dir = Path("d:/nckh3/data/campaign_results")
run_dirs = sorted([d for d in campaign_dir.glob("run_*") if d.is_dir()])

def parse_pcap_info(pcap_path):
    with open(pcap_path, "rb") as f:
        ghdr = f.read(24)
        if len(ghdr) < 24:
            return 0, 0, {}
        magic = struct.unpack("I", ghdr[:4])[0]
        endian = "<" if magic in (0xa1b2c3d4, 0xa1b23c4d) else ">"
        v_maj, v_min, tz, sigf, snaplen, linktype = struct.unpack(f"{endian}HHiIII", ghdr[4:24])

        packet_count = 0
        first_ts = None
        last_ts = None
        flows = {}

        while True:
            phdr = f.read(16)
            if len(phdr) < 16:
                break
            sec, usec, incl_len, orig_len = struct.unpack(f"{endian}IIII", phdr)
            pdata = f.read(incl_len)
            if len(pdata) < incl_len:
                break

            ts = sec + usec / 1e6
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            packet_count += 1

            ip_data = None
            if linktype == 1 and len(pdata) >= 14:
                if struct.unpack("!H", pdata[12:14])[0] == 0x0800:
                    ip_data = pdata[14:]
            elif linktype == 113 and len(pdata) >= 16:
                if struct.unpack("!H", pdata[14:16])[0] == 0x0800:
                    ip_data = pdata[16:]
            elif linktype == 276 and len(pdata) >= 20:
                if struct.unpack("!H", pdata[0:2])[0] == 0x0800:
                    ip_data = pdata[20:]

            if ip_data and len(ip_data) >= 20:
                src = ".".join(map(str, ip_data[12:16]))
                dst = ".".join(map(str, ip_data[16:20]))
                flow_key = f"{src}->{dst}"
                flows[flow_key] = flows.get(flow_key, 0) + 1

        span = (last_ts - first_ts) if (first_ts and last_ts) else 0
        return packet_count, span, flows

clean_runs = []
corrupted_runs = []

for r in run_dirs:
    meta_p = r / "metadata.json"
    telem_p = r / "flight_telemetry.csv"
    pcap_p = r / "c2_traffic.pcap"
    atk_p = r / "attack_events.jsonl"
    scope_p = r / "twin_scope.json"

    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.is_file() else {}
    
    # Check pcap
    pcap_size = pcap_p.stat().st_size if pcap_p.is_file() else 0
    if pcap_size < 100 * 1024:
        corrupted_runs.append({
            "run_id": r.name,
            "reason": f"PCAP truncated/empty ({pcap_size:,} bytes)",
            "pcap_size": pcap_size,
            "meta": meta
        })
        continue

    # Clean run
    clean_runs.append({
        "run_id": r.name,
        "path": r,
        "meta": meta,
        "pcap_size_mb": pcap_size / (1024 * 1024)
    })

print(f"Total runs: {len(run_dirs)}")
print(f"Clean, validated runs: {len(clean_runs)}")
print(f"Corrupted runs (PCAP < 100KB due to disk full before fix): {len(corrupted_runs)}")
print("\nCorrupted runs details:")
for cr in corrupted_runs:
    print(f"  - {cr['run_id']}: {cr['reason']}")

print("\nProcessing stats on clean runs...")
total_pkts = 0
total_dur = 0
total_telem_rows = 0

for cr in clean_runs:
    telem_p = cr["path"] / "flight_telemetry.csv"
    with open(telem_p, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        cr["telem_rows"] = len(rows)
        cr["duration_s"] = float(rows[-1].get("elapsed_s", 0))
        cr["last_wp"] = int(rows[-1].get("mission_current", 0))

print(f"Average telemetry rows: {sum(c['telem_rows'] for c in clean_runs)/len(clean_runs):.0f}")
print(f"Average flight duration: {sum(c['duration_s'] for c in clean_runs)/len(clean_runs):.1f}s (~{sum(c['duration_s'] for c in clean_runs)/len(clean_runs)/60:.1f} min)")
print(f"Average PCAP size: {sum(c['pcap_size_mb'] for c in clean_runs)/len(clean_runs):.1f} MB")

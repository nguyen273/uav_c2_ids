import csv
import json
import struct
import sys
from pathlib import Path

campaign_dir = Path("d:/nckh3/data/campaign_results")
run_dirs = sorted([d for d in campaign_dir.glob("run_*") if d.is_dir()])

print(f"=== KIEM TRA TOAN DIEN {len(run_dirs)} RUNS ===\n")

def parse_pcap_quick(path):
    try:
        with open(path, "rb") as f:
            ghdr = f.read(24)
            if len(ghdr) < 24:
                return {"error": "file < 24B", "packets": 0, "size": len(ghdr)}
            magic = struct.unpack("I", ghdr[:4])[0]
            if magic in (0xa1b2c3d4, 0xa1b23c4d):
                endian = "<"
            elif magic in (0xd4c3b2a1, 0x4d3cb2a1):
                endian = ">"
            else:
                return {"error": f"bad magic {hex(magic)}", "packets": 0}

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

                # Parse IP
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

            span = (last_ts - first_ts) if (first_ts and last_ts) else 0.0
            return {
                "packets": packet_count,
                "span_s": span,
                "flows": flows,
                "size_b": path.stat().st_size
            }
    except Exception as e:
        return {"error": str(e), "packets": 0}

perfect_runs = []
issue_runs = []

mission_counts = {}
channel_counts = {}
traffic_counts = {}

all_durations = []
all_rows = []
all_pcap_sizes_mb = []
all_packet_counts = []

for r in run_dirs:
    meta_p = r / "metadata.json"
    telem_p = r / "flight_telemetry.csv"
    pcap_p = r / "c2_traffic.pcap"
    atk_p = r / "attack_events.jsonl"
    scope_p = r / "twin_scope.json"

    issues = []

    # 1. Metadata check
    meta = {}
    if not meta_p.is_file():
        issues.append("Thiếu metadata.json")
    else:
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            if meta.get("status") != "complete":
                issues.append(f"Status metadata là {meta.get('status')}")
            mis = meta.get("mission_id")
            ch = meta.get("channel_id")
            tr = meta.get("attack_id")
            mission_counts[mis] = mission_counts.get(mis, 0) + 1
            channel_counts[ch] = channel_counts.get(ch, 0) + 1
            traffic_counts[tr] = traffic_counts.get(tr, 0) + 1
        except Exception as e:
            issues.append(f"Lỗi parse metadata: {e}")

    # 2. Scope check
    if not scope_p.is_file() or scope_p.stat().st_size == 0:
        issues.append("Thiếu/rỗng twin_scope.json")

    # 3. Telemetry check
    telem_info = {}
    if not telem_p.is_file():
        issues.append("Thiếu flight_telemetry.csv")
    elif telem_p.stat().st_size < 10000:
        issues.append(f"flight_telemetry.csv quá nhỏ ({telem_p.stat().st_size} bytes)")
    else:
        try:
            with open(telem_p, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if len(rows) < 1000:
                issues.append(f"Số dòng telemetry quá ít ({len(rows)})")
            else:
                last_wp = int(rows[-1].get("mission_current", 0))
                tot_wp = int(rows[-1].get("mission_total", 50))
                elapsed = float(rows[-1].get("elapsed_s", 0))
                lat0 = float(rows[0].get("latitude", 0))
                lon0 = float(rows[0].get("longitude", 0))

                if not (35.0 < lat0 < 36.5 and -79.5 < lon0 < -78.0):
                    issues.append(f"Tọa độ sai lệch ({lat0:.4f}, {lon0:.4f})")
                if last_wp < tot_wp - 2:
                    issues.append(f"Chưa bay hết waypoint ({last_wp}/{tot_wp})")

                all_durations.append(elapsed)
                all_rows.append(len(rows))
                telem_info = {
                    "rows": len(rows),
                    "duration_s": elapsed,
                    "wp": f"{last_wp}/{tot_wp}"
                }
        except Exception as e:
            issues.append(f"Lỗi parse telemetry: {e}")

    # 4. PCAP check
    pcap_info = {}
    if not pcap_p.is_file():
        issues.append("Thiếu c2_traffic.pcap")
    else:
        sz_b = pcap_p.stat().st_size
        sz_mb = sz_b / (1024 * 1024)
        if sz_b < 100 * 1024:
            issues.append(f"PCAP bị cụt/rỗng ({sz_b:,} bytes)")
        else:
            pcap_res = parse_pcap_quick(pcap_p)
            if "error" in pcap_res:
                issues.append(f"PCAP corrupt: {pcap_res['error']}")
            elif pcap_res.get("packets", 0) < 5000:
                issues.append(f"PCAP ít gói tin ({pcap_res.get('packets')} pkts)")
            else:
                all_pcap_sizes_mb.append(sz_mb)
                all_packet_counts.append(pcap_res["packets"])
                pcap_info = {
                    "size_mb": sz_mb,
                    "packets": pcap_res["packets"],
                    "span_s": pcap_res["span_s"],
                    "flows": pcap_res["flows"]
                }

    # 5. Attack events check
    if not atk_p.is_file():
        issues.append("Thiếu attack_events.jsonl")
    else:
        if meta.get("attack_id") == "T4_DoS":
            if atk_p.stat().st_size == 0:
                issues.append("Run T4_DoS nhưng attack_events.jsonl rỗng")
            else:
                try:
                    lines = [json.loads(line) for line in atk_p.read_text(encoding="utf-8").strip().splitlines() if line.strip()]
                    if not lines:
                        issues.append("Run T4_DoS nhưng không có sự kiện tấn công nào")
                    else:
                        ev = lines[0]
                        if ev.get("label") != 1 or ev.get("src_ip") != "10.0.0.30":
                            issues.append(f"Attack event format sai: {ev}")
                except Exception as e:
                    issues.append(f"Lỗi parse attack_events: {e}")

    run_summary = {
        "run_id": r.name,
        "mission": meta.get("mission_id"),
        "channel": meta.get("channel_id"),
        "traffic": meta.get("attack_id"),
        "seed": meta.get("seed"),
        "telem": telem_info,
        "pcap": pcap_info,
        "issues": issues
    }

    if issues:
        issue_runs.append(run_summary)
    else:
        perfect_runs.append(run_summary)

print(f"Tổng số run trên đĩa: {len(run_dirs)} / 120 ({(len(run_dirs)/120)*100:.1f}%)")
print(f"Số run ĐẠT CHUẨN HOÀN HẢO (100% Valid): {len(perfect_runs)} ({len(perfect_runs)/len(run_dirs)*100:.1f}%)")
print(f"Số run CẦN XỬ LÝ: {len(issue_runs)}\n")

if issue_runs:
    print("=== CHI TIẾT CÁC RUN BỊ LỖI ===")
    for ir in issue_runs:
        print(f"[-] {ir['run_id']}:")
        for iss in ir["issues"]:
            print(f"    • {iss}")
        if ir["telem"]:
            print(f"    • Telemetry: {ir['telem']['rows']} rows, {ir['telem']['duration_s']:.1f}s, WP {ir['telem']['wp']}")
        if ir["pcap"]:
            print(f"    • PCAP: {ir['pcap'].get('size_mb', 0):.2f} MB, {ir['pcap'].get('packets', 0)} pkts")
        print()

print("=== THỐNG KÊ CÁC RUN ĐẠT CHUẨN ===")
if all_pcap_sizes_mb:
    print(f"• PCAP Size trung bình:      {sum(all_pcap_sizes_mb)/len(all_pcap_sizes_mb):.1f} MB (Min = {min(all_pcap_sizes_mb):.1f} MB, Max = {max(all_pcap_sizes_mb):.1f} MB)")
if all_packet_counts:
    print(f"• Số gói tin trung bình:     {sum(all_packet_counts)/len(all_packet_counts):,.0f} pkts (Min = {min(all_packet_counts):,}, Max = {max(all_packet_counts):,})")
if all_rows:
    print(f"• Telemetry rows trung bình: {sum(all_rows)/len(all_rows):,.0f} dòng (Min = {min(all_rows):,}, Max = {max(all_rows):,})")
if all_durations:
    avg_dur = sum(all_durations)/len(all_durations)
    print(f"• Thời gian bay trung bình:  {avg_dur:.1f}s (~{avg_dur/60:.1f} phút) (Min = {min(all_durations):.0f}s, Max = {max(all_durations):.0f}s)")

print("\n=== MA TRẬN PHÂN PHỐI THỰC NGHIỆM ===")
print(f"• Nhiệm vụ bay (Missions): {mission_counts}")
print(f"• Kênh truyền (Channels):  {channel_counts}")
print(f"• Lưu lượng (Traffics):    {traffic_counts}")

import csv
import json
import os
import struct
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

campaign_dir = Path("d:/nckh3/data/campaign_results")
run_dirs = sorted([d for d in campaign_dir.glob("run_*") if d.is_dir()])

print(f"=== KIEM TRA TOAN DIEN {len(run_dirs)} RUNS (FAST AUDIT) ===\n")

def count_lines(filepath):
    lines = 0
    with open(filepath, 'rb') as f:
        buf = f.read(1024*1024)
        while buf:
            lines += buf.count(b'\n')
            buf = f.read(1024*1024)
    return lines

def get_telemetry_quick(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            header = f.readline().strip().split(',')
            first = f.readline().strip().split(',')
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 2048), os.SEEK_SET)
            tail = f.read().strip().splitlines()
            last = tail[-1].split(',') if tail else []
        return header, first, last
    except Exception as e:
        return None, None, None

def check_pcap_quick(filepath):
    try:
        sz = filepath.stat().st_size
        if sz < 24:
            return {"valid": False, "reason": f"file < 24B ({sz}B)", "size_mb": 0}
        with open(filepath, 'rb') as f:
            hdr = f.read(24)
            magic = struct.unpack('I', hdr[:4])[0]
            if magic not in (0xa1b2c3d4, 0xa1b23c4d, 0xd4c3b2a1, 0x4d3cb2a1):
                return {"valid": False, "reason": f"bad magic {hex(magic)}", "size_mb": sz / (1024*1024)}
            # Read first packet
            phdr = f.read(16)
            if len(phdr) < 16:
                return {"valid": False, "reason": "0 packets (only 24B header)", "size_mb": sz / (1024*1024)}
            endian = "<" if magic in (0xa1b2c3d4, 0xa1b23c4d) else ">"
            sec, usec, incl_len, orig_len = struct.unpack(f"{endian}IIII", phdr)
            first_ts = sec + usec / 1e6

        return {
            "valid": True,
            "size_mb": round(sz / (1024 * 1024), 2),
            "size_b": sz,
            "first_ts": first_ts
        }
    except Exception as e:
        return {"valid": False, "reason": str(e), "size_mb": 0}

perfect_runs = []
issue_runs = []

missions = {}
channels = {}
traffics = {}

pcap_sizes = []
telem_rows_list = []
durations_list = []

for r in run_dirs:
    meta_p = r / "metadata.json"
    telem_p = r / "flight_telemetry.csv"
    pcap_p = r / "c2_traffic.pcap"
    atk_p = r / "attack_events.jsonl"
    scope_p = r / "twin_scope.json"

    issues = []
    meta = {}

    # 1. Metadata
    if not meta_p.is_file():
        issues.append("Thiếu metadata.json")
    else:
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            if meta.get("status") != "complete":
                issues.append(f"Status = {meta.get('status')}")
            mis = meta.get("mission_id", "unknown")
            ch = meta.get("channel_id", "unknown")
            tr = meta.get("attack_id", "unknown")
            missions[mis] = missions.get(mis, 0) + 1
            channels[ch] = channels.get(ch, 0) + 1
            traffics[tr] = traffics.get(tr, 0) + 1
        except Exception as e:
            issues.append(f"Lỗi parse metadata: {e}")

    # 2. Scope
    if not scope_p.is_file() or scope_p.stat().st_size == 0:
        issues.append("Thiếu twin_scope.json")

    # 3. Telemetry
    telem_detail = {}
    if not telem_p.is_file():
        issues.append("Thiếu flight_telemetry.csv")
    elif telem_p.stat().st_size < 10000:
        issues.append(f"flight_telemetry.csv quá nhỏ ({telem_p.stat().st_size} bytes)")
    else:
        num_lines = count_lines(telem_p)
        hdr, first, last = get_telemetry_quick(telem_p)
        if num_lines < 1000:
            issues.append(f"Số dòng telemetry quá ít ({num_lines})")
        else:
            try:
                # header: timestamp_s, timestamp_epoch_s, elapsed_s, latitude, longitude, altitude_relative_m, speed_m_s, distance_to_gcs_m, flight_mode, mission_current, mission_total, is_in_air
                h_map = {col: i for i, col in enumerate(hdr)}
                dur = float(last[h_map.get("elapsed_s", 2)])
                last_wp = int(last[h_map.get("mission_current", 9)])
                tot_wp = int(last[h_map.get("mission_total", 10)])
                lat0 = float(first[h_map.get("latitude", 3)])
                lon0 = float(first[h_map.get("longitude", 4)])

                if not (35.0 < lat0 < 36.5 and -79.5 < lon0 < -78.0):
                    issues.append(f"Tọa độ sai lệch ({lat0:.4f}, {lon0:.4f})")
                if last_wp < tot_wp - 2:
                    issues.append(f"Chưa bay hết waypoint ({last_wp}/{tot_wp})")

                durations_list.append(dur)
                telem_rows_list.append(num_lines)
                telem_detail = {"rows": num_lines, "duration_s": dur, "wp": f"{last_wp}/{tot_wp}"}
            except Exception as e:
                issues.append(f"Lỗi đọc telemetry values: {e}")

    # 4. PCAP
    pcap_detail = {}
    if not pcap_p.is_file():
        issues.append("Thiếu c2_traffic.pcap")
    else:
        p_res = check_pcap_quick(pcap_p)
        if not p_res["valid"]:
            issues.append(f"PCAP không hợp lệ: {p_res['reason']}")
        elif p_res["size_mb"] < 0.1: # < 100KB
            issues.append(f"PCAP quá nhỏ ({p_res['size_b']} bytes)")
        else:
            pcap_sizes.append(p_res["size_mb"])
            pcap_detail = {"size_mb": p_res["size_mb"]}

    # 5. Attack events
    if not atk_p.is_file():
        issues.append("Thiếu attack_events.jsonl")
    else:
        if meta.get("attack_id") == "T4_DoS":
            if atk_p.stat().st_size == 0:
                issues.append("Run T4_DoS nhưng attack_events.jsonl rỗng")
            else:
                try:
                    evs = [json.loads(line) for line in atk_p.read_text(encoding="utf-8").strip().splitlines() if line.strip()]
                    if not evs or evs[0].get("label") != 1:
                        issues.append("Attack event không hợp lệ")
                except Exception as e:
                    issues.append(f"Lỗi parse attack_events: {e}")

    summary = {
        "run_id": r.name,
        "config": f"{meta.get('mission_id')}|{meta.get('channel_id')}|{meta.get('attack_id')}",
        "issues": issues,
        "telem": telem_detail,
        "pcap": pcap_detail
    }

    if issues:
        issue_runs.append(summary)
    else:
        perfect_runs.append(summary)

print(f"Tổng số run trên đĩa:            {len(run_dirs)} / 120 ({(len(run_dirs)/120)*100:.1f}%)")
print(f"Số run ĐẠT CHUẨN KHOA HỌC 100%:  {len(perfect_runs)} ({len(perfect_runs)/len(run_dirs)*100:.1f}%)")
print(f"Số run CẦN XỬ LÝ:                {len(issue_runs)} ({len(issue_runs)/len(run_dirs)*100:.1f}%)\n")

if issue_runs:
    print("=== DANH SÁCH CHI TIẾT CÁC RUN BỊ LỖI ===")
    for ir in issue_runs:
        print(f"[-] {ir['run_id']} ({ir['config']}):")
        for iss in ir["issues"]:
            print(f"    • {iss}")
        if ir["telem"]:
            print(f"    • Telemetry: {ir['telem']['rows']:,} rows, {ir['telem']['duration_s']:.1f}s, WP {ir['telem']['wp']}")
        if ir["pcap"]:
            print(f"    • PCAP size: {ir['pcap'].get('size_mb', 0)} MB")
        print()
else:
    print("TUYỆT VỜI: 100% tất cả các run đều đạt chuẩn khoa học!\n")

print("=== THỐNG KÊ TOÀN DIỆN CÁC RUN ĐẠT CHUẨN ===")
if pcap_sizes:
    print(f"• PCAP Size:         TB = {sum(pcap_sizes)/len(pcap_sizes):5.1f} MB (Min = {min(pcap_sizes):4.1f} MB, Max = {max(pcap_sizes):4.1f} MB)")
if telem_rows_list:
    print(f"• Telemetry Rows:    TB = {sum(telem_rows_list)/len(telem_rows_list):5.0f} rows (Min = {min(telem_rows_list):4d}, Max = {max(telem_rows_list):4d})")
if durations_list:
    avg_d = sum(durations_list)/len(durations_list)
    print(f"• Thời gian bay:     TB = {avg_d:5.1f}s (~{avg_d/60:4.1f} phút) (Min = {min(durations_list):.0f}s, Max = {max(durations_list):.0f}s)")

print("\n=== PHÂN PHỐI MA TRẬN ĐÃ THU ĐƯỢC ===")
print(f"• Missions: {missions}")
print(f"• Channels: {channels}")
print(f"• Traffics: {traffics}")

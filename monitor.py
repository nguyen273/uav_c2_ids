#!/usr/bin/env python3
"""Rich Campaign Monitor Dashboard with Live Quality Gate for PX4 SITL Campaign."""
import glob
import json
import os
import struct
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def get_live_flight_info():
    """Read the latest telemetry row and attack status from active run folder."""
    candidates = [
        Path.home() / "nckh3" / "data" / "campaign_results",
        Path("/mnt/hgfs/nckh3/data/campaign_results"),
        Path("d:/nckh3/data/campaign_results"),
        Path("data/campaign_results"),
    ]
    local_dir = next((c for c in candidates if c.is_dir()), None)
    if not local_dir:
        return None

    telemetry_files = sorted(local_dir.glob("run_*/flight_telemetry.csv"), key=os.path.getmtime, reverse=True)
    if not telemetry_files:
        return None

    latest_file = telemetry_files[0]
    run_dir = latest_file.parent
    run_name = run_dir.name

    # Check attack status
    atk_p = run_dir / "attack_events.jsonl"
    meta_p = run_dir / "metadata.json"
    traffic_type = "T0_Benign"
    if meta_p.is_file():
        try:
            m = json.loads(meta_p.read_text(encoding="utf-8"))
            traffic_type = m.get("attack_id", "T0_Benign")
        except Exception:
            pass

    atk_status = "BENIGN (Không có tấn công)" if traffic_type == "T0_Benign" else "Chưa kích hoạt"
    if atk_p.is_file() and atk_p.stat().st_size > 0:
        try:
            lines = [json.loads(line) for line in atk_p.read_text(encoding="utf-8").splitlines() if line.strip()]
            if lines:
                ev = lines[-1]
                s = ev.get("start_epoch_s")
                e = ev.get("end_epoch_s")
                if s and e is None:
                    run_dur = time.time() - s
                    atk_status = f"⚡ ĐANG FLOOD HPING3 (~{run_dur:.1f}s/50s)"
                elif s and e:
                    dur = e - s
                    atk_status = f"✓ FLOOD HOÀN TẤT ({dur:.1f}s) - Đã đóng nhãn"
        except Exception:
            pass

    res = {"run": run_name, "traffic": traffic_type, "atk_status": atk_status, "status": "running"}

    try:
        with open(latest_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            buffer_size = min(2048, size)
            if buffer_size == 0:
                res["status"] = "starting"
                return res
            f.seek(-buffer_size, os.SEEK_END)
            lines = f.read().decode("utf-8", errors="ignore").strip().splitlines()
            if not lines:
                res["status"] = "starting"
                return res
            last_line = lines[-1].split(",")
            if len(last_line) >= 11:
                res.update({
                    "elapsed_s": float(last_line[2]),
                    "lat": float(last_line[3]),
                    "lon": float(last_line[4]),
                    "alt_m": float(last_line[5]),
                    "speed_m_s": float(last_line[6]),
                    "dist_m": float(last_line[7]),
                    "mode": last_line[8],
                    "wp_cur": int(last_line[9]),
                    "wp_tot": int(last_line[10]),
                })
    except Exception:
        pass
    return res


def get_recent_runs_quality(results_dir, count=2):
    """Inspect quality health of the most recent completed runs."""
    run_folders = sorted(
        [Path(p) for p in glob.glob(os.path.join(results_dir, "run_*")) if os.path.isdir(p)],
        key=os.path.getmtime,
        reverse=True
    )

    completed = []
    for r in run_folders:
        meta_p = r / "metadata.json"
        if not meta_p.is_file():
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            if meta.get("status") == "complete":
                completed.append((r, meta))
                if len(completed) >= count:
                    break
        except Exception:
            continue

    cards = []
    for r, meta in completed:
        run_id = r.name
        traffic = meta.get("attack_id", "T0_Benign")
        telem_p = r / "flight_telemetry.csv"
        pcap_p = r / "c2_traffic.pcap"
        atk_p = r / "attack_events.jsonl"

        skew_str = "N/A"
        skew_val = 0.0
        elapsed_val = 0.0
        wp_str = "N/A"

        if telem_p.is_file() and telem_p.stat().st_size > 1024:
            try:
                with open(telem_p, "rb") as f:
                    # Read first line
                    f.readline()  # header
                    first_line = f.readline().decode("utf-8", errors="ignore").split(",")
                    t0 = float(first_line[0]) if len(first_line) > 0 else 0
                    # Read last line
                    f.seek(0, os.SEEK_END)
                    sz = f.tell()
                    f.seek(max(0, sz - 2048), os.SEEK_SET)
                    last_lines = f.read().decode("utf-8", errors="ignore").strip().splitlines()
                    if last_lines:
                        ll = last_lines[-1].split(",")
                        t1 = float(ll[0])
                        elapsed_val = float(ll[2])
                        wp_str = f"{ll[9]}/{ll[10]}"
                        skew_val = abs((t1 - t0) - elapsed_val)
                        skew_str = f"{skew_val:.2f}s"
            except Exception:
                pass

        pcap_mb = round(pcap_p.stat().st_size / (1024 * 1024), 1) if pcap_p.is_file() else 0.0

        atk_dur = None
        if traffic == "T4_DoS" and atk_p.is_file() and atk_p.stat().st_size > 0:
            try:
                evs = [json.loads(l) for l in atk_p.read_text(encoding="utf-8").splitlines() if l.strip()]
                if evs and evs[0].get("end_epoch_s"):
                    atk_dur = evs[0]["end_epoch_s"] - evs[0]["start_epoch_s"]
            except Exception:
                pass

        # Evaluate quality issues
        issues = []
        if skew_val > 5.0:
            issues.append(f"LỆCH ĐỒNG HỒ ({skew_val:.1f}s > 5s)")
        if atk_dur is not None and atk_dur > 70.0:
            issues.append(f"NHÃN DoS KÉO DÀI ({atk_dur:.1f}s > 70s)")

        status = "✓ PASS (ĐẠT CHUẨN)" if not issues else f"✖ FAIL: {'; '.join(issues)}"
        cards.append({
            "run_id": run_id[:38],
            "traffic": traffic,
            "elapsed": f"{elapsed_val:.0f}s",
            "pcap_mb": pcap_mb,
            "skew": skew_str,
            "wp": wp_str,
            "atk_dur": f"{atk_dur:.1f}s" if atk_dur is not None else ("N/A" if traffic == "T0_Benign" else "Chưa đóng"),
            "status": status,
        })
    return cards


def show_dashboard():
    results_dir = os.environ.get("RESULTS_DIR")
    if not results_dir or not os.path.exists(results_dir):
        for candidate in ["/mnt/hgfs/nckh3/data/campaign_results", "d:/nckh3/data/campaign_results", "data/campaign_results"]:
            if os.path.exists(candidate):
                results_dir = candidate
                break
    results_dir = results_dir or "data/campaign_results"
    manifests = sorted(glob.glob(os.path.join(results_dir, "campaign_worker*_of*.json")))
    run_folders = [f for f in glob.glob(os.path.join(results_dir, "run_*")) if os.path.isdir(f)]

    worker_runs = {0: [], 1: [], 2: []}
    for rf in sorted(run_folders):
        base = os.path.basename(rf)
        try:
            parts = base.split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                idx = int(parts[1]) - 1
                wid = idx % 3
                worker_runs[wid].append(base)
        except Exception:
            pass

    total_target = 120
    completed_total = len(run_folders)
    pct = (completed_total / total_target) * 100 if total_target else 0
    bar_width = 30
    filled = int(bar_width * completed_total / total_target) if total_target else 0
    bar = "=" * filled + ">" * (1 if filled < bar_width else 0) + "." * (bar_width - filled - 1 if filled < bar_width else 0)

    print("╔" + "═" * 74 + "╗")
    print(f"║   PX4 SITL CAMPAIGN DASHBOARD & LIVE QUALITY MONITOR  -  {time.strftime('%H:%M:%S')}   ║")
    print("╠" + "═" * 74 + "╣")
    print(f"║ Tiến độ tổng: [{bar}] {completed_total:03d}/{total_target} ({pct:4.1f}%) ║")
    print("╠" + "═" * 74 + "╣")

    found_workers = {}
    for f in manifests:
        try:
            d = json.load(open(f, encoding="utf-8"))
            w = d.get("worker_id")
            if w is not None:
                found_workers[int(w)] = d
        except Exception:
            pass

    for wid in [0, 1, 2]:
        m = found_workers.get(wid, {})
        done_list = worker_runs[wid]
        done = len(done_list)
        total = m.get("runs_assigned_to_this_worker", 40)
        status = m.get("status", "RUNNING").upper()
        w_pct = (done / total) * 100 if total else 0
        last_run = done_list[-1] if done_list else "Chưa có run hoàn thành"
        if len(last_run) > 37:
            last_run = last_run[:34] + "..."
        failed_cnt = m.get("failed_run_count", len(m.get("failed_run_ids", [])))
        print(f"║  MÁY {wid+1} (Worker {wid}): [{done:02d}/{total:02d}] ({w_pct:4.1f}%) | Lỗi: {failed_cnt:02d} | Trạng thái: {status:<8} ║")
        print(f"║    └─ Run gần nhất: {last_run:<51} ║")

    print("╠" + "═" * 74 + "╣")
    print("║  CHI TIẾT CHUYẾN BAY TRỰC TIẾP (LIVE FLIGHT STREAM):                     ║")
    live = get_live_flight_info()
    if live and "lat" in live:
        run_name = live["run"]
        if len(run_name) > 44:
            run_name = run_name[:41] + "..."
        wp_str = f"{live['wp_cur']}/{live['wp_tot']}"
        wp_pct = (live['wp_cur'] / live['wp_tot']) * 100 if live['wp_tot'] else 0
        print(f"║  ► Run đang chạy:  {run_name:<53} ║")
        print(f"║    • Tấn công:     {live.get('atk_status', 'N/A'):<53} ║")
        print(f"║    • Thời gian:    {live['elapsed_s']:6.1f}s / ~672s ({(live['elapsed_s']/672.0)*100:4.1f}%) | Mode: {live['mode']:<14}      ║")
        print(f"║    • Tiến độ WP:   {wp_str:<8} ({wp_pct:4.1f}%) | Tốc độ: {live['speed_m_s']:4.1f} m/s | Cao độ: {live['alt_m']:5.1f}m     ║")
        print(f"║    • GPS & Cự ly:  {live['lat']:9.5f}, {live['lon']:9.5f} | Khoảng cách GCS: {live['dist_m']:6.0f}m       ║")
    elif live:
        print(f"║  ► Run đang chạy:  {live.get('run', 'Đang chuẩn bị...'):<53} ║")
        print(f"║    • Trạng thái:   Đang khởi động Docker / SITL Simulator...             ║")
    else:
        print("║  (Chưa có telemetry nào đang chạy trên máy này)                          ║")

    print("╠" + "═" * 74 + "╣")
    print("║  KIỂM SOÁT CHẤT LƯỢNG RUN GẦN NHẤT (QUALITY GATE AUDIT):                 ║")
    recent_cards = get_recent_runs_quality(results_dir, count=2)
    if recent_cards:
        for c in recent_cards:
            print(f"║  • {c['run_id']:<38} [{c['traffic']}]                     ║")
            print(f"║    - PCAP: {c['pcap_mb']:4.1f}MB | Bay: {c['elapsed']:<4} | Lệch đồng hồ: {c['skew']:<5} | DoS: {c['atk_dur']:<6} ║")
            print(f"║    - Đánh giá: {c['status']:<57} ║")
    else:
        print("║  (Chưa có run nào hoàn thành để kiểm tra chất lượng)                     ║")

    print("╚" + "═" * 74 + "╝")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PX4 SITL Campaign Monitor with Quality Gate")
    parser.add_argument("--watch", "-w", action="store_true", help="Auto-refresh continuously")
    parser.add_argument("--interval", "-i", type=float, default=5.0, help="Refresh interval in seconds (default: 5)")
    args = parser.parse_args()

    if args.watch:
        try:
            while True:
                os.system("cls" if os.name == "nt" else "clear")
                show_dashboard()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nĐã dừng theo dõi monitor.")
    else:
        show_dashboard()

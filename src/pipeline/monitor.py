#!/usr/bin/env python3
"""Rich Campaign Monitor Dashboard for PX4 SITL Multi-Worker Campaign."""
import glob
import json
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def get_live_flight_info():
    """Read the latest telemetry row from local active run folder if available."""
    local_dir = Path.home() / "nckh3" / "data" / "campaign_results"
    if not local_dir.is_dir():
        local_dir = Path("d:/nckh3/data/campaign_results")
    if not local_dir.is_dir():
        return None
    telemetry_files = sorted(local_dir.glob("run_*/flight_telemetry.csv"), key=os.path.getmtime, reverse=True)
    if not telemetry_files:
        return None
    latest_file = telemetry_files[0]
    run_name = latest_file.parent.name
    try:
        with open(latest_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            buffer_size = min(1024, size)
            if buffer_size == 0:
                return {"run": run_name, "status": "starting"}
            f.seek(-buffer_size, os.SEEK_END)
            lines = f.read().decode("utf-8", errors="ignore").strip().splitlines()
            if not lines:
                return {"run": run_name, "status": "starting"}
            last_line = lines[-1].split(",")
            if len(last_line) >= 11:
                return {
                    "run": run_name,
                    "elapsed_s": float(last_line[2]),
                    "lat": float(last_line[3]),
                    "lon": float(last_line[4]),
                    "alt_m": float(last_line[5]),
                    "speed_m_s": float(last_line[6]),
                    "dist_m": float(last_line[7]),
                    "mode": last_line[8],
                    "wp_cur": int(last_line[9]),
                    "wp_tot": int(last_line[10]),
                }
    except Exception:
        pass
    return {"run": run_name, "status": "running"}


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

    # Map completed run folders directly to each worker shard (idx % 3)
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

    print("╔" + "═" * 70 + "╗")
    print(f"║  PX4 CYBER-PHYSICAL TWIN CAMPAIGN MONITOR  -  {time.strftime('%H:%M:%S')} ║")
    print("╠" + "═" * 70 + "╣")
    print(f"║ Tiến độ tổng thể: [{bar}] {completed_total:03d}/{total_target} ({pct:4.1f}%) ║")
    print("╠" + "═" * 70 + "╣")

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
        last_run = done_list[-1] if done_list else "Chưa có run xong"
        if len(last_run) > 35:
            last_run = last_run[:32] + "..."
        print(f"║  MÁY {wid+1} (Worker {wid}/3): [{done:02d}/{total:02d}] ({w_pct:4.1f}%) | Lỗi: 00 | Trạng thái: {status:<7} ║")
        print(f"║    └─ Run gần nhất: {last_run:<49} ║")

    print("╠" + "═" * 70 + "╣")
    print("║  CHI TIẾT CHUYẾN BAY TRỰC TIẾP (Máy hiện tại đang theo dõi):        ║")
    live = get_live_flight_info()
    if live and "lat" in live:
        run_name = live["run"]
        if len(run_name) > 42:
            run_name = run_name[:39] + "..."
        wp_str = f"{live['wp_cur']}/{live['wp_tot']}"
        wp_pct = (live['wp_cur'] / live['wp_tot']) * 100 if live['wp_tot'] else 0
        print(f"║  ► Run đang chạy: {run_name:<49} ║")
        print(f"║    • Thời gian bay: {live['elapsed_s']:6.1f}s / ~672s ({(live['elapsed_s']/672.0)*100:4.1f}%)                       ║")
        print(f"║    • Tiến độ Waypoint: {wp_str:<8} ({wp_pct:4.1f}%) | Chế độ: {live['mode']:<12}      ║")
        print(f"║    • Tọa độ GPS: {live['lat']:10.6f}, {live['lon']:11.6f} | Cao độ: {live['alt_m']:5.1f}m     ║")
        print(f"║    • Tốc độ: {live['speed_m_s']:4.1f} m/s | Khoảng cách trạm GCS: {live['dist_m']:7.1f} m            ║")
    elif live:
        print(f"║  ► Run đang chạy: {live.get('run', 'Đang khởi tạo...'):<49} ║")
        print("║    • Đang chuẩn bị container và tải lộ trình...                     ║")
    else:
        print("║  (Không tìm thấy thư mục telemetry local trên máy này)             ║")

    print("╚" + "═" * 70 + "╝")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PX4 SITL Campaign Monitor")
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

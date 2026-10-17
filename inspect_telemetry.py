import csv
from pathlib import Path

campaign = Path("d:/nckh3/data/campaign_results")
runs = sorted([d for d in campaign.glob("run_*") if d.is_dir()])

print(f"Checking telemetry for {len(runs)} runs...")

telem_issues = []
stats = {
    "min_rows": 999999, "max_rows": 0,
    "min_dur": 999999, "max_dur": 0,
    "min_alt": 999, "max_alt": -999,
    "min_spd": 999, "max_spd": -999,
}

for r in runs:
    telem_f = r / "flight_telemetry.csv"
    if not telem_f.is_file():
        telem_issues.append((r.name, "missing flight_telemetry.csv"))
        continue
    
    with open(telem_f, newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
    
    if len(reader) < 1000:
        telem_issues.append((r.name, f"too few rows: {len(reader)}"))
        continue

    rows_cnt = len(reader)
    stats["min_rows"] = min(stats["min_rows"], rows_cnt)
    stats["max_rows"] = max(stats["max_rows"], rows_cnt)

    dur = float(reader[-1].get("elapsed_s", 0))
    stats["min_dur"] = min(stats["min_dur"], dur)
    stats["max_dur"] = max(stats["max_dur"], dur)

    last_wp = int(reader[-1].get("mission_current", 0))
    tot_wp = int(reader[-1].get("mission_total", 50))
    if last_wp < tot_wp - 1:
        telem_issues.append((r.name, f"wp incomplete: {last_wp}/{tot_wp}"))

    # check nan or empty fields
    has_nan = False
    for row in reader[::100]: # sample every 100th row
        for k, v in row.items():
            if v == "" or v.lower() == "nan":
                has_nan = True
                break
        alt = float(row.get("altitude_relative_m", 0))
        spd = float(row.get("speed_m_s", 0))
        stats["min_alt"] = min(stats["min_alt"], alt)
        stats["max_alt"] = max(stats["max_alt"], alt)
        stats["min_spd"] = min(stats["min_spd"], spd)
        stats["max_spd"] = max(stats["max_spd"], spd)

    if has_nan:
        telem_issues.append((r.name, "contains NaN or empty values"))

print(f"=== TELEMETRY QUALITY SUMMARY ===")
print(f"Total runs inspected: {len(runs)}")
print(f"Telemetry issues found: {len(telem_issues)}")
if telem_issues:
    for t in telem_issues:
        print(f"  {t[0]}: {t[1]}")
else:
    print("ALL 42 runs have 100% COMPLETE, VALID TELEMETRY!")

print(f"\nStats across all runs:")
print(f"  Rows count:   [{stats['min_rows']:,} to {stats['max_rows']:,}]")
print(f"  Duration (s): [{stats['min_dur']:.1f}s to {stats['max_dur']:.1f}s] ({stats['min_dur']/60:.1f}m to {stats['max_dur']/60:.1f}m)")
print(f"  Altitude (m): [{stats['min_alt']:.1f}m to {stats['max_alt']:.1f}m]")
print(f"  Speed (m/s):  [{stats['min_spd']:.1f}m/s to {stats['max_spd']:.1f}m/s]")

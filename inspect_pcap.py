import struct
import sys
from pathlib import Path

def parse_pcap(path):
    with open(path, "rb") as f:
        ghdr = f.read(24)
        if len(ghdr) < 24:
            return {"error": "file too small", "packets": 0}
        magic = struct.unpack("I", ghdr[:4])[0]
        if magic == 0xa1b2c3d4:
            endian = "<"
        elif magic == 0xd4c3b2a1:
            endian = ">"
        elif magic == 0xa1b23c4d: # nanosecond pcap
            endian = "<"
        elif magic == 0x4d3cb2a1:
            endian = ">"
        else:
            return {"error": f"unknown magic {hex(magic)}", "packets": 0}

        v_maj, v_min, tz, sigf, snaplen, linktype = struct.unpack(f"{endian}HHiIII", ghdr[4:24])

        packet_count = 0
        first_ts = None
        last_ts = None
        protocols = {}
        total_bytes = 0

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
            total_bytes += orig_len

            # parse IP header if possible
            # linktype: 1 = Ethernet (offset 14), 113 = SLL (offset 16), 276 = SLL2 (offset 20)
            ip_data = None
            if linktype == 1 and len(pdata) >= 14:
                eth_type = struct.unpack("!H", pdata[12:14])[0]
                if eth_type == 0x0800:
                    ip_data = pdata[14:]
            elif linktype == 113 and len(pdata) >= 16:
                proto_type = struct.unpack("!H", pdata[14:16])[0]
                if proto_type == 0x0800:
                    ip_data = pdata[16:]
            elif linktype == 276 and len(pdata) >= 20:
                proto_type = struct.unpack("!H", pdata[0:2])[0]
                if proto_type == 0x0800:
                    ip_data = pdata[20:]

            if ip_data and len(ip_data) >= 20:
                proto = ip_data[9]
                src = ".".join(map(str, ip_data[12:16]))
                dst = ".".join(map(str, ip_data[16:20]))
                key = f"proto_{proto}"
                protocols[key] = protocols.get(key, 0) + 1

        return {
            "linktype": linktype,
            "packets": packet_count,
            "total_bytes": total_bytes,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "span_s": (last_ts - first_ts) if (first_ts and last_ts) else 0,
            "protocols": protocols
        }

if __name__ == "__main__":
    campaign = Path("d:/nckh3/data/campaign_results")
    runs = sorted([d for d in campaign.glob("run_*") if d.is_dir()])
    problem_runs = []
    good_runs = []
    
    for run_p in runs:
        pcap_f = run_p / "c2_traffic.pcap"
        if not pcap_f.is_file():
            problem_runs.append((run_p.name, "missing pcap", 0, 0))
            continue
        res = parse_pcap(pcap_f)
        pkts = res.get("packets", 0)
        span = res.get("span_s", 0)
        sz = pcap_f.stat().st_size
        if pkts < 5000 or span < 300:
            problem_runs.append((run_p.name, f"{sz:,}B", pkts, f"{span:.1f}s"))
        else:
            good_runs.append((run_p.name, f"{sz/(1024*1024):.1f}MB", pkts, f"{span:.1f}s"))

    print(f"=== PCAP INSPECTION SUMMARY ===")
    print(f"Good runs (pkts >= 5000 and span >= 300s): {len(good_runs)}/{len(runs)}")
    print(f"Problematic runs: {len(problem_runs)}/{len(runs)}")
    if problem_runs:
        print("\nProblematic runs list:")
        for r in problem_runs:
            print(f"  {r[0]}: size={r[1]}, pkts={r[2]}, span={r[3]}")

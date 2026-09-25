# UAV C2 Traffic & Attack Dataset Generator (PX4 SITL)

Bộ mã nguồn điều khiển mô phỏng PX4 SITL và thu thập dữ liệu lưu lượng mạng C2 (UDP/MAVLink), telemetry, và nhãn tấn công cho UAV IDS.

## 📁 Cấu trúc thư mục

```text
generate/
├── attacks/                # Module giả lập tấn công (FLOOD, REPLAY, FDI, C2 Degradation,...)
├── collectors/             # Module bắt gói tin (pcap), telemetry MAVSDK, gazebo ground truth
├── missions/               # Module thực thi các kịch bản bay tự động (S1 - S4)
├── config/
│   └── scenario.yaml       # Cấu hình tham số mô phỏng, kịch bản bay và thông số tấn công
├── run_experiment.py       # Script điều phối chính để thu thập dataset
├── requirements.txt        # Các thư viện phụ thuộc
└── README.md
```

## 🚀 Hướng dẫn cài đặt & chạy

### 1. Cài đặt thư viện
```bash
pip install -r requirements.txt
sudo apt-get install wireshark-common tcpdump
```

### 2. Thiết lập môi trường PX4 SITL
```bash
export PX4_DIR="$HOME/PX4-Autopilot"
```

### 3. Chạy sinh dữ liệu
- Chạy toàn bộ các đợt thu thập:
  ```bash
  python run_experiment.py
  ```
- Chạy thử 1 lần bay (Pilot test):
  ```bash
  python run_experiment.py --only-run RUN_00001 --output-root data/pilot --fail-fast
  ```
- Hiển thị giao diện 3D Gazebo:
  ```bash
  export PX4_START_CMD="make px4_sitl gz_x500"
  python run_experiment.py
  ```

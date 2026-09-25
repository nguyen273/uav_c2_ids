# UAV C2 IDS DATASET (PHASE 3 FINAL PRODUCTION RELEASE)

> **Phiên bản:** v2.1.0 
> **Tổng số cửa sổ:** 59,380 windows (1.0s window, 0.5s stride)  
> **Tổng số thực nghiệm:** 156 runs (12 Benign + 144 Attack)  
> **Số cột:** 59 cột sạch 
> **Vantage Point:** GCS-side (Trạm điều khiển mặt đất thu/phát MAVLink UDP)

---

## 1. DANH MỤC CÁC FILE TRONG THƯ MỤC NÀY

| Tên File | Định Dạng | Số Dòng | Dung Lượng | Mô Tả |
| :--- | :---: | :---: | :---: | :--- |
| [`windows_1s.parquet`](windows_1s.parquet) | Parquet | 59,380 | ~8.20 MB | **Master Dataset dạng Parquet** (tối ưu tốc độ nạp & dung lượng). |
| [`windows_1s.csv`](windows_1s.csv) | CSV | 59,380 | ~38.15 MB | **Master Dataset dạng CSV** (phù hợp mở bằng Excel/Pandas/R). |
| [`train.parquet`](train.parquet) | Parquet | 41,426 | ~5.75 MB | Tập **Huấn luyện (Train)** sạch (110 runs, 0 transition). |
| [`val.parquet`](val.parquet) | Parquet | 9,840 | ~1.48 MB | Tập **Validation** sạch để tune ngưỡng & siêu tham số (26 runs, 0 transition). |
| [`test.parquet`](test.parquet) | Parquet | 7,538 | ~1.13 MB | Tập **Kiểm thử (Test)** chính (20 runs, 0 transition, freeze evaluation). |
| [`transition.parquet`](transition.parquet) | Parquet | 576 | ~152 KB | Tập **Cửa sổ chuyển tiếp (Transition)** độc lập để đo độ trễ phát hiện thời gian thực (Time-to-First-Alert). |
| [`split_manifest.csv`](split_manifest.csv) | CSV | 156 | ~6.7 KB | Bảng ánh xạ phân vùng Run-level cho toàn bộ 156 runs. |
| [`dataset_manifest.json`](dataset_manifest.json) | JSON | - | ~4.9 KB | Metadata chi tiết, đặc tả 3 Feature Views và các báo cáo nhãn. |

---

## 2. PHÂN VÙNG DỮ LIỆU (RUN-LEVEL STRATIFIED PARTITION)

Dữ liệu được chia theo cấp độ Run (Run-Level Split) để triệt tiêu hoàn toàn rò rỉ thời gian giữa các cửa sổ liền kề:

```
[ Toàn bộ 59,380 Cửa Sổ (156 Runs) ]
   │
   ├── [ 576 Transition Windows ] ──────────► transition.parquet (Đo Detection Delay)
   │
   └── [ 58,804 Normal Evaluation Windows ]
         ├── Train (70%): 41,426 windows (110 runs) ──► train.parquet
         ├── Val   (15%):  9,840 windows (26 runs)  ──► val.parquet
         └── Test  (15%):  7,538 windows (20 runs)  ──► test.parquet
```

---

## 3. CÁC HỆ THỐNG NHÃN (LABEL SCHEMES)

### A. Nhị phân (Binary IDS - Track E1)
* `label_binary`: `0` (Benign - 41,812 windows) | `1` (Attack - 17,568 windows).

### B. 6 Nhãn Chi Tiết (Fine-grained - Track E2)
* `label_attack`: `FLOOD` (2,928), `REPLAY` (2,928), `FDI_GCS_STATE_SPOOF` (2,928), `FDI_GCS_POSITION_DRIFT` (2,928), `C2_DEGRADATION` (2,928), `MAVLINK_ANOMALY` (2,928), `BENIGN` (41,812).

### C. 5 Nhóm Họ Tấn Công (Coarse-grained Families - Track E3.1 LOGO)
* Gộp `FDI_GCS_STATE_SPOOF` + `FDI_GCS_POSITION_DRIFT` $\rightarrow$ họ **`FDI`** (5,856 windows).
* 5 họ: `FLOOD`, `REPLAY`, `FDI`, `C2_DEGRADATION`, `MAVLINK_ANOMALY`.

---

## 4. BA KHÔNG GIAN ĐẶC TRƯNG (FEATURE VIEWS)

1. **$X_{\text{baseline}}$ (32 đặc trưng)**: Đặc trưng lưu lượng mạng, IAT, MAVLink syntax và Telemetry chuẩn (đã bỏ `temperature_delta` 100% NaN).
2. **$X_{\text{pruned}}$ (24 đặc trưng - Compact Set)**: Bộ đặc trưng tinh gọn loại bỏ hằng số và cộng tuyến, tối ưu cho UAV Edge Computer:
   * *Traffic Dynamics (8)*: `packet_rate`, `byte_rate`, `pkt_len_mean`, `pkt_len_std`, `iat_mean`, `iat_std`, `iat_max`, `iat_p95`.
   * *MAVLink Semantics (8)*: `mavlink_msg_rate`, `mavlink_unique_msg_count`, `mavlink_msg_entropy`, `mavlink_seq_gap_mean`, `mavlink_seq_gap_std`, `mavlink_duplicate_seq_ratio`, `mavlink_command_rate`, `gcs_to_uav_ratio`.
   * *UAV Kinematics (8)*: `altitude_mean`, `altitude_std`, `speed_mean`, `speed_std`, `roll_abs_mean`, `pitch_abs_mean`, `yaw_rate_mean`, `battery_delta`.
3. **$X_{\text{extended}}$ (34 đặc trưng)**: $X_{\text{baseline}}$ bổ sung 2 đặc trưng ngữ nghĩa GCS có thông tin: `landed_state_on_ground_ratio`, `position_altitude_drift_rate` (loại bỏ 2 trường hằng số std=0: `landed_state_transition_count`, `landed_vs_motion_inconsistency`).

---

## 5. DANH SÁCH TOÀN BỘ 23 CỘT CẤM ĐƯA VÀO MA TRẬN ĐẦU VÀO X (ANTI-LEAKAGE BLACKLIST)

Để ngăn chặn tuyệt đối **rò rỉ dữ liệu (Data Leakage)** và **học đường tắt (Shortcut Learning)**, khi huấn luyện và kiểm thử bất kỳ mô hình Machine Learning nào, bạn loại bỏ các cột sau đây ra khỏi ma trận đầu vào $X$:

### 📌 Danh Sách 23 Cột Cấm Dưới Dạng Mảng Python (Copy-Paste Sẵn):
```python
EXCLUDED_COLUMNS = [
    # 1. Rò rỉ Ground-Truth & Nhãn Mục Tiêu (6 cột)
    "attack_overlap_ratio",
    "label_binary",
    "label_attack",
    "attack_type",
    "attack_variant",
    "severity",

    # 2. Metadata Định Danh Thực Nghiệm (8 cột)
    "run_id",
    "scenario_id",
    "seed",
    "split",
    "window_index",
    "n_windows",
    "capture_vantage",
    "capture_port",

    # 3. Mốc Thời Gian Tuyệt Đối (2 cột)
    "window_start_epoch",
    "window_end_epoch",

    # 4. Cờ Trạng Thái Cửa Sổ & Pipeline Internal QC (7 cột)
    "transition",
    "edge_window",
    "telemetry_interpolated",
    "telemetry_stale",
    "n_telemetry_rows",
    "n_mavlink_decoded",
    "n_mavlink_failed"
]
```

### 📋 Bảng Chi Tiết Từng Cột Và Lý Do Bắt Buộc Loại Bỏ:

| STT | Tên Cột | Kiểu Dữ Liệu | Nhóm Phân Loại | Lý Do Bắt Buộc Loại Khỏi Ma Trận Đầu Vào $X$ |
| :---: | :--- | :---: | :--- | :--- |
| **1** | `attack_overlap_ratio` | `float64` | Rò rỉ Ground-Truth | **CỰC KỲ NGUY HIỂM**: Tỷ lệ giao thoa thời gian với cuộc tấn công ($0.0$ ở Benign, $>0.0$ ở Attack). Nếu đưa vào $X$, mô hình sẽ dùng cột này làm đường tắt (shortcut) gian lận nhãn thay vì học bản chất lưu lượng/kinematics. |
| **2** | `label_binary` | `int64` | Nhãn mục tiêu | Nhãn mục tiêu của bài toán phân loại nhị phân ($0$: Normal, $1$: Attack). |
| **3** | `label_attack` | `string` | Nhãn mục tiêu | Nhãn mục tiêu của bài toán phân loại 6 lớp tấn công chi tiết. |
| **4** | `attack_type` | `string` | Nhãn thực nghiệm | Nhãn họ tấn công gốc được cấu hình từ kịch bản thực nghiệm. |
| **5** | `attack_variant` | `string` | Nhãn thực nghiệm | Tên biến thể tấn công chi tiết (`alt_drift_slow`, `state_arm_spoof`...). |
| **6** | `severity` | `string` | Nhãn thực nghiệm | Mức độ cường độ tấn công (`low`, `medium`, `high`). |
| **7** | `run_id` | `string` | Metadata định danh | Mã chuyến bay (`RUN_00001`...). Model sẽ học vẹt ID nếu đưa vào. |
| **8** | `scenario_id` | `string` | Metadata kịch bản | Mã kịch bản bay định trước (`S01`, `S02`, `S03`, `S04`). |
| **9** | `seed` | `int64` | Metadata thực nghiệm | Hạt giống ngẫu nhiên của lần chạy. |
| **10** | `split` | `string` | Metadata phân vùng | Nhãn phân vùng dữ liệu (`train`, `val`, `test`, `transition`). |
| **11** | `window_index` | `int64` | Metadata thời gian | Thứ tự thời gian của cửa sổ trong chuyến bay. |
| **12** | `n_windows` | `int64` | Metadata thực nghiệm | Tổng số cửa sổ của chuyến bay. |
| **13** | `capture_vantage` | `string` | Metadata cảm biến | Điểm đặt cảm biến thu thập (`gcs_side` - hằng số). |
| **14** | `capture_port` | `int64` | Metadata mạng | Cổng mạng thu thập (cổng UDP 14550 / 14551). |
| **15** | `window_start_epoch` | `float64` | Mốc thời gian | Timestamp Unix tuyệt đối theo giây của host. Model có thể ghi nhớ thời điểm thực diễn ra tấn công. |
| **16** | `window_end_epoch` | `float64` | Mốc thời gian | Timestamp kết thúc cửa sổ. |
| **17** | `transition` | `bool` | Cờ trạng thái cửa sổ | Cờ đánh dấu cửa sổ chuyển tiếp ranh giới. Dùng để tách bài test đo độ trễ riêng, không làm đặc trưng. |
| **18** | `edge_window` | `bool` | Cờ trạng thái cửa sổ | Cờ đánh dấu cửa sổ rìa đầu/cuối của chuyến bay. |
| **19** | `telemetry_interpolated` | `bool` | Pipeline QC nội bộ | Cờ trạng thái nội suy telemetry (QC nội bộ). |
| **20** | `telemetry_stale` | `bool` | Pipeline QC nội bộ | Cờ cảnh báo telemetry trễ hơn 400ms (QC nội bộ). |
| **21** | `n_telemetry_rows` | `int64` | Pipeline QC nội bộ | Số mẫu telemetry thu được trong 1 giây (metadata kiểm định). |
| **22** | `n_mavlink_decoded` | `int64` | Pipeline QC nội bộ | Số gói tin MAVLink giải mã thành công (metadata kiểm định). |
| **23** | `n_mavlink_failed` | `int64` | Pipeline QC nội bộ | Số gói tin MAVLink bị lỗi giải mã (metadata kiểm định). |

> **Quy Tắc Vàng**: Ma trận đầu vào $X$ **CHỈ ĐƯỢC PHÉP CHỨA** các cột đặc trưng nằm trong 1 trong 3 Feature Views được định nghĩa tại Phần 4 ($X_{\text{baseline}}: 32$, $X_{\text{pruned}}: 24$, hoặc $X_{\text{extended}}: 34$). Toàn bộ 23 cột trên chỉ dùng làm nhãn mục tiêu $y$ hoặc metadata kiểm định sau dự đoán.

---

## 6. HƯỚNG DẪN NẠP DỮ LIỆU NHANH TRONG PYTHON

### Cách 1: Nạp Đặc Trưng Theo Feature View Cụ Thể (Khuyên Dùng)
```python
import pandas as pd
import json

# 1. Đọc dữ liệu phân vùng sạch
train_df = pd.read_parquet("train.parquet")
test_df  = pd.read_parquet("test.parquet")

# 2. Đọc danh sách đặc trưng từ manifest
with open("dataset_manifest.json") as f:
    manifest = json.load(f)

# Chọn Feature View muốn dùng: 'X_baseline' (32), 'X_pruned' (24), hoặc 'X_extended' (34)
feature_cols = manifest["feature_views"]["X_pruned"]["features"]

# 3. Tách ma trận đầu vào X và nhãn y
X_train = train_df[feature_cols]
y_train = train_df["label_binary"]

X_test  = test_df[feature_cols]
y_test  = test_df["label_binary"]

print(f"X_train shape: {X_train.shape}, y_train shape: {y_train.shape}")
print(f"X_test shape:  {X_test.shape},  y_test shape:  {y_test.shape}")
```

### Cách 2: Loại Bỏ Toàn Bộ 23 Cột Cấm Bằng Danh Sách `EXCLUDED_COLUMNS`
```python
import pandas as pd

train_df = pd.read_parquet("train.parquet")

EXCLUDED_COLUMNS = [
    "attack_overlap_ratio", "label_binary", "label_attack", "attack_type",
    "attack_variant", "severity", "run_id", "scenario_id", "seed", "split",
    "window_index", "n_windows", "capture_vantage", "capture_port",
    "window_start_epoch", "window_end_epoch", "transition", "edge_window",
    "telemetry_interpolated", "telemetry_stale", "n_telemetry_rows",
    "n_mavlink_decoded", "n_mavlink_failed"
]

# Tách toàn bộ đặc trưng còn lại (36 đặc trưng ứng viên)
X_train_all = train_df.drop(columns=EXCLUDED_COLUMNS)
y_train = train_df["label_binary"]

print(f"Features count: {X_train_all.shape[1]}") # Đúng 36 features
```

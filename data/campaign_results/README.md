# UAV C2 Campaign Results Directory (120 Runs)

This directory corresponds to the raw multi-run execution campaign of the Cyber-Physical UAV C2 Digital Twin.

## Campaign Matrix Design
The campaign consists of **120 full flight runs** combining:
- **3 Missions**: `F1`, `F4`, `F7` (derived from empirical AERPAW AFAR GPS trajectories)
- **4 Channel Scenarios**:
  - `C1_LOS`: Line-of-sight propagation profile
  - `C2_Moderate`: Moderate shadowing and path-loss profile
  - `C3_NLOS`: Non-line-of-sight attenuation profile
  - `C4_Dynamic`: Time-varying dynamic channel state profile
- **2 Traffic Regimes**:
  - `T0_Benign`: Nominal MAVLink C2 command, telemetry, and control traffic
  - `T4_DoS`: High-rate UDP flood targeting the GCS MAVLink C2 endpoint
- **5 Seeds per combination**: $3 \times 4 \times 2 \times 5 = 120$ runs total.

## Per-Run Artifacts
Each run directory contains:
- `c2_traffic.pcap`: Raw network packet capture on the C2 link
- `flight_telemetry.csv`: High-frequency MAVSDK flight telemetry (position, velocity, altitude, distance to GCS)
- `attack_events.jsonl`: Ground-truth timestamps and parameters of injected attack intervals
- `twin_scope.json`: Complete simulation and channel profile declaration
- `metadata.json`: Provenance hashes, execution parameters, and environment tags
- Process logs: `gcs.log`, `px4.log`, `mavlink-router.log`, `tcpdump.log`

## Accessing Full Raw PCAPs
Due to the multi-gigabyte size (~5.3 GB raw, ~2.3 GB compressed) of the complete packet capture archive:
- The full archive `campaign_results_kaggle.zip` is hosted on Kaggle: [UAV C2 PCAP Dataset](https://www.kaggle.com/datasets/nguyen273mmb/uav-c2-pcap).
- To run the baseline evaluation without storing 5.3 GB locally, refer to [`UAV_C2_Baseline_Kaggle.ipynb`](../../UAV_C2_Baseline_Kaggle.ipynb) or [`test.ipynb`](../../test.ipynb).
- To reproduce runs locally using PX4 SITL and Containernet, execute:
  ```bash
  bash src/pipeline/run_all.sh
  ```

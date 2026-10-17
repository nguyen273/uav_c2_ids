#!/bin/bash
# run_all.sh
# Dataset-32 Layer 1 fit, optional AADM evaluation, then SITL/IDS.

set -e

# Đảm bảo script luôn chạy đúng thư mục gốc chứa src/pipeline
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=========================================="
echo " Starting PX4 SITL Cyber-Physical Twin Pipeline "
echo " (3.4 GHz core + separate 3.6 GHz/5G evidence; target C2 qdisc remains stress-only) "
echo "=========================================="
echo "Working directory: $SCRIPT_DIR"

cd "$SCRIPT_DIR"

# Tạo thư mục đầu ra
mkdir -p "$PROJECT_ROOT/data/processed"
mkdir -p "$PROJECT_ROOT/data/model_fits"
mkdir -p "$PROJECT_ROOT/data/missions"
mkdir -p "$PROJECT_ROOT/data/campaign_results"

DATASET32_DIR="${DATASET32_DIR:-$PROJECT_ROOT/Dataset-32 AERPAW Air-to-Ground Channel Sounding and Path Loss Measurements at 3.4 GHz with Linear and Zigzag UAV Trajectories}"
if [[ -f "$PROJECT_ROOT/data/model_fits/layer1_dataset32_pathloss_fit.json" && -f "$PROJECT_ROOT/data/processed/dataset32/dataset32_pathloss.csv" ]]; then
    echo -e "\n--- P2/P3: Layer 1 fit already exists in data/model_fits, skipping raw Dataset-32 reprocessing ---"
elif [[ -d "$DATASET32_DIR" ]]; then
    echo -e "\n--- P2/P3: Validate Dataset-32 and fit flight-grouped 3.4 GHz path-loss model ---"
    python3 P2_prepare_dataset32.py --dataset_dir "$DATASET32_DIR" \
        --output_dir "$PROJECT_ROOT/data/processed/dataset32"
    python3 P3_layer1_dataset32.py \
        --input_csv "$PROJECT_ROOT/data/processed/dataset32/dataset32_pathloss.csv" \
        --output_dir "$PROJECT_ROOT/data/model_fits"
else
    echo "ERROR: Neither Layer 1 fit nor Dataset-32 directory found!" >&2
    exit 1
fi
P8_LAYER1_ARGS=()

# Additional empirical datasets are processed as separate, frequency- and
# technology-specific evidence. They do not alter P8's 3.4 GHz Dataset-32 gate.
A2G36_DIR="${A2G36_DIR:-$PROJECT_ROOT/Measured A2G Channel Dataset (CIR) in Multiple Sce}"
if [[ -d "$A2G36_DIR/Appendix/PL_data" ]]; then
    echo -e "\n--- Independent 3.6 GHz multi-scenario propagation branch ---"
    python3 P2_prepare_a2g36.py --dataset_dir "$A2G36_DIR" \
        --output_dir "$PROJECT_ROOT/data/processed/a2g36"
    python3 P3_layer1_a2g36.py --input_csv "$PROJECT_ROOT/data/processed/a2g36/a2g36_pathloss.csv" \
        --output_dir "$PROJECT_ROOT/data/model_fits"
else
    echo "3.6 GHz A2G dataset not found at $A2G36_DIR; independent branch skipped." >&2
fi

AERPAW_5G_DIR="${AERPAW_5G_DIR:-$PROJECT_ROOT/Dryad}"
if [[ -d "$AERPAW_5G_DIR/yaw45" && -d "$AERPAW_5G_DIR/yaw315" ]]; then
    echo -e "\n--- Ericsson 5G NSA empirical KPI branch ---"
    python3 P2_prepare_aerpaw5g.py --dataset_dir "$AERPAW_5G_DIR" \
        --output_dir "$PROJECT_ROOT/data/processed/aerpaw5g"
    python3 P3_evaluate_aerpaw5g.py --input_csv "$PROJECT_ROOT/data/processed/aerpaw5g/aerpaw_5g_kpis.csv" \
        --output_dir "$PROJECT_ROOT/data/model_fits"
else
    echo "AERPAW 5G dataset not found at $AERPAW_5G_DIR; KPI branch skipped." >&2
fi

PAWPRINTS4G_DIR="${PAWPRINTS4G_DIR:-$PROJECT_ROOT/AERPAW_PawPrints_SemiCircles}"
if [[ -f "$PAWPRINTS4G_DIR/Logs/pawprints_4G.csv" ]]; then
    echo -e "\n--- Pawprints 4G LTE KPI reference branch (not C2 calibration) ---"
    python3 P2_prepare_pawprints4g.py --dataset_dir "$PAWPRINTS4G_DIR" \
        --output_dir "$PROJECT_ROOT/data/processed/pawprints4g"
    python3 P3_evaluate_pawprints4g.py \
        --input_csv "$PROJECT_ROOT/data/processed/pawprints4g/pawprints4g_kpis.csv" \
        --output_dir "$PROJECT_ROOT/data/model_fits"
else
    echo "Pawprints 4G dataset not found at $PAWPRINTS4G_DIR; LTE reference branch skipped." >&2
fi

if [[ "${LAYER1_ONLY:-0}" == "1" ]]; then
    python3 P4_layer2_mobility.py \
        --afar_dir "$PROJECT_ROOT/AFAR  2023_SigMF" \
        --output_dir "$PROJECT_ROOT/data/missions"
    python3 P11_calibration_card.py --base_dir "$PROJECT_ROOT"
    echo "Reference-data preparation/evaluation finished. Artifacts are under $PROJECT_ROOT/data/processed and $PROJECT_ROOT/data/model_fits."
    exit 0
fi

if [[ -z "${AADM_PROCESSED_CSV:-}" && -d "$PROJECT_ROOT/AADM2025Dryad/USRP" ]]; then
    echo -e "\n--- Process AADM GPS, USRP and controller report logs ---"
    python3 "$PROJECT_ROOT/uav-cas/Scripts/0-step1_process_aadm.py" \
        --aadm_dir "$PROJECT_ROOT/AADM2025Dryad/USRP" \
        --output_dir "$PROJECT_ROOT/data/processed/aadm" --no_plots
    AADM_PROCESSED_CSV="$PROJECT_ROOT/data/processed/aadm/aadm_testbed.csv"
fi
if [[ -n "${AADM_PROCESSED_CSV:-}" ]]; then
    echo -e "\n--- P6: Evaluate AADM held-out-flight SNR and measured rate ---"
    python3 P6_evaluate_layer3_aadm.py --input_csv "$AADM_PROCESSED_CSV" --output_dir "$PROJECT_ROOT/data/model_fits"
else
    echo "AADM evaluation skipped: set AADM_PROCESSED_CSV or provide AADM2025Dryad/USRP." >&2
fi
if [[ -f "$PROJECT_ROOT/data/processed/aadm/aadm_development.csv" && -f "$PROJECT_ROOT/data/processed/aadm/aadm_testbed.csv" ]]; then
    echo -e "\n--- P12: Compare AADM development-DT and testbed distributions by flight ---"
    python3 P12_compare_aadm_domain.py \
        --development_csv "$PROJECT_ROOT/data/processed/aadm/aadm_development.csv" \
        --testbed_csv "$PROJECT_ROOT/data/processed/aadm/aadm_testbed.csv" \
        --output_dir "$PROJECT_ROOT/data/model_fits"
fi

if [[ -f "$PROJECT_ROOT/data/model_fits/layer2_afar_mobility_calibration.json" && -f "$PROJECT_ROOT/data/missions/missions_manifest.json" ]]; then
    echo -e "\n--- P4/P14: Layer 2 mobility fit and missions already exist in data/, skipping raw AFAR reprocessing ---"
elif [[ -d "$PROJECT_ROOT/AFAR  2023_SigMF" ]]; then
    echo -e "\n--- P4: Preparing deterministic sampled AFAR GPS traces ---"
    python3 P4_layer2_mobility.py \
        --afar_dir "$PROJECT_ROOT/AFAR  2023_SigMF" \
        --output_dir "$PROJECT_ROOT/data/missions"
    python3 P14_calibrate_layer2_mobility.py \
        --missions_dir "$PROJECT_ROOT/data/missions" \
        --output_dir "$PROJECT_ROOT/data/model_fits"
else
    echo "ERROR: Neither Layer 2 mobility fit nor AFAR directory found!" >&2
    exit 1
fi
python3 P11_calibration_card.py --base_dir "$PROJECT_ROOT"

echo -e "\n--- P8: Running PX4 SITL + Gazebo + MAVLink campaign ---"
P8_WORKER_ARGS=()
if [[ -n "${WORKER_ID:-}" && -n "${NUM_WORKERS:-}" ]]; then
    P8_WORKER_ARGS=(--worker_id "$WORKER_ID" --num_workers "$NUM_WORKERS")
fi

P8_SYNC_ARGS=()
if [[ -n "${SYNC_TARGET:-}" ]]; then
    P8_SYNC_ARGS=(--sync_target "$SYNC_TARGET")
fi

# Dùng sudo nếu cần (đã thiết kế để fail-closed nếu không có root)
# Truyền trực tiếp SYNC_TARGET và cờ --sync_target để tránh mất env khi sudo bỏ qua -E
sudo SYNC_TARGET="${SYNC_TARGET:-}" python3 P8_run_campaign.py \
    --base_dir "$PROJECT_ROOT" \
    --output_dir "$PROJECT_ROOT/data/campaign_results" \
    "${P8_WORKER_ARGS[@]}" \
    "${P8_SYNC_ARGS[@]}" \
    "${P8_LAYER1_ARGS[@]}"

if [[ "${SKIP_POST_CAMPAIGN:-0}" == "1" || ( -n "${NUM_WORKERS:-}" && "${NUM_WORKERS:-1}" -gt 1 ) ]]; then
    echo -e "\nWorker run completed for worker ${WORKER_ID:-0}/${NUM_WORKERS}."
    echo "P9/P10 skipped on worker node. Aggregate all worker outputs before running P9 and P10."
    exit 0
fi

echo -e "\n--- P9: Dataset Schema Extraction (PCAP/Flow Generation) ---"
python3 P9_dataset_schema.py \
    --campaign_dir "$PROJECT_ROOT/data/campaign_results"

echo -e "\n--- P10: IDS Evaluation ---"
python3 P10_ids.py \
    --campaign_dir "$PROJECT_ROOT/data/campaign_results"

echo -e "\n=========================================="
echo " Pipeline Finished Successfully           "
echo "=========================================="

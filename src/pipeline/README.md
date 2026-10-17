# UAV-CAS cyber-physical twin pipeline

The repository contains a **PX4 SITL + Gazebo + MAVSDK/MAVLink + Containernet** execution path. Empirical reference processing is branched by domain: Dataset-32 is the 3.4 GHz path-loss trend fit; the 3.6 GHz CIR dataset is a separate multi-scenario branch; Ericsson 5G NSA and Pawprints 4G are separate cellular KPI references. None is silently transferred into MAVLink qdisc calibration.

This is a **simulation/emulation UAV C2 prototype with domain-specific empirical references**, not a calibrated physical UAV or calibrated end-to-end C2 twin. The 3.4 and 3.6 GHz branches fit path-loss trends in their source domains; they are not active RF propagation in SITL. Ericsson 5G, Pawprints 4G, and AADM provide independent radio/link KPI references, not MAVLink C2 measurements. P5/P7 qdisc profiles remain declared stress assumptions. T4 is a bounded UDP flood against the GCS MAVLink UDP endpoint, not proof of flight-control compromise. Replay/FDI/MITM are excluded until implemented as protocol-correct MAVLink attacks.

## Input/output contract

- P2/P3 Layer 1: `P2_prepare_dataset32.py` validates all four Dataset-32 CSV/SigMF metadata pairs and creates a provenance manifest. `P3_layer1_dataset32.py` fits a flight-fixed-effects log-distance slope with equal flight weighting and exact leave-one-flight-out validation. Holdout scores are demeaned within the held-out flight and validate only relative distance trend. The artifact is not absolute power/path-loss prediction, SNR, PER, C2 delay, packet loss or goodput.
- AADM: the processor parses controller `Report` events (timestamp, serving BS, SNR, reported data-mule rate, cumulative received data). P6 evaluates held-out-flight link KPIs; P12 compares flight-level distributions between development Digital Twin and real testbed domains when both are available. These are external AADM comparisons, not validation of this project’s C2 twin or MAVLink calibration.
- Independent 3.6 GHz A2G: `P2_prepare_a2g36.py` extracts source-provided Appendix `PL_data` arrays into scenario/altitude-tagged rows and summarizes CIR workbook snapshots separately. `P3_layer1_a2g36.py` fits a separate within-group distance slope and leaves one scenario-altitude group out. It is never pooled with 3.4 GHz Dataset-32. Group-offset demeaning validates relative trend only, not an absolute intercept or a general altitude law.
- Ericsson 5G NSA: `P2_prepare_aerpaw5g.py` aligns LTE/NR RSRP with SINR and aligns available NR CQI/MCS/throughput by nearest timestamps (2 s maximum), preserves source altitude as logged, and derives horizontal distance to the documented LW1 coordinate. `P3_evaluate_aerpaw5g.py` reports orientation-held-out RSRP distance trend and NR throughput-vs-SINR checks. The two orientations are from one campaign, so these are exploratory within-campaign checks, not broad independent validation and not C2 calibration.
- Pawprints 4G: `P2_prepare_pawprints4g.py` preserves source LTE RSRP/RSRQ/RSSI/PCI/timing-advance fields, hashes the input, and derives horizontal distance to the published base-station coordinate. `P3_evaluate_pawprints4g.py` reports contiguous-time-block holdout for a distance–RSRP trend from connected rows, with PCI-specific fixed intercepts to reduce cell-reselection confounding. It is one flight and a cellular KPI reference only; it has no throughput, packet loss, RTT, jitter, or MAVLink traffic.
- P4: all valid AFAR testbed GPS traces are retained in `data/missions` with hashes, location IDs, and train/holdout assigned by the pipeline's top-level source-directory ID. This is a pipeline-defined split key, not asserted to be the dataset's campaign identifier. P8 defaults to F1/F4/F7 from three distinct training source groups and enforces manifest partitions. P14 fits a scalar-speed Gauss-Markov model (`alpha`, mean speed, innovation spread), a 95th-percentile turn-rate cap, and an equal-trace median altitude profile from train groups. P8/P7 use seeded per-waypoint speeds, the turn-rate speed cap at corners, and the train-fitted altitude profile in PX4 mission items. The MAVSDK logger records in-air state and mission progress. P13 separately scores completed SITL runs against route geometry using horizontal cross-track and altitude-profile error after the 15 m mission offset. AFAR timestamps are not treated as desired PX4 timing. This is AFAR-calibrated waypoint speed plus simulated route-tracking evaluation, not full or physical flight-dynamics validation.
- P5/P7/P8: Dataset-32's 3.4 GHz value is a reference-domain gate/metadata label; SITL does not simulate a 3.4 GHz RF carrier. A named fixed stress profile drives `tc/netem`; position does not change delay/loss/rate. These profiles are not empirical RF-to-network or C2 calibration.
- P14/P13 Layer 2: `P14_calibrate_layer2_mobility.py` fits training-group velocity dynamics/distributions and reports Jensen-Shannon distribution shift on held-out AFAR source groups; the fitted scalar-speed Gauss-Markov process generates waypoint speed commands in P7. P13 scores completed SITL route tracking on holdout traces. These are distinct checks; neither calibrates swarm topology or full aircraft dynamics.
- P7 artifacts per run: `c2_traffic.pcap`, `flight_telemetry.csv`, `attack_events.jsonl`, `twin_scope.json`, process logs and best-effort ULog copy.
- P9 requires packet capture, event file, and live telemetry; outputs packet/flow/window Parquet views. Telemetry is live MAVSDK position data with speed/distance derived in the GCS logger.
- P10 reports grouped scenario holdout and leave-one-channel-out results for network-only, telemetry-only, and combined feature sets. P9 adds packet/byte rates, packet-length and interarrival summaries, UDP fraction, and active-flow counts. These are IP/UDP window features; payloads are not decoded as MAVLink, and IDS performance does not establish twin fidelity.

## Required runtime image

P7 deliberately requires an image named `uav-cas-sitl:latest` (override with `UAV_CAS_SITL_IMAGE`) containing all of the following: `/PX4-Autopilot` source with `make px4_sitl gz_x500` buildable, Gazebo Garden dependencies/models, `mavlink-routerd`, Python 3 + `mavsdk`, `tcpdump`, `hping3`, and a shell. Build the supplied image on Linux from the repository root with `docker build -t uav-cas-sitl:latest -f src/pipeline/Dockerfile.sitl .`; this performs a large PX4/Gazebo build and has not been executed here. **The existing `uav-cas-node:latest` image is insufficient**: its Dockerfile does not install PX4, Gazebo, MAVSDK, or mavlink-router. The Linux host needs Docker, Mininet/Containernet, Mininet Docker support, `tc`, root privileges, Python dependencies, and the source datasets in expected project-root locations. Configure Docker/Containernet to allow the node networking and qdisc operations.

Modern Gazebo SITL command used here is `HEADLESS=1 make px4_sitl gz_x500`. PX4 documents this target for Gazebo on Ubuntu 22.04, with `PX4_HOME_LAT/LON/ALT` to set the origin. The previous `make px4_sitl gazebo` was Gazebo Classic and has been removed. Build/pre-provision the required image in Linux before running; Windows native is not supported for this P7 path.

The default `run_all.sh` path now uses Dataset-32 CSV labels, not the older AERPAW A2G raw-IQ folder. It validates/consolidates Dataset-32 and fits Layer 1, then runs AADM evaluation, route preparation and (on a configured Linux host) SITL. Install pipeline requirements first:

```sh
python3 -m pip install -r src/pipeline/requirements.txt
bash src/pipeline/run_all.sh
```

For reference-data preparation/evaluation without entering the long SITL campaign:

```sh
LAYER1_ONLY=1 bash src/pipeline/run_all.sh
```

To generate and inspect only the Dataset-32 Layer 1 artifact on any supported Python host:

```sh
python3 src/pipeline/P2_prepare_dataset32.py \
  --dataset_dir "Dataset-32 AERPAW Air-to-Ground Channel Sounding and Path Loss Measurements at 3.4 GHz with Linear and Zigzag UAV Trajectories" \
  --output_dir data/processed/dataset32
python3 src/pipeline/P3_layer1_dataset32.py \
  --input_csv data/processed/dataset32/dataset32_pathloss.csv \
  --output_dir data/model_fits
```

Dataset-32 provides four flights (two repeated linear and two repeated zigzag trajectories) at 3.4 GHz, with source-processed 3D distance and `path_loss_db` in CSV. P3 removes a separate level offset per flight while estimating the distance slope, because the flights have substantially different path-loss levels. Only four independent flights support validation; dense rows within a flight are not independent replicates. Review `layer1_dataset32_pathloss_fit.json` and its flight-level holdout metrics before campaign use. P8 rejects any requested frequency outside the measured 3.4 GHz support. The qdisc mapping remains assumed.

Additional reference artifacts:

- `data/processed/a2g36/a2g36_pathloss.csv`, `a2g36_cir_snapshot_summary.csv`, and `a2g36_manifest.json`; fit report `data/model_fits/layer1_a2g36_pathloss_fit.json`.
- `data/processed/aerpaw5g/aerpaw_5g_kpis.csv` and `aerpaw_5g_manifest.json`; evaluation `data/model_fits/aerpaw_5g_kpi_holdout.json`.
- `data/processed/pawprints4g/pawprints4g_kpis.csv` and `pawprints4g_manifest.json`; single-flight LTE exploratory report `data/model_fits/pawprints4g_rsrp_holdout.json`.
- Set `A2G36_DIR` or `AERPAW_5G_DIR` if the dataset folders are elsewhere. Source folders are read-only inputs to the pipeline.
- Set `PAWPRINTS4G_DIR` if `AERPAW_PawPrints_SemiCircles/Logs/pawprints_4G.csv` is stored elsewhere.
- Ericsson 5G NSA citation: Asokan et al. (2025), [Dryad DOI 10.5061/dryad.wh70rxx06](https://doi.org/10.5061/dryad.wh70rxx06). Its README specifies throughput in Mbps and a 30 m campaign; logged altitude values are retained without imposing an AGL conversion.
- Pawprints 4G citation: Singh et al. (2025), [Dryad DOI 10.5061/dryad.gmsbcc30x](https://doi.org/10.5061/dryad.gmsbcc30x). The processed copy has 2,021 rows and 1,217 connected rows. Its one-flight block holdout is exploratory; it contains no target C2 packet KPIs.
- 3.6 GHz A2G citation: Li et al. (2025), “Air-to-ground channel dataset via UAV-aided measurements in multiple scenarios,” Data in Brief 63, 112086, [DOI 10.1016/j.dib.2025.112086](https://doi.org/10.1016/j.dib.2025.112086); data [DOI 10.17632/8p6r8ft4fz.2](https://doi.org/10.17632/8p6r8ft4fz.2).

### Layer 2 train-only fit and holdout evaluation

First rebuild the mobility calibration artifact from train source groups. The holdout group is used only for distribution scoring and later evaluation, never model fitting:

```sh
python3 src/pipeline/P14_calibrate_layer2_mobility.py --missions_dir data/missions --output_dir data/model_fits
sudo -E python3 src/pipeline/P8_run_campaign.py --base_dir "$PWD" --output_dir "$PWD/data/mobility_holdout_results" --partition holdout --channel C1_LOS --traffic T0_Benign --repetitions 3
python3 src/pipeline/P13_evaluate_layer2_mobility.py --campaign_dir data/mobility_holdout_results --missions_dir data/missions --partition holdout --output "$PWD/data/model_fits/layer2_mobility_holdout.json"
python3 src/pipeline/P15_kinematic_milestones.py --campaign_dir data/mobility_holdout_results --missions_dir data/missions --partition holdout --p13_report data/model_fits/layer2_mobility_holdout.json --output data/model_fits/layer2_kinematic_milestones.json
```

P13 compares telemetry positions to the nearest point on the AFAR route polyline and compares altitude to the interpolated route altitude plus the mission’s 15 m offset. It filters samples using recorded in-air and mission-progress fields. It does not compare time/speed/heading because the AFAR source timestamps are not a validated PX4 schedule. The scores support only a held-out **SITL route-tracking** claim, not physical flight-dynamics calibration.

P15 adds per-quantity pilot gates for 1 Hz horizontal speed, vertical rate, course turn rate, course-over-ground heading, horizontal acceleration, and lag-1 speed autocorrelation. Distribution thresholds are the 95th percentile of pairwise within-source-group AFAR train-trace distances. A pass means the SITL feature is within variability seen among train traces in this archive; these thresholds are not universal aircraft standards. P15 reports mission duration descriptively because AFAR timing is not replayed. It explicitly leaves attitude, body angular rates, and three-axis acceleration unconfirmed because AFAR has no synchronized ground truth for them. P13 route errors remain `scored_threshold_pending` until project-level numerical tolerances are pre-registered.

### Calibration and independent AADM evaluation

The legacy Gürses raw-IQ and source-postprocessor fit branches are no longer called by `run_all.sh`. Their scripts and downloaded source files may remain in the workspace for archival/reference use, but they do not contribute to the Layer 1 artifact or default pipeline. AADM remains an optional separate evaluation:

```sh
python3 uav-cas/Scripts/0-step1_process_aadm.py --aadm_dir "AADM2025Dryad/USRP" --output_dir data/processed/aadm --no_plots
python3 src/pipeline/P6_evaluate_layer3_aadm.py --input_csv data/processed/aadm/aadm_testbed.csv --output_dir data/model_fits
```

The AADM processor writes to the requested output directory. Its report-log rate parsing lets P6 evaluate held-out AADM controller-reported data-mule rate. This does not calibrate C2/MAVLink delay/loss/goodput. The Dataset-32 slope is recorded as the Layer 1 reference but does not scale qdisc values; those profiles remain assumptions. Do not label Layer 3 calibrated without synchronized KPI from the target link and a held-out evaluation using compatible radios, traffic, and topology.

Default campaign: 3 routes × 4 qdisc profiles × benign/DoS × 5 repetitions = 120 SITL runs, run serially in real time. The 3.4 GHz option is a Dataset-32 reference-domain gate/metadata label; SITL does not simulate an RF carrier. Start with one pilot:

```sh
sudo -E python3 src/pipeline/P8_run_campaign.py --base_dir "$PWD" --output_dir "$PWD/data/pilot" --mission F1_afar_trace_1 --channel C1_LOS --traffic T0_Benign --frequency_ghz 3.4 --repetitions 1
```

For future absolute RF calibration, record one row/artifact set per TX/RX frequency and gain setting using `calibration_manifest_template.yaml`. Preserve raw instrument outputs, reference plane, device serials, RF path/attenuation, repeatability, uncertainty and instrument certificate. Dataset-32 fit parameters must not be converted to received dBm by assuming a link budget.

P7 fails closed if the SITL image, dependencies, route, model fit, mission telemetry or required artifacts are missing. Its Linux/Docker end-to-end run has not been executed from this Windows workspace, so integration is source-level only until the pilot command succeeds.

## Evidence still required before making stronger DT claims

1. Run the pilot and inspect PX4/Gazebo startup, MAVLink mission upload/ACK, actual telemetry progression, qdisc counters, PCAP decode and ULog. Confirm the AFAR route is physically executable in the selected Gazebo world.
2. Validate the flight trace and altitude transformation against source flight metadata; the 15 m baseline is a simulator safety mapping, not empirical AGL.
3. Obtain/verify the source path-loss processing and its uncertainty/reference-plane documentation before claiming traceable absolute Layer 1 calibration; the current fit uses the published processed `path_loss_db` labels as supplied.
4. Fit/validate delay/loss/goodput mappings from synchronized channel-quality and packet KPI measurements before calling Layer 3 calibrated.
5. Compare PX4/Gazebo outputs against paired physical flights, with predeclared trajectory, telemetry, network and uncertainty metrics, before claiming physical twin fidelity.
6. Implement packet-level MAVLink Replay/FDI/MITM if those attack labels are needed; current campaign supports only benign traffic and bounded-duration UDP DoS.

## Four-layer empirical-reference design

`P11_calibration_card.py` writes `data/model_fits/four_layer_calibration_card.json`. It distinguishes source-domain model fitting/holdout, recorded trajectory replay, external KPI references, and missing end-to-end C2 fidelity. It deliberately does not label L1/L3 as calibration of the active C2 simulator because fitted references do not drive P5 qdisc. Generated SITL/IDS metrics cannot establish physical equivalence.

| Layer | Current operational role | Dataset split / evidence | What can be claimed |
|---|---|---|---|
| 1. Propagation | Independent 3.4 GHz Dataset-32 and 3.6 GHz trend fits; no pooling | Flight-held-out for Dataset-32; scenario/altitude-group holdout for 3.6 GHz | Source-domain path-loss trend fit; not an active RF model in SITL |
| 2. Mobility | AFAR testbed GPS trace replay in PX4 SITL | Source-group partition; P13 evaluates a separately run holdout against route geometry | Simulated route-tracking score (cross-track and altitude); not real-flight dynamics validation |
| 3. Link KPIs | Separate AERPAW 5G, Pawprints 4G and AADM reference reports | 5G orientation holdout and single-flight Pawprints time-block check; AADM flight holdout | External radio KPI references. Target C2 qdisc remains stress-only |
| 4. End-to-end | SITL generates PCAP, telemetry, events and derived data views; optional P12 compares AADM development/testbed distributions | No independent target-C2 reference run; AADM comparison is external and unpaired | Data generation and domain comparison only; own C2 end-to-end fidelity is not validated |

`run_all.sh` builds the card after empirical processing (and in `LAYER1_ONLY` mode). Run `python3 src/pipeline/P11_calibration_card.py --base_dir .` to refresh it after manually processing a dataset. The card records expected missing evidence instead of upgrading claims based on file presence of generated campaigns.

Mobility holdout is assigned by the manifest’s pipeline-defined `source_group_id` (five top-level source IDs, three locations), not by an assumed campaign label. P8 defaults to F1/F4/F7 in three training groups and refuses split leakage. P14 fits scalar-speed Gauss-Markov parameters and reference distributions from train groups only; its fixed-bin JSD checks train-to-holdout distribution shift. P8/P7 couple seeded speed profiles to waypoint commands. Run a separate holdout campaign before P13; its route-geometry scores assess SITL tracking, not physical flight dynamics or swarm topology.

The default matrix is 3 missions × 4 fixed qdisc stress profiles × 2 traffic conditions (benign/UDP DoS) × 5 repetitions = 120 requested runs. `3.4 GHz` is the Dataset-32 reference-carrier gate and metadata only; `simulated_rf_carrier_ghz` is null. The four channel names are scenario labels for fixed stress profiles, not measured LOS/NLOS states.

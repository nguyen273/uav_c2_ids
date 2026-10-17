"""Assemble a conservative, machine-readable four-layer evidence/claim card."""
import argparse
import json
from pathlib import Path


def build(root: Path, output: Path):
    fit_dir = root / 'data' / 'model_fits'
    l1_34 = fit_dir / 'layer1_dataset32_pathloss_fit.json'
    l1_36 = fit_dir / 'layer1_a2g36_pathloss_fit.json'
    kpi5g = fit_dir / 'aerpaw_5g_kpi_holdout.json'
    pawprints4g = fit_dir / 'pawprints4g_rsrp_holdout.json'
    aadm_domain = fit_dir / 'aadm_development_testbed_domain_comparison.json'
    aadm = fit_dir / 'layer3_aadm_holdout_metrics.json'
    mobility_manifest = root / 'data' / 'missions' / 'missions_manifest.json'
    mobility_fit = fit_dir / 'layer2_afar_mobility_calibration.json'
    mobility_evaluation = fit_dir / 'layer2_mobility_holdout.json'
    kinematic_milestones = fit_dir / 'layer2_kinematic_milestones.json'
    mobility_fit_report = json.loads(mobility_fit.read_text(encoding='utf-8')) if mobility_fit.exists() else {}
    mobility_report = json.loads(mobility_evaluation.read_text(encoding='utf-8')) if mobility_evaluation.exists() else {}
    milestone_report = json.loads(kinematic_milestones.read_text(encoding='utf-8')) if kinematic_milestones.exists() else {}
    mobility_has_holdout = (mobility_report.get('required_partition') == 'holdout'
                            and bool(mobility_report.get('results')))
    mobility_is_fitted = (mobility_fit_report.get('calibration_status') ==
                          'afar_train_source_group_mobility_profile_with_heldout_distribution_evaluation')

    layers = {
        'layer1_propagation': {
            'status': 'pathloss_trend_fit_and_grouped_holdout_not_coupled' if (l1_34.exists() or l1_36.exists()) else 'not_run',
            'evidence': [str(p.relative_to(root)) for p in (l1_34, l1_36) if p.exists()],
            'domain': (['Dataset-32: 3.4 GHz, AERPAW Lake Wheeler, observed flights/range/altitude'] if l1_34.exists() else []) +
                      (['3.6 GHz CIR: separate scenario/altitude-group branch'] if l1_36.exists() else []),
            'claim_limit': 'Source-domain path-loss trend fit and grouped holdout only; the fit is not coupled into the SITL qdisc, and no absolute link budget or RF-to-C2 mapping is established.',
        },
        'layer2_mobility': {
            'status': ('train_group_profile_and_sitl_route_tracking_holdout_evaluated' if mobility_is_fitted and mobility_has_holdout else
                       'train_group_mobility_profile_fit_and_holdout_distribution_scored' if mobility_is_fitted else
                       'sitl_route_tracking_report_has_no_holdout_runs' if mobility_evaluation.exists() else
                       'recorded_trajectory_replay_library' if mobility_manifest.exists() else 'not_run'),
            'evidence': [str(p.relative_to(root)) for p in (mobility_manifest, mobility_fit, mobility_evaluation, kinematic_milestones) if p.exists()],
            'kinematic_milestone_statuses': {key: value.get('status') for key, value in milestone_report.get('milestones', {}).items()},
            'domain': ['AFAR recorded GPS trajectories; pipeline-defined source-directory holdout recorded in manifest'],
            'claim_limit': 'P14 fits an equal-trace-weighted scalar-speed Gauss-Markov process, turn-rate reference, and median altitude profile from AFAR train groups; P7 couples seeded waypoint speeds, turn-related speed caps, and altitude profile into PX4 mission commands. Holdout JSD scores AFAR feature-distribution shift, while P13 separately scores SITL route tracking. This does not establish real-aircraft dynamics, timing fidelity, or swarm topology.',
        },
        'layer3_link_kpis': {
            'status': 'external_radio_kpi_reference_only_c2_stress_assumptions' if (kpi5g.exists() or aadm.exists() or pawprints4g.exists()) else 'c2_stress_assumptions_only',
            'evidence': [str(p.relative_to(root)) for p in (kpi5g, aadm, pawprints4g) if p.exists()],
            'domain': (['Ericsson AERPAW 5G LTE/NR KPIs'] if kpi5g.exists() else []) +
                      (['AADM radio/data-mule KPIs; campaign-specific'] if aadm.exists() else []) +
                      (['Pawprints AERPAW LTE RSRP/RSRQ/RSSI and cell association'] if pawprints4g.exists() else []),
            'claim_limit': 'These datasets do not measure the target MAVLink C2 link; qdisc delay/loss/rate remain stress assumptions.',
        },
        'layer4_end_to_end_fidelity': {
            'status': 'simulation_artifacts_no_target_C2_fidelity_validation',
            'evidence': [str(aadm_domain.relative_to(root))] if aadm_domain.exists() else [],
            'domain': ( ["AADM development-DT vs testbed external distribution comparison; not this project twin"] if aadm_domain.exists() else []) +
                      ['PX4 SITL/Gazebo + MAVLink/Containernet generated runs, when executed'],
            'claim_limit': 'No synchronized target C2 measurement/reference twin pair; generated PCAP/telemetry and IDS scores alone do not establish fidelity.',
        },
    }
    fitted = sum(layer['status'] == 'pathloss_trend_fit_and_grouped_holdout_not_coupled' for layer in layers.values())
    card = {
        'schema_version': 1,
        'overall_status': 'simulation_only_c2_emulation_with_external_empirical_references',
        'overall_claim': 'PX4/MAVLink simulation/emulation pipeline with domain-specific empirical reference fits, recorded trajectory replay, and declared network stress profiles; C2 impairment calibration and independent end-to-end fidelity validation are not established.',
        'layers_with_pathloss_fit_and_grouped_holdout': fitted,
        'layers': layers,
        'claim_gates': {
            'may_claim_domain_specific_pathloss_fit_and_validation': fitted >= 1,
            'may_claim_target_c2_link_calibrated': False,
            'may_claim_end_to_end_c2_calibrated': False,
            'may_claim_physical_uav_equivalence': False,
        },
        'required_next_evidence': [
            'Run P8 on the manifest holdout source group, P13 for path/altitude errors, and P15 for per-quantity kinematic pilot gates; the P15 limits are within-source-group archive thresholds, not universal flight standards.',
            'Obtain compatible synchronized C2 reference KPIs (radio configuration, MAVLink PCAP, loss, RTT/jitter, goodput) before fitting target-C2 qdisc mappings.',
            'Validate end-to-end simulator outputs against independent compatible C2 references on predeclared trajectory, network, and telemetry metrics.',
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(card, indent=2), encoding='utf-8')
    return card


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base_dir', required=True)
    parser.add_argument('--output', default='data/model_fits/four_layer_calibration_card.json')
    args = parser.parse_args()
    root = Path(args.base_dir).resolve()
    destination = Path(args.output)
    if not destination.is_absolute():
        destination = root / destination
    result = build(root, destination)
    print(json.dumps({'output': str(destination), 'overall_status': result['overall_status'],
                      'layer_statuses': {k: v['status'] for k, v in result['layers'].items()}}, indent=2))


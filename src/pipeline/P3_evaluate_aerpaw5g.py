"""Grouped descriptive checks for the Dryad LTE/NR RSRP/SINR/throughput data."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _scores(y, p, unit):
    e=np.asarray(p)-np.asarray(y)
    return {"n":int(len(e)),"rmse":float(np.sqrt(np.mean(e**2))),"mae":float(np.mean(np.abs(e))),
            "bias_pred_minus_observed":float(np.mean(e)),"unit":unit}


def evaluate(input_csv: Path, output_dir: Path) -> Path:
    df=pd.read_csv(input_csv)
    needed={"orientation","radio_access_technology","rsrp_value_dbm","sinr_db","throughput_mbps","horizontal_distance_to_lw1_m"}
    if needed-set(df): raise ValueError(f"Normalized 5G input missing {sorted(needed-set(df))}")
    for c in needed-{"orientation","radio_access_technology"}: df[c]=pd.to_numeric(df[c],errors="coerce")
    df=df.replace([np.inf,-np.inf],np.nan)
    report={"evaluation_type":"AERPAW_Dryad_5G_external_empirical_KPI_check",
            "source":"Dryad DOI 10.5061/dryad.wh70rxx06",
            "role":"technology-specific LTE/NR network KPI evidence; not target C2 calibration, not Dataset-32/3.6 GHz propagation fit",
            "grouping":"leave-one-yaw-orientation-out; only two orientations from one campaign, so exploratory and weak external generalization evidence",
            "dependence_note":"Rows are temporally sampled within two flights and are not independent replicates; metrics are descriptive and no row-level confidence interval is implied.",
            "results":{}}
    for rat in ("LTE","NR"):
        ratdf=df[df.radio_access_technology==rat]
        if ratdf.empty: continue
        entry={"observations":int(len(ratdf)),"orientation_summary":{}}
        for orientation,part in ratdf.groupby("orientation"):
            entry["orientation_summary"][str(orientation)]={
                "n":int(len(part)),"rsrp_dbm_median":float(part.rsrp_value_dbm.median()),
                "sinr_db_median":float(part.sinr_db.median()),
                "throughput_mbps_median":float(part.throughput_mbps.median()) if rat=="NR" and part.throughput_mbps.notna().any() else None,
                "distance_horizontal_m_range":[float(part.horizontal_distance_to_lw1_m.min()),float(part.horizontal_distance_to_lw1_m.max())]}
        folds=[]
        for held in sorted(ratdf.orientation.dropna().unique()):
            train=ratdf[ratdf.orientation!=held]; test=ratdf[ratdf.orientation==held]
            tr=train.dropna(subset=["rsrp_value_dbm","horizontal_distance_to_lw1_m"])
            te=test.dropna(subset=["rsrp_value_dbm","horizontal_distance_to_lw1_m"])
            item={"heldout_orientation":str(held),"training_orientations":sorted(train.orientation.unique().tolist())}
            if len(tr)>=10 and len(te)>=10 and tr.horizontal_distance_to_lw1_m.nunique()>3:
                x=np.log10(np.maximum(tr.horizontal_distance_to_lw1_m.to_numpy(float),1.0))
                slope,intercept=np.polyfit(x,tr.rsrp_value_dbm.to_numpy(float),1)
                xt=np.log10(np.maximum(te.horizontal_distance_to_lw1_m.to_numpy(float),1.0))
                # Remove held-out orientation offset; reports trend only, not absolute RSRP transfer.
                actual=te.rsrp_value_dbm.to_numpy(float); pred=slope*(xt-xt.mean())+actual.mean()
                item.update({"rsrp_distance_slope_db_per_decade":float(slope),
                             "heldout_rsrp_trend_metrics":_scores(actual-actual.mean(),pred-actual.mean(),"dB; demeaned trend")})
            if rat=="NR":
                tr2=train.dropna(subset=["sinr_db","throughput_mbps"]); te2=test.dropna(subset=["sinr_db","throughput_mbps"])
                if len(tr2)>=10 and len(te2)>=10 and tr2.sinr_db.nunique()>2:
                    slope,intercept=np.polyfit(tr2.sinr_db.to_numpy(float),tr2.throughput_mbps.to_numpy(float),1)
                    pred=np.maximum(0,slope*te2.sinr_db.to_numpy(float)+intercept)
                    item["heldout_throughput_from_sinr_metrics"]=_scores(te2.throughput_mbps.to_numpy(float),pred,"Mbps")
                    item["throughput_model"]={"type":"linear fit on training orientation", "slope_mbps_per_db":float(slope),"intercept_mbps":float(intercept)}
            folds.append(item)
        entry["orientation_holdout_folds"]=folds
        entry["interpretation"]="Orientation holdout is a same-campaign check with one training orientation; report per fold and do not call it broad external validation. RSRP trend score is demeaned and does not validate absolute propagation intercept."
        report["results"][rat]=entry
    out=output_dir.resolve(); out.mkdir(parents=True,exist_ok=True); target=out/"aerpaw_5g_kpi_holdout.json"
    target.write_text(json.dumps(report,indent=2),encoding="utf-8"); return target


if __name__=="__main__":
    ap=argparse.ArgumentParser(description=__doc__); ap.add_argument("--input_csv",required=True); ap.add_argument("--output_dir",required=True)
    a=ap.parse_args(); print(evaluate(Path(a.input_csv),Path(a.output_dir)))

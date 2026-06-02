"""
export.py
Handles saving session data to Excel (unified workbook) and JSON (progress save).
Mirrors the MATLAB localSave function.
"""

import json, os
import numpy as np
import pandas as pd
from datetime import datetime
from .session_state import SessionState, SAMPLE_RATE


def _nan_safe(v):
    """Convert numpy/float NaN to None for JSON."""
    if v is None:
        return None
    if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
        return None
    if isinstance(v, (np.floating, np.integer)):
        return float(v)
    return v


def save_json(state: SessionState, out_dir: str) -> str:
    """Save progress as JSON (re-loadable)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{state.patient_id}_{state.session}_{ts}.json"
    path = os.path.join(out_dir, fname)

    payload = {
        "patient_id": state.patient_id,
        "session": state.session,
        "raw_path": state.raw_path,
        "results": {},
        "log": state.log_lines,
    }

    for analysis in ("CA", "CVR"):
        payload["results"][analysis] = {}
        for vessel in ("MCA", "PCA"):
            r = state.results[analysis][vessel]
            if r is not None:
                payload["results"][analysis][vessel] = _serialise_dict(r)

    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    return fname


def save_excel(state: SessionState, out_dir: str) -> str:
    """
    Save unified Excel workbook with sheets:
      Master          – full raw data reflecting any NaN edits
      MCA_CA / PCA_CA – CA results table
      MCA_CVR_Base / MCA_CVR_Hyp / MCA_CVR_Summary (same for PCA)
      Marks           – mark labels + times
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{state.patient_id}_{state.session}_{ts}_Unified.xlsx"
    path = os.path.join(out_dir, fname)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:

        # ── Master sheet ──
        if state.raw_headers and state.raw_rows:
            master_df = pd.DataFrame(state.raw_rows, columns=state.raw_headers)
            # Reflect edits in the modified columns. Use the name-resolved
            # positions captured at load time so this works regardless of the
            # source file's column order.
            col_positions = getattr(state, "column_positions", {}) or {}
            sig_to_arr = {
                "mean_u": state.mean_u,
                "env_u":  state.env_u,
                "etco2":  state.etco2,
                "co2":    state.co2,
                "abp":    state.abp,
            }
            for signal, arr in sig_to_arr.items():
                col_idx = col_positions.get(signal)
                if (col_idx is not None and col_idx < len(state.raw_headers)
                        and arr is not None and len(arr) == len(master_df)):
                    master_df[state.raw_headers[col_idx]] = arr
            master_df.to_excel(writer, sheet_name="Master", index=False)

        # ── CA sheets ──
        for vessel in ("MCA", "PCA"):
            ca = state.results["CA"].get(vessel)
            if ca and "table" in ca:
                df = pd.DataFrame(ca["table"])
                df.to_excel(writer, sheet_name=f"{vessel}_CA", index=False)

        # ── CVR sheets ──
        for vessel in ("MCA", "PCA"):
            cvr = state.results["CVR"].get(vessel)
            if cvr:
                if "baseline" in cvr:
                    pd.DataFrame(cvr["baseline"]).to_excel(writer, sheet_name=f"{vessel}_CVR_Base", index=False)
                if "hypercapnia" in cvr:
                    pd.DataFrame(cvr["hypercapnia"]).to_excel(writer, sheet_name=f"{vessel}_CVR_Hyp", index=False)
                if "summary" in cvr:
                    pd.DataFrame([cvr["summary"]]).to_excel(writer, sheet_name=f"{vessel}_CVR_Summary", index=False)

        # ── Marks sheet ──
        if state.marks_labels:
            marks_df = pd.DataFrame({
                "Time_s": state.marks_times,
                "Label": state.marks_labels,
            })
            marks_df.to_excel(writer, sheet_name="Marks", index=False)

    return fname


def _serialise_dict(d):
    """Recursively convert numpy types for JSON."""
    if isinstance(d, dict):
        return {k: _serialise_dict(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_serialise_dict(x) for x in d]
    if isinstance(d, np.ndarray):
        return d.tolist()
    if isinstance(d, (np.floating, np.integer)):
        return float(d)
    return d

"""
export.py
Handles saving session data to Excel (unified workbook) and JSON (progress save).
Mirrors the MATLAB localSave function.
"""

import json, os, re
import numpy as np
import pandas as pd
from datetime import datetime
from .session_state import SessionState, SAMPLE_RATE
from . import pi_analysis

# Sheet PI.m reads from and writes back to.
PI_SHEET = "PI Demographics"
PI_LEAD_COLUMNS = ("PatientID", "SessionSpeed", "Vessel")


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

    # PI epochs: metrics and time bounds only. The per-sample waveform stays
    # out of the progress file — it is recoverable from the raw recording, and
    # a few hundred epochs of it would dwarf everything else here.
    if state.pi_epochs:
        payload["results"]["PI"] = {
            "speed": state.pi_speed,
            "vessel": state.pi_vessel,
            "summary": pi_analysis.summarize(state.pi_epochs),
            "epochs": [
                _serialise_dict({k: v for k, v in e.items() if k not in ("time", "amp")})
                for e in pi_analysis.numbered(state.pi_epochs)
            ],
        }

    # allow_nan=False is a tripwire: the payload is already NaN-scrubbed by
    # _serialise_dict, so this should never fire — but if a NaN ever slips
    # through we want a loud failure (caught by the save route) rather than a
    # silently invalid file that browsers and other tools refuse to parse.
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str, allow_nan=False)
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


def _safe_stem(s: str) -> str:
    return re.sub(r"[^\w.\-]", "_", (s or "").strip()) or "PI"


def save_pi_excel(row: dict, out_dir: str,
                  prior_path: str | None = None,
                  prior_filename: str | None = None,
                  fallback_stem: str = "PI") -> str:
    """
    Append one PI row to the "PI Demographics" sheet and write a new workbook.

    PI.m opens an existing spreadsheet, adds the row, pads both sides with NaN
    so old and new column sets line up, and saves under a timestamped name —
    never overwriting the source. Same contract here, with two differences:
    the prior workbook is optional (with none, the sheet is created fresh), and
    any *other* sheets in that workbook are carried across rather than dropped,
    which is what MATLAB's writetable does to them.
    """
    ts = datetime.now().strftime("%d%b%Y_%H%M%S")
    other_sheets: dict[str, pd.DataFrame] = {}

    if prior_path:
        book = pd.read_excel(prior_path, sheet_name=None)
        if PI_SHEET not in book:
            raise ValueError(
                f"Prior workbook has no '{PI_SHEET}' sheet "
                f"(found: {', '.join(book) or 'no sheets'})."
            )
        old = book.pop(PI_SHEET)
        other_sheets = book
        stem = os.path.splitext(prior_filename or os.path.basename(prior_path))[0]
        if "---" in stem:
            stem = stem.split("---")[0]
    else:
        old = pd.DataFrame()
        stem = fallback_stem

    new_row = pd.DataFrame([row])
    # Concatenating onto an empty frame warns and can coerce dtypes in pandas
    # 2.x, so short-circuit the first-row case.
    combined = new_row if old.empty else pd.concat([old, new_row], ignore_index=True)

    # PI.m orders columns as the three IDs, then MATLAB's setdiff() output —
    # which is sorted. Mirror that so appended rows always align.
    lead = [c for c in PI_LEAD_COLUMNS if c in combined.columns]
    rest = sorted(c for c in combined.columns if c not in lead)
    combined = combined[lead + rest]

    fname = f"{_safe_stem(stem)}---{ts}.xlsx"
    path = os.path.join(out_dir, fname)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        combined.to_excel(writer, sheet_name=PI_SHEET, index=False)
        for name, df in other_sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
    return fname


def _serialise_dict(d):
    """Recursively convert numpy types for JSON and replace NaN/Inf with None.

    NaN is expected in the results (e.g. correlation values over cleaned
    windows); leaving it in produces the literal token ``NaN``, which is not
    valid JSON, so the saved progress file fails to reopen in any strict parser.
    """
    if isinstance(d, dict):
        return {k: _serialise_dict(v) for k, v in d.items()}
    if isinstance(d, (list, tuple)):
        return [_serialise_dict(x) for x in d]
    if isinstance(d, np.ndarray):
        return _serialise_dict(d.tolist())
    if isinstance(d, (np.floating, np.integer)):
        d = float(d)
    if isinstance(d, float):
        return None if (np.isnan(d) or np.isinf(d)) else d
    return d

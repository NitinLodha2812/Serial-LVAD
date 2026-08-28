"""
export.py
Handles saving session data to Excel (unified workbook) and JSON (progress save).
Mirrors the MATLAB localSave function.
"""

import json, os, re, shutil, tempfile, zipfile
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


PROGRESS_FORMAT = "lvad_progress"
PROGRESS_VERSION = 2


def build_progress_payload(state: SessionState, sessions: list) -> dict:
    """
    The reloadable progress snapshot: study mode + identity + every saved
    CA/CVR session (results and selection ranges) + the current PI epochs.

    `sessions` is the list of per-label session payloads from cacvr_sessions
    (each a dict with "label", "results", "selections"). Kept as a list so
    RAMPs speeds and Serial LVAD vessels both round-trip cleanly.
    """
    payload = {
        "format": PROGRESS_FORMAT,
        "version": PROGRESS_VERSION,
        "study_mode": state.study_mode,
        "patient_id": state.patient_id,
        "session": state.session,
        "base_name": state.base_name,
        "raw_path": state.raw_path,
        "cacvr_sessions": [
            {
                "label": s.get("label") or "",
                "results": _serialise_dict(s.get("results", {})),
                "selections": _serialise_dict(s.get("selections", {})),
            }
            for s in sessions
        ],
        "log": state.log_lines,
    }
    # PI metrics only (per-sample waveform stays out — recoverable from the raw
    # recording, and would dwarf everything else here).
    if state.pi_epochs:
        payload["pi"] = {
            "speed": state.pi_speed,
            "vessel": state.pi_vessel,
            "summary": pi_analysis.summarize(state.pi_epochs),
            "epochs": [
                _serialise_dict({k: v for k, v in e.items() if k not in ("time", "amp")})
                for e in pi_analysis.numbered(state.pi_epochs)
            ],
        }
    return payload


def save_progress_json(state: SessionState, sessions: list, out_dir: str) -> str:
    """Write the standalone progress JSON (the file 'Load Progress' reopens)."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{_safe_stem(state.patient_id)}_{_safe_stem(state.session)}_{ts}_progress.json"
    path = os.path.join(out_dir, fname)
    payload = build_progress_payload(state, sessions)
    # allow_nan=False is a tripwire: the payload is already NaN-scrubbed, so
    # this should never fire — but a stray NaN must fail loudly rather than
    # write a file no strict parser can reopen.
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str, allow_nan=False)
    return fname


def parse_progress_payload(data: dict) -> dict:
    """
    Normalise any progress-JSON schema we have written into:
        {study_mode, patient_id, session, base_name, sessions, pi}
    where `sessions` is [{label, results, selections}].

    Tolerates three shapes: the current list form, an older
    ``cacvr_sessions: {label: results}`` dict, and the original single-session
    ``results: {CA/CVR: {vessel: ...}}`` snapshot.
    """
    patient_id = data.get("patient_id", "") or ""
    session = data.get("session", "") or ""
    study_mode = data.get("study_mode") or "serial_lvad"
    base_name = data.get("base_name") or ""
    if not base_name:
        raw = data.get("raw_path") or ""
        stem = os.path.splitext(os.path.basename(raw))[0] if raw else ""
        base_name = stem or f"{patient_id}_{session}".strip("_") or "reopened"

    sessions = []
    cs = data.get("cacvr_sessions")
    if isinstance(cs, list):
        for s in cs:
            sessions.append({
                "label": s.get("label", ""),
                "results": s.get("results", {}) or {},
                "selections": s.get("selections", {}) or {},
            })
    elif isinstance(cs, dict):
        for label, results in cs.items():
            sessions.append({
                "label": "" if label == "nolabel" else label,
                "results": results or {},
                "selections": {},
            })
    elif isinstance(data.get("results"), dict):
        # Original single-session snapshot. PI (if present) is pulled out
        # separately below, so exclude it from the CA/CVR results here.
        res = {k: v for k, v in data["results"].items() if k in ("CA", "CVR")}
        if any((res.get(a) or {}).get(v) for a in ("CA", "CVR") for v in ("MCA", "PCA")):
            sessions.append({"label": session, "results": res, "selections": {}})

    pi = data.get("pi")
    if pi is None and isinstance(data.get("results"), dict):
        pi = data["results"].get("PI")

    return {
        "study_mode": study_mode,
        "patient_id": patient_id,
        "session": session,
        "base_name": base_name,
        "sessions": sessions,
        "pi": pi,
    }


def _master_dataframe(state: SessionState) -> pd.DataFrame:
    """Full raw table with the edited (NaN-brushed) columns written back.

    Edits live on the shared per-signal arrays, which every tab reads and
    writes, so a single master reflects deletions made on any plot — there is
    no need for a per-tab or per-session master copy.
    """
    master_df = pd.DataFrame(state.raw_rows, columns=state.raw_headers)
    col_positions = getattr(state, "column_positions", {}) or {}
    sig_to_arr = {
        "mean_u": state.mean_u, "env_u": state.env_u, "etco2": state.etco2,
        "co2": state.co2, "abp": state.abp,
    }
    for signal, arr in sig_to_arr.items():
        col_idx = col_positions.get(signal)
        if (col_idx is not None and col_idx < len(state.raw_headers)
                and arr is not None and len(arr) == len(master_df)):
            master_df[state.raw_headers[col_idx]] = arr
    return master_df


def _write_cacvr_sheets(writer, results: dict) -> int:
    """Write the CA/CVR result sheets for one session; return how many written."""
    n = 0
    ca_all = results.get("CA", {}) or {}
    cvr_all = results.get("CVR", {}) or {}
    for vessel in ("MCA", "PCA"):
        ca = ca_all.get(vessel)
        if ca and "table" in ca:
            pd.DataFrame(ca["table"]).to_excel(writer, sheet_name=f"{vessel}_CA", index=False)
            n += 1
    for vessel in ("MCA", "PCA"):
        cvr = cvr_all.get(vessel)
        if not cvr:
            continue
        if "baseline" in cvr:
            pd.DataFrame(cvr["baseline"]).to_excel(writer, sheet_name=f"{vessel}_CVR_Base", index=False); n += 1
        if "hypercapnia" in cvr:
            pd.DataFrame(cvr["hypercapnia"]).to_excel(writer, sheet_name=f"{vessel}_CVR_Hyp", index=False); n += 1
        if "summary" in cvr:
            pd.DataFrame([cvr["summary"]]).to_excel(writer, sheet_name=f"{vessel}_CVR_Summary", index=False); n += 1
    return n


def _write_marks_sheet(writer, state: SessionState):
    if state.marks_labels:
        pd.DataFrame({
            "Time_s": state.marks_times,
            "Label": state.marks_labels,
        }).to_excel(writer, sheet_name="Marks", index=False)


def save_excel(state: SessionState, out_dir: str) -> str:
    """
    Legacy single-shot unified workbook (Master + CA + CVR + Marks) for the
    current working results. Kept for the Main-tab Save button; the accumulate-
    then-export-all workflow lives in export_all_cacvr.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"{state.patient_id}_{state.session}_{ts}_Unified.xlsx"
    path = os.path.join(out_dir, fname)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        if state.raw_headers and state.raw_rows:
            _master_dataframe(state).to_excel(writer, sheet_name="Master", index=False)
        _write_cacvr_sheets(writer, state.results)
        _write_marks_sheet(writer, state)
    return fname


def save_master_excel(state: SessionState, path: str):
    """Master + Marks only — the study-wide raw data, written once."""
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        _master_dataframe(state).to_excel(writer, sheet_name="Master", index=False)
        _write_marks_sheet(writer, state)


def save_session_excel(results: dict, path: str) -> int:
    """One session's CA + CVR sheets. Returns the sheet count (0 = nothing)."""
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        n = _write_cacvr_sheets(writer, results)
        if n == 0:
            # openpyxl refuses to save a book with no visible sheet — give the
            # (shouldn't-happen) empty session a placeholder so it still opens.
            pd.DataFrame({"note": ["no CA/CVR results in this session"]}).to_excel(
                writer, sheet_name="empty", index=False)
    return n


def export_all_cacvr(state: SessionState, sessions: list, out_dir: str):
    """
    Bundle the whole study into one download:
      <patient>_<ts>_Master.xlsx   — raw data + edits, ONCE
      <patient>_<label>.xlsx       — CA+CVR sheets, one per saved session/speed
      <patient>_<ts>_progress.json — reloadable snapshot of everything (redo net)
    all zipped together. PI is exported separately by design.

    `sessions` is the list of saved session payloads (from cacvr_sessions),
    each a dict with "label" and serialised "results".
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    patient = _safe_stem(state.patient_id) or "patient"
    base = f"{patient}_{ts}"
    workdir = tempfile.mkdtemp()
    manifest = []
    try:
        # The master needs the raw recording. After a JSON reopen there is no
        # raw data, so skip it rather than write an empty sheet.
        if state.raw_headers and state.raw_rows:
            master_name = f"{base}_Master.xlsx"
            save_master_excel(state, os.path.join(workdir, master_name))
            manifest.append(master_name)

        used = set()
        for s in sessions:
            label = _safe_stem(s.get("label")) or "session"
            sname = f"{patient}_{label}.xlsx"
            # Guard against two labels colliding after sanitisation.
            i = 2
            while sname in used:
                sname = f"{patient}_{label}_{i}.xlsx"; i += 1
            used.add(sname)
            save_session_excel(s.get("results", {}), os.path.join(workdir, sname))
            manifest.append(sname)

        json_name = f"{base}_progress.json"
        _write_progress_json(state, sessions, os.path.join(workdir, json_name))
        manifest.append(json_name)

        zip_name = f"{base}_export.zip"
        with zipfile.ZipFile(os.path.join(out_dir, zip_name), "w", zipfile.ZIP_DEFLATED) as z:
            for name in manifest:
                z.write(os.path.join(workdir, name), arcname=name)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return zip_name, manifest


def _write_progress_json(state: SessionState, sessions: list, path: str):
    """The redo safety-net bundled inside the study zip — same schema as the
    standalone progress file, so either one can be reopened."""
    with open(path, "w") as f:
        json.dump(build_progress_payload(state, sessions), f,
                  indent=2, default=str, allow_nan=False)


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


def serialise(d):
    """Public: JSON-safe deep copy (numpy → python, NaN/Inf → None)."""
    return _serialise_dict(d)


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

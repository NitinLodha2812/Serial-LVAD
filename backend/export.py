"""
export.py
Handles saving session data to Excel (unified workbook) and JSON (progress save).
Mirrors the MATLAB localSave function.
"""

import json, os, re, shutil, tempfile, zipfile
import numpy as np
import pandas as pd
from datetime import datetime
from .session_state import SessionState, SAMPLE_RATE, MODE_RAMPS, MODE_SERIAL
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

    base_name = data.get("base_name") or ""
    if not base_name:
        # raw_path may be a Windows path from another machine; split on both
        # separators so we recover just the file stem (which is the base_name a
        # fresh load of the same recording would produce).
        raw = (data.get("raw_path") or "").replace("\\", "/")
        stem = os.path.splitext(raw.rsplit("/", 1)[-1])[0] if raw else ""
        base_name = stem or f"{patient_id}_{session}".strip("_") or "reopened"

    # Legacy files (before study modes) carry no study_mode — infer it. RAMPs
    # patient ids look like R###, and RAMPs sessions/labels say "Speed".
    study_mode = data.get("study_mode")
    if study_mode not in ("ramps", "serial_lvad"):
        cs = data.get("cacvr_sessions")
        labels = ([s.get("label", "") for s in cs] if isinstance(cs, list)
                  else list(cs.keys()) if isinstance(cs, dict) else [])
        hay = " ".join([patient_id, session, base_name, *labels]).lower()
        is_r = re.match(r"\s*r\d", patient_id.strip(), re.IGNORECASE) is not None
        study_mode = "ramps" if (is_r or "speed" in hay) else "serial_lvad"

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


# ── per-analysis frames for the study export (one tab each) ──────────────

_CVR_SUMMARY_ORDER = [
    ("base_mcbf", "Baseline MCBF"), ("base_wcbf", "Baseline WCBF"), ("base_co2", "Baseline CO2"),
    ("hyp_mcbf", "Hypercapnia MCBF"), ("hyp_wcbf", "Hypercapnia WCBF"), ("hyp_co2", "Hypercapnia CO2"),
    ("delta_mcbf", "Delta MCBF"), ("delta_wcbf", "Delta WCBF"), ("delta_co2", "Delta CO2"),
    ("mcvr", "MCVR"), ("wcvr", "WCVR"),
    ("base_co2_time", "Baseline CO2 time (s)"), ("hyp_co2_time", "Hypercapnia CO2 time (s)"),
]


def _ca_frame(ca: dict):
    """CA tab = the per-window CA table (Time/TCD/ABP/MAP/MFV/Corr/MX/MeanMFV)."""
    if ca and ca.get("table"):
        return pd.DataFrame(ca["table"])
    return None


def _cvr_frame(cvr: dict):
    """CVR tab = one compact key/value table of the computed values."""
    if not cvr:
        return None
    s = cvr.get("summary") if isinstance(cvr, dict) and "summary" in cvr else cvr
    s = s or {}
    rows = [{"Metric": label, "Value": s.get(key)}
            for key, label in _CVR_SUMMARY_ORDER if s.get(key) is not None]
    return pd.DataFrame(rows, columns=["Metric", "Value"]) if rows else None


def pi_epochs_frame(epochs: list):
    """PI tab = one row per beat, columns for the metrics (as in the PI table)."""
    if not epochs:
        return None
    rows = []
    for e in pi_analysis.numbered(epochs):
        native = e["type"] == pi_analysis.NATIVE
        rows.append({
            "Beat": ("Native #" if native else "Artificial #") + str(e["ordinal"]),
            "Type": "Native" if native else "Artificial",
            "Start_s": e.get("t_start"), "End_s": e.get("t_end"),
            "Max": e.get("max"), "Min": e.get("min"), "Mean": e.get("mean"),
            "PW_s": e.get("pw"), "PI": e.get("pi"),
        })
    cols = ["Beat", "Type", "Start_s", "End_s", "Max", "Min", "Mean", "PW_s", "PI"]
    return pd.DataFrame(rows, columns=cols)


def _workbook_from_frames(path: str, frames: list) -> int:
    """Write (sheet_name, dataframe) pairs; skip None frames. Returns tab count."""
    frames = [(n, df) for (n, df) in frames if df is not None and not df.empty]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, df in frames:
            df.to_excel(writer, sheet_name=name[:31], index=False)
        if not frames:
            pd.DataFrame({"note": ["no results in this workbook"]}).to_excel(
                writer, sheet_name="empty", index=False)
    return len(frames)


def _merge_cacvr_by_vessel(cacvr_sessions: list) -> dict:
    """Collapse all tags into per-vessel CA/CVR (first non-null wins). Robust to
    the tag being the vessel (clean data) or messy legacy labels."""
    out = {"MCA": {"CA": None, "CVR": None}, "PCA": {"CA": None, "CVR": None}}
    for s in cacvr_sessions:
        r = s.get("results", {}) or {}
        for v in ("MCA", "PCA"):
            if out[v]["CA"] is None and (r.get("CA") or {}).get(v):
                out[v]["CA"] = r["CA"][v]
            if out[v]["CVR"] is None and (r.get("CVR") or {}).get(v):
                out[v]["CVR"] = r["CVR"][v]
    return out


def _pi_epochs_by_vessel(pi_sessions: list) -> dict:
    out = {"MCA": [], "PCA": []}
    for p in pi_sessions:
        v = (p.get("vessel") or "").strip().upper()
        if v in out:
            out[v].extend(p.get("epochs", []) or [])
    return out


def _pi_epochs_for_speed(pi_sessions: list, speed: str) -> list:
    target = (speed or "").strip().lower()
    epochs = []
    for p in pi_sessions:
        if (p.get("speed") or "").strip().lower() == target:
            epochs.extend(p.get("epochs", []) or [])
    return epochs


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


def export_all_cacvr(state: SessionState, cacvr_sessions: list, pi_sessions: list, out_dir: str):
    """
    Bundle the whole study into one zip. Workbook layout is mode-specific:

      Serial LVAD — ONE workbook per session (the recording), up to 6 tabs:
                    MCA_CA, MCA_CVR, MCA_PI, PCA_CA, PCA_CVR, PCA_PI
                    (fewer if a vessel/analysis wasn't collected).
      RAMPs       — ONE workbook per speed, up to 3 tabs: CA, CVR, PI.

    Plus a `*_Master.xlsx` (raw data + edits, written once, skipped after a
    JSON reopen with no raw data) and a `*_progress.json`. The standalone
    "PI Demographics" workbook is separate and unaffected.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    patient = _safe_stem(state.patient_id) or "patient"
    base = f"{patient}_{ts}"
    workdir = tempfile.mkdtemp()
    manifest = []
    try:
        if state.raw_headers and state.raw_rows:
            master_name = f"{base}_Master.xlsx"
            save_master_excel(state, os.path.join(workdir, master_name))
            manifest.append(master_name)

        used = set()

        def unique(name):
            out, i = name, 2
            while out in used:
                out = name[:-5] + f"_{i}.xlsx"; i += 1
            used.add(out)
            return out

        if state.study_mode == MODE_RAMPS:
            # One workbook per speed (tag), tabs CA / CVR / PI (MCA-only study).
            for s in cacvr_sessions:
                label = s.get("label") or ""
                r = s.get("results", {}) or {}
                frames = [
                    ("CA",  _ca_frame((r.get("CA") or {}).get("MCA"))),
                    ("CVR", _cvr_frame((r.get("CVR") or {}).get("MCA"))),
                    ("PI",  pi_epochs_frame(_pi_epochs_for_speed(pi_sessions, label))),
                ]
                wbname = unique(f"{patient}_{_safe_stem(label) or 'speed'}.xlsx")
                _workbook_from_frames(os.path.join(workdir, wbname), frames)
                manifest.append(wbname)
        else:
            # Serial LVAD: one workbook for the session, MCA/PCA × CA/CVR/PI.
            vessels = _merge_cacvr_by_vessel(cacvr_sessions)
            pis = _pi_epochs_by_vessel(pi_sessions)
            frames = []
            for v in ("MCA", "PCA"):
                frames.append((f"{v}_CA",  _ca_frame(vessels[v]["CA"])))
                frames.append((f"{v}_CVR", _cvr_frame(vessels[v]["CVR"])))
                frames.append((f"{v}_PI",  pi_epochs_frame(pis[v])))
            sess = _safe_stem(state.session) or "session"
            wbname = unique(f"{patient}_{sess}.xlsx")
            _workbook_from_frames(os.path.join(workdir, wbname), frames)
            manifest.append(wbname)

        json_name = f"{base}_progress.json"
        _write_progress_json(state, cacvr_sessions, os.path.join(workdir, json_name))
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

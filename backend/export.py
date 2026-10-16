"""
export.py
Handles saving session data to Excel (unified workbook) and JSON (progress save).
Mirrors the MATLAB localSave function.
"""

import json, os, re, shutil, tempfile, zipfile
import numpy as np
import pandas as pd
from datetime import datetime
from .session_state import (
    SessionState, SAMPLE_RATE, MODE_RAMPS, MODE_SERIAL, GUI_VERSION,
)
from . import pi_analysis


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
PROGRESS_VERSION = 3   # v3: PI folded into each tag (pi_epochs with waveforms)


def build_progress_payload(state: SessionState, sessions: list) -> dict:
    """
    The reloadable progress snapshot: GUI version + study mode + identity +
    every saved tag (CA/CVR results, selection ranges, and the PI beats that
    ride on that tag). The PI beats keep their per-sample waveform so the PI
    window can be redrawn on reopen. `sessions` is the list of per-label
    session payloads from cacvr_sessions; kept as a list so RAMPs speeds and
    Serial LVAD vessels both round-trip cleanly.
    """
    return {
        "format": PROGRESS_FORMAT,
        "version": PROGRESS_VERSION,
        "gui_version": GUI_VERSION,
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
                "pi_epochs": _serialise_dict(s.get("pi_epochs", []) or []),
                "pi_abp_shift": s.get("pi_abp_shift", 0.0) or 0.0,
            }
            for s in sessions
        ],
        "log": state.log_lines,
    }


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
                "pi_epochs": s.get("pi_epochs", []) or [],
                "pi_abp_shift": s.get("pi_abp_shift", 0.0) or 0.0,
            })
    elif isinstance(cs, dict):
        for label, results in cs.items():
            sessions.append({
                "label": "" if label == "nolabel" else label,
                "results": results or {},
                "selections": {}, "pi_epochs": [], "pi_abp_shift": 0.0,
            })
    elif isinstance(data.get("results"), dict):
        res = {k: v for k, v in data["results"].items() if k in ("CA", "CVR")}
        if any((res.get(a) or {}).get(v) for a in ("CA", "CVR") for v in ("MCA", "PCA")):
            sessions.append({"label": session, "results": res, "selections": {},
                             "pi_epochs": [], "pi_abp_shift": 0.0})

    return {
        "gui_version": data.get("gui_version"),
        "study_mode": study_mode,
        "patient_id": patient_id,
        "session": session,
        "base_name": base_name,
        "sessions": sessions,
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


def _numeric_id(text) -> "int | None":
    """The numeric portion of a string (VAD051 -> 51, R125 -> 125, 'Speed 4' -> 4,
    '9600' -> 9600). None if there are no digits."""
    digits = re.sub(r"\D", "", str(text or ""))
    return int(digits) if digits else None


def _ca_frame(ca: dict):
    """CA sheet = the per-window CA table (Time/TCD/ABP/MAP/MFV/Corr/MX/MeanMFV).
    FinalMX and MeanMFV are scalars, so they appear once (first row) rather than
    being repeated down the whole column."""
    if not (ca and ca.get("table")):
        return None
    df = pd.DataFrame(ca["table"])
    for col in ("FinalMX", "MeanMFV"):
        if col in df.columns and len(df) > 1:
            first = df[col].iloc[0]
            df[col] = [first] + [None] * (len(df) - 1)
    return df


# The CVR summary values that go in the combined CVR sheet's side block, in order.
_CVR_SHEET_SUMMARY = [
    ("base_mcbf", "Base MCBF"), ("base_wcbf", "Base WCBF"), ("base_co2", "Base CO2"),
    ("hyp_mcbf", "Hyp MCBF"), ("hyp_wcbf", "Hyp WCBF"), ("hyp_co2", "Hyp CO2"),
    ("delta_mcbf", "Delta MCBF"), ("delta_wcbf", "Delta WCBF"), ("delta_co2", "Delta CO2"),
    ("mcvr", "MCVR"), ("wcvr", "WCVR"),
]


def _cvr_frame(cvr: dict):
    """CVR sheet = the baseline and hypercapnic epoch samples side by side, plus a
    summary block of the CVR values (one sheet, replacing the old separate
    baseline/hypercapnia/summary sheets)."""
    if not cvr:
        return None
    base = cvr.get("baseline") or {}
    hyp = cvr.get("hypercapnia") or {}
    s = cvr.get("summary") or cvr or {}
    bt, bm, be = base.get("Time_s", []), base.get("meanU", []), base.get("envU", [])
    ht, hm, he = hyp.get("Time_s", []), hyp.get("meanU", []), hyp.get("envU", [])
    summary = [(label, s.get(key)) for key, label in _CVR_SHEET_SUMMARY if s.get(key) is not None]
    n = max(len(bt), len(ht), len(summary))
    if n == 0:
        return None

    def at(lst, i):
        return lst[i] if i < len(lst) else None

    cols = ["Baseline Time", "baseline meanU", "baseline envU", " ",
            "Hypercapnic Time", "Hypercapnic meanU", "Hypercapnic envU", "  ",
            "Metric", "Value"]
    rows = []
    for i in range(n):
        lab, val = summary[i] if i < len(summary) else (None, None)
        rows.append({
            "Baseline Time": at(bt, i), "baseline meanU": at(bm, i), "baseline envU": at(be, i),
            " ": None,
            "Hypercapnic Time": at(ht, i), "Hypercapnic meanU": at(hm, i), "Hypercapnic envU": at(he, i),
            "  ": None, "Metric": lab, "Value": val,
        })
    return pd.DataFrame(rows, columns=cols)


def pi_epochs_frame(tcd_epochs: list, abp_epochs: list = None):
    """PI tab = one row per beat (as in the on-screen PI table). Columns are the
    TCD metrics; if ABP epochs exist (after TCD->ABP sync) their metrics are
    appended as ABP_* columns. Pulse amplitude (Hi-Lo) replaces pulse width."""
    if not tcd_epochs:
        return None
    # Finalise PI the group's way (artificial PI uses the average native mean,
    # and the artificial mean is blanked) before laying out rows or averages.
    tcd_epochs = pi_analysis.resolve_pi(tcd_epochs)
    abp_by_id = {e["id"]: e for e in (abp_epochs or [])}
    rows = []
    for e in pi_analysis.numbered(tcd_epochs):
        native = e["type"] == pi_analysis.NATIVE
        row = {
            "Beat": ("Native #" if native else "Artificial #") + str(e["ordinal"]),
            "Type": "Native" if native else "Artificial",
            "Start_s": e.get("t_start"), "End_s": e.get("t_end"),
            "TCD_Hi": e.get("max"), "TCD_Lo": e.get("min"), "TCD_Mean": e.get("mean"),
            "TCD_PulseAmp": e.get("pulse_amp"), "TCD_PI": e.get("pi"),
        }
        a = abp_by_id.get(e["id"])
        if a:
            row.update({
                "ABP_Hi": a.get("max"), "ABP_Lo": a.get("min"), "ABP_Mean": a.get("mean"),
                "ABP_PulseAmp": a.get("pulse_amp"), "ABP_PI": a.get("pi"),
            })
        rows.append(row)

    has_abp = any(abp_by_id.get(e["id"], {}).get("max") is not None for e in tcd_epochs)

    # Per-class running averages (Hi/Lo/Mean/Pulse Amp/PI) as trailing rows.
    for kind, name in ((pi_analysis.NATIVE, "Native average"),
                       (pi_analysis.ARTIFICIAL, "Artificial average")):
        tcd_items = [e for e in tcd_epochs if e["type"] == kind]
        if not tcd_items:
            continue
        avg = pi_analysis.average_metrics(tcd_items)
        row = {"Beat": name, "Type": "average", "Start_s": None, "End_s": None,
               "TCD_Hi": avg["max"], "TCD_Lo": avg["min"], "TCD_Mean": avg["mean"],
               "TCD_PulseAmp": avg["pulse_amp"], "TCD_PI": avg["pi"]}
        if has_abp:
            abp_items = [abp_by_id[e["id"]] for e in tcd_items if e["id"] in abp_by_id]
            aavg = pi_analysis.average_metrics(abp_items)
            row.update({"ABP_Hi": aavg["max"], "ABP_Lo": aavg["min"], "ABP_Mean": aavg["mean"],
                        "ABP_PulseAmp": aavg["pulse_amp"], "ABP_PI": aavg["pi"]})
        rows.append(row)

    cols = ["Beat", "Type", "Start_s", "End_s",
            "TCD_Hi", "TCD_Lo", "TCD_Mean", "TCD_PulseAmp", "TCD_PI"]
    if has_abp:
        cols += ["ABP_Hi", "ABP_Lo", "ABP_Mean", "ABP_PulseAmp", "ABP_PI"]
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


def _pi_epochs_for_vessel(cacvr_sessions: list, vessel: str) -> list:
    """Serial LVAD: the tag IS the vessel, so a vessel's PI beats come from the
    tag(s) whose label matches."""
    out = []
    for s in cacvr_sessions:
        if (s.get("label") or "").strip().upper() == vessel:
            out.extend(s.get("pi_epochs", []) or [])
    return out


# Metric key -> column label for the long-format PI sheet.
_PI_LONG_METRICS = [("max", "Hi"), ("min", "Lo"), ("mean", "Mean"),
                    ("pulse_amp", "PA"), ("pi", "PI")]


def _pi_long_rows(epochs: list, key_fields: dict, subject_id):
    """Rows for one speed/vessel group in the long-format PI sheet. PI is resolved
    the group's way (artificial PI uses the average native mean), and each row
    carries the per-variable averages for its own Native/Artificial group,
    repeated on every row of that group (unmerged)."""
    resolved = pi_analysis.numbered(pi_analysis.resolve_pi(epochs))
    rows = []
    for type_name, type_code in ((pi_analysis.NATIVE, 1), (pi_analysis.ARTIFICIAL, 2)):
        grp = [e for e in resolved if e["type"] == type_name]
        if not grp:
            continue
        avg = pi_analysis.average_metrics(grp)
        for e in grp:
            row = {"Subject ID": subject_id, **key_fields,
                   "Beat Type (Native=1, Artificial=2)": type_code, "Beat #": e["ordinal"]}
            for mkey, label in _PI_LONG_METRICS:
                row[label] = e.get(mkey)
            for mkey, label in _PI_LONG_METRICS:
                row["Avg " + label] = avg.get(mkey)
            rows.append(row)
    return rows


def _pi_long_frame(state: SessionState, cacvr_sessions: list):
    """One long-format PI sheet for the whole study (all speeds for RAMPs, both
    vessels for Serial), for conversion to wide format in STATA. Columns are
    numeric codes per the agreed template; averages repeat per
    Native/Artificial group within each speed/vessel."""
    subject = _numeric_id(state.patient_id)
    ramps = state.study_mode == MODE_RAMPS
    rows = []
    if ramps:
        for order, s in enumerate(cacvr_sessions, start=1):
            eps = s.get("pi_epochs", []) or []
            if not eps:
                continue
            rows += _pi_long_rows(eps, {"Order": order, "Speed (#)": _numeric_id(s.get("label"))}, subject)
        key_cols = ["Order", "Speed (#)"]
    else:
        order = _numeric_id(state.session) or 1
        for vessel, code in (("MCA", 1), ("PCA", 2)):
            eps = _pi_epochs_for_vessel(cacvr_sessions, vessel)
            if not eps:
                continue
            rows += _pi_long_rows(eps, {"Order": order, "Vessel (MCA=1, PCA=2)": code}, subject)
        key_cols = ["Order", "Vessel (MCA=1, PCA=2)"]
    if not rows:
        return None
    cols = (["Subject ID"] + key_cols + ["Beat Type (Native=1, Artificial=2)", "Beat #"]
            + [lab for _, lab in _PI_LONG_METRICS]
            + ["Avg " + lab for _, lab in _PI_LONG_METRICS])
    return pd.DataFrame(rows, columns=cols)


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


def save_master_text(state: SessionState, path: str):
    """The cleaned Master as a comma-delimited text file in the same shape as the
    original raw recording (same columns and header), with the NaN-brushed edits
    applied. Lets the operator reload the cleaned data later without having to
    delete the same segments again. Removed samples are written as empty fields."""
    _master_dataframe(state).to_csv(path, index=False, na_rep="")


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


def _marks_frame(state: SessionState):
    if not state.marks_labels:
        return None
    return pd.DataFrame({"Time_s": state.marks_times, "Label": state.marks_labels})


def _study_frames(state: SessionState, cacvr_sessions: list) -> list:
    """The (sheet_name, dataframe) list for the single study workbook:
    Master + Marks, then one CA and one CVR sheet per speed (RAMPs) or vessel
    (Serial), then one long-format PI sheet. Sheet names are made unique and
    trimmed to Excel's 31-char limit."""
    frames = []
    if state.raw_headers and state.raw_rows:
        frames.append(("Master", _master_dataframe(state)))
        frames.append(("Marks", _marks_frame(state)))

    used = set()

    def sheet(name):
        name = name[:31]
        out, i = name, 2
        while out.lower() in used:
            suffix = f" {i}"
            out = name[:31 - len(suffix)] + suffix
            i += 1
        used.add(out.lower())
        return out

    if state.study_mode == MODE_RAMPS:
        for s in cacvr_sessions:
            label = s.get("label") or "speed"
            r = s.get("results", {}) or {}
            frames.append((sheet(f"CA {label}"),  _ca_frame((r.get("CA") or {}).get("MCA"))))
            frames.append((sheet(f"CVR {label}"), _cvr_frame((r.get("CVR") or {}).get("MCA"))))
    else:
        vessels = _merge_cacvr_by_vessel(cacvr_sessions)
        for v in ("MCA", "PCA"):
            frames.append((sheet(f"{v} CA"),  _ca_frame(vessels[v]["CA"])))
            frames.append((sheet(f"{v} CVR"), _cvr_frame(vessels[v]["CVR"])))

    frames.append((sheet("PI"), _pi_long_frame(state, cacvr_sessions)))
    return frames


def export_all_cacvr(state: SessionState, cacvr_sessions: list, out_dir: str):
    """
    Bundle the whole study per the agreed format: ONE Excel workbook per patient
    plus one progress JSON, zipped (with the cleaned Master as text too).

    The single workbook holds:
      - Master + Marks (the raw data with edits, skipped after a JSON reopen with
        no raw data),
      - one CA sheet and one CVR sheet per speed (RAMPs) or per vessel (Serial:
        MCA CA, MCA CVR, PCA CA, PCA CVR) — FinalMX shown once, the CVR baseline
        and hypercapnic epochs combined on one sheet with a summary block,
      - one long-format PI sheet for the whole study (all speeds/vessels), with
        numeric codes and per-group averages, for conversion to wide format.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    patient = _safe_stem(state.patient_id) or "patient"
    base = f"{patient}_{ts}"
    workdir = tempfile.mkdtemp()
    manifest = []
    try:
        wbname = f"{base}.xlsx"
        _workbook_from_frames(os.path.join(workdir, wbname), _study_frames(state, cacvr_sessions))
        manifest.append(wbname)

        # The cleaned Master as comma-delimited text, so the edited data can be
        # reloaded without re-deleting the same segments each time.
        if state.raw_headers and state.raw_rows:
            master_txt = f"{base}_Master.txt"
            save_master_text(state, os.path.join(workdir, master_txt))
            manifest.append(master_txt)

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

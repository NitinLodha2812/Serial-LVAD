"""
app.py  –  Flask server for Serial LVAD Study
"""

import os, json, math, tempfile, shutil
import numpy as np
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask.json.provider import DefaultJSONProvider
from backend.session_state import (
    SessionState, SAMPLE_RATE, FIVE_MIN_SAMPLES, BASELINE_SAMPLES, HYPERCAP_SAMPLES,
    window_end_for_valid_count,
)
from backend.calculations import compute_mx, compute_cvr
from backend.export import save_excel, save_json, save_pi_excel
from backend import pi_analysis


def _json_safe(o):
    """Recursively replace NaN/Inf floats with None so every response is strict,
    browser-parseable JSON.

    The calculations legitimately produce NaN — e.g. MAP/MFV/correlation values
    for 3-second windows that fall on cleaned/removed data. Python's json module
    emits these as the literal token ``NaN``, which is invalid JSON and makes the
    browser's ``response.json()`` throw ("non-JSON response"), so a computed MX
    would never reach the GUI. numpy.float64 subclasses float, so values straight
    out of numpy are handled here too.
    """
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


class SafeJSONProvider(DefaultJSONProvider):
    """Flask JSON provider that scrubs NaN/Inf out of every response body."""

    def dumps(self, obj, **kwargs):
        return super().dumps(_json_safe(obj), **kwargs)


app = Flask(__name__)
app.json = SafeJSONProvider(app)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "serial_lvad_uploads")
EXPORT_DIR = os.path.join(tempfile.gettempdir(), "serial_lvad_exports")
# PI selections auto-save here, one file per (recording, speed) — the web
# equivalent of PI.m's `<basename>_speed<N>_selections.mat` beside the data.
PI_SESSION_DIR = os.path.join(tempfile.gettempdir(), "serial_lvad_pi_sessions")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(PI_SESSION_DIR, exist_ok=True)

# ── global session state (single-user desktop app) ──
state: SessionState | None = None


# ═══════════════════════════════════════════════════════════════════════
#  PAGES
# ═══════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ═══════════════════════════════════════════════════════════════════════
#  LOAD FILE
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/load", methods=["POST"])
def api_load():
    global state
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file provided"}), 400

    # Sanitize filename — remove special chars, keep spaces
    import re as _re
    safe_name = _re.sub(r'[^\w\s.\-]', '_', f.filename)
    if not safe_name:
        safe_name = "upload.txt"
    filepath = os.path.join(UPLOAD_DIR, safe_name)

    # A saved progress (.json) file is not a raw recording — it has no waveform
    # samples — so feeding it to the CSV parser yields a confusing tokenizing
    # error. Catch it early with a clear message instead.
    if (f.filename or "").lower().endswith(".json"):
        return jsonify({"error": (
            "That looks like a saved progress (.json) file, not a raw recording. "
            "Reopening saved progress isn't supported yet — please load the "
            "original .txt/.csv recording."
        )}), 400

    try:
        f.save(filepath)
    except Exception as e:
        return jsonify({"error": f"File save failed: {e}"}), 400

    # Parse into a fresh state and only commit it to the global session on
    # success. Otherwise a failed load (wrong file, bad format) would wipe the
    # session the user is in the middle of working on.
    new_state = SessionState()
    try:
        new_state.load_txt(filepath, f.filename)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Load error:\n{tb}")
        return jsonify({"error": f"Parse error: {e}"}), 400

    try:
        overview = new_state.get_overview_json()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Overview error:\n{tb}")
        return jsonify({"error": f"Data processing error: {e}"}), 400

    state = new_state
    return jsonify(overview)


# ═══════════════════════════════════════════════════════════════════════
#  DATA RANGE (for zoom / full-res)
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/range")
def api_range():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    start = float(request.args.get("start", 0))
    end = float(request.args.get("end", state.time[-1]))
    return jsonify(state.get_range_json(start, end))


# ═══════════════════════════════════════════════════════════════════════
#  CA – SELECT 5-MIN WINDOW
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/ca/select", methods=["POST"])
def api_ca_select():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400

    body = request.get_json()
    start_time = float(body.get("start_time", 0))

    idx_start = int(np.searchsorted(state.time, start_time))

    # Extend the 5-min window past any brushed-out / NaN samples so it still
    # captures FIVE_MIN_SAMPLES of *valid* data — a sample counts as valid only
    # when both ABP and the TCD envelope are present, since the MX correlation
    # consumes both. With no removed data this reduces to the original
    # fixed-length window (idx_start + FIVE_MIN_SAMPLES - 1).
    ca_valid = ~np.isnan(state.abp) & ~np.isnan(state.env_u)
    idx_end, n_valid, exhausted = window_end_for_valid_count(
        ca_valid, idx_start, FIVE_MIN_SAMPLES
    )
    n_extended = (idx_end - idx_start + 1) - FIVE_MIN_SAMPLES
    if exhausted:
        state.log(
            f"CA: Not enough valid samples to fill a 5-min window "
            f"({n_valid}/{FIVE_MIN_SAMPLES} valid); using all available data."
        )
    elif n_extended > 0:
        state.log(
            f"CA: Window extended by {n_extended} samples "
            f"({n_extended / SAMPLE_RATE:.1f}s) to recover {FIVE_MIN_SAMPLES} "
            f"valid samples past removed data."
        )

    inds = slice(idx_start, idx_end + 1)
    # Preserve NaN samples in the selection — compute_mx slices the array
    # into non-overlapping 375-sample (3-second) windows, so dropping NaN
    # rows here would silently misalign the windowing in wall-clock time.
    # NaN handling lives inside compute_mx (>=50% valid threshold + nanmean).
    sel_time = state.time[inds].copy()
    sel_env  = state.env_u[inds].copy()
    sel_abp  = state.abp[inds].copy()

    state.ca_selection = {
        "time": sel_time,
        "env": sel_env,
        "abp": sel_abp,
    }

    n_nan_env = int(np.isnan(sel_env).sum())
    n_nan_abp = int(np.isnan(sel_abp).sum())
    state.log(
        f"CA: 5-min selection stored, starting at t={state.time[idx_start]:.2f} "
        f"({len(sel_time)} samples; NaN env={n_nan_env}, NaN abp={n_nan_abp})"
    )

    # Frontend only needs the time range for the shaded mask annotation —
    # the full-resolution arrays remain in state for the calculation.
    return jsonify({
        "start_time": float(state.time[idx_start]),
        "end_time": float(state.time[idx_end]),
        "n_samples": int(len(sel_time)),
        "n_nan_env": n_nan_env,
        "n_nan_abp": n_nan_abp,
    })


# ═══════════════════════════════════════════════════════════════════════
#  CA – CLEAR SELECTION
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/ca/clear", methods=["POST"])
def api_ca_clear():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    state.ca_selection = None
    state.ca_result = None
    state.log("CA: selection cleared.")
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════
#  CA – CALCULATE MX
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/ca/calculate", methods=["POST"])
def api_ca_calculate():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    if state.ca_selection is None:
        return jsonify({"error": "No selection. Click Select Start first."}), 400

    body = request.get_json() or {}
    vessel = body.get("vessel", "MCA")

    abp = state.ca_selection["abp"]
    env = state.ca_selection["env"]
    sel_time = state.ca_selection["time"]

    state.log("CA: Beginning MX calculation...")
    result = compute_mx(abp, env)

    if "error" in result:
        state.log(f"CA: MX calculation failed — {result['error']}")
        return jsonify(result), 400

    state.log(f"CA: MX calculation complete. MX = {result['final_mx']}")
    state.ca_result = result

    # Build table for export (matching MATLAB Tout)
    n_time = len(sel_time)
    n_map = len(result["MAP"])
    n_mfv = len(result["MFV"])
    n_corr = len(result["corr_coeffs"])
    max_n = max(n_time, n_map, n_mfv, n_corr)

    def pad(arr, length):
        out = [None] * length
        for i, v in enumerate(arr):
            if i < length:
                out[i] = round(float(v), 4) if v is not None and not (isinstance(v, float) and np.isnan(v)) else None
        return out

    table = {
        "Time_s": pad(sel_time.tolist(), max_n),
        "TCD": pad(env.tolist(), max_n),
        "ABP": pad(abp.tolist(), max_n),
        "MAP": pad(result["MAP"], max_n),
        "MFV": pad(result["MFV"], max_n),
        "CorrCoeff": pad(result["corr_coeffs"], max_n),
        "FinalMX": [result["final_mx"]] * max_n,
        # Per-vessel TCD-envelope average; vessel label is the user's choice
        # at calc time and lives in the sheet name (MCA_CA / PCA_CA).
        "MeanMFV": [result.get("mean_mfv")] * max_n,
    }

    result["table"] = table
    state.results["CA"][vessel] = result
    state.log(f"CA: Stored CA result for {vessel} into unified sessionResults.")

    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════
#  CVR – SELECT BASELINE
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/cvr/select_baseline", methods=["POST"])
def api_cvr_select_baseline():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400

    body = request.get_json()
    start_time = float(body.get("start_time", 0))
    idx = int(np.searchsorted(state.time, start_time))

    # Extend the baseline window past removed/NaN samples so it still spans
    # BASELINE_SAMPLES of valid data. A sample is valid only when meanU, envU
    # and ETCO2 are all present, since the CVR baseline averages all three.
    base_valid = ~np.isnan(state.mean_u) & ~np.isnan(state.env_u) & ~np.isnan(state.etco2)
    idx_end, n_valid, exhausted = window_end_for_valid_count(
        base_valid, idx, BASELINE_SAMPLES
    )
    n_extended = (idx_end - idx + 1) - BASELINE_SAMPLES
    if exhausted:
        state.log(
            f"CVR: baseline could not reach {BASELINE_SAMPLES} valid samples "
            f"({n_valid} found); using all available data."
        )
    elif n_extended > 0:
        state.log(
            f"CVR: baseline window extended by {n_extended} samples "
            f"({n_extended / SAMPLE_RATE:.1f}s) past removed data."
        )
    inds = slice(idx, idx_end + 1)

    # Preserve NaN; nanmean inside compute_cvr handles it correctly.
    sel_time = state.time[inds].copy()
    sel_mean = state.mean_u[inds].copy()
    sel_env  = state.env_u[inds].copy()
    sel_co2  = state.etco2[inds].copy()

    if len(sel_time) == 0 or np.all(np.isnan(sel_mean) & np.isnan(sel_env) & np.isnan(sel_co2)):
        return jsonify({"error": "Baseline selection contains no valid data."}), 400

    state.cvr_baseline = {
        "time": sel_time, "mean": sel_mean,
        "env": sel_env, "co2": sel_co2,
    }
    state.log(
        f"CVR: baseline start t={state.time[idx]:.2f} "
        f"({len(sel_time)} samples)"
    )

    return jsonify({
        "start_time": float(state.time[idx]),
        "end_time": float(state.time[idx_end]),
        "n_samples": int(len(sel_time)),
    })


# ═══════════════════════════════════════════════════════════════════════
#  CVR – SELECT HYPERCAPNIA
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/cvr/select_hypercapnia", methods=["POST"])
def api_cvr_select_hypercapnia():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400

    body = request.get_json()
    start_time = float(body.get("start_time", 0))
    idx = int(np.searchsorted(state.time, start_time))

    # Extend the hypercapnia window past removed/NaN samples so it still spans
    # HYPERCAP_SAMPLES of valid data (meanU, envU and ETCO2 all present).
    hyp_valid = ~np.isnan(state.mean_u) & ~np.isnan(state.env_u) & ~np.isnan(state.etco2)
    idx_end, n_valid, exhausted = window_end_for_valid_count(
        hyp_valid, idx, HYPERCAP_SAMPLES
    )
    n_extended = (idx_end - idx + 1) - HYPERCAP_SAMPLES
    if exhausted:
        state.log(
            f"CVR: hypercapnia could not reach {HYPERCAP_SAMPLES} valid samples "
            f"({n_valid} found); using all available data."
        )
    elif n_extended > 0:
        state.log(
            f"CVR: hypercapnia window extended by {n_extended} samples "
            f"({n_extended / SAMPLE_RATE:.1f}s) past removed data."
        )
    inds = slice(idx, idx_end + 1)

    sel_time = state.time[inds].copy()
    sel_mean = state.mean_u[inds].copy()
    sel_env  = state.env_u[inds].copy()
    sel_co2  = state.etco2[inds].copy()

    if len(sel_time) == 0 or np.all(np.isnan(sel_mean) & np.isnan(sel_env) & np.isnan(sel_co2)):
        return jsonify({"error": "Hypercapnia selection contains no valid data."}), 400

    state.cvr_hypercap = {
        "time": sel_time, "mean": sel_mean,
        "env": sel_env, "co2": sel_co2,
    }
    state.log(
        f"CVR: hypercapnia start t={state.time[idx]:.2f} "
        f"({len(sel_time)} samples)"
    )

    return jsonify({
        "start_time": float(state.time[idx]),
        "end_time": float(state.time[idx_end]),
        "n_samples": int(len(sel_time)),
    })


# ═══════════════════════════════════════════════════════════════════════
#  CVR – SELECT CO2 SEARCH WINDOW (gas-on → gas-off)
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/cvr/select_co2_window", methods=["POST"])
def api_cvr_select_co2_window():
    """
    Define an explicit time range for peak-ETCO2 detection. This is broader
    than the (~10 s) hypercapnia selection and typically spans gas-on to
    gas-off, so the search captures the true ETCO2 peak even when the
    brain-response window lags the inhaled-CO2 rise.
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400

    body = request.get_json() or {}
    try:
        start = float(body["start_time"])
        end   = float(body["end_time"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "start_time and end_time (numbers) are required."}), 400
    if end < start:
        start, end = end, start
    if end <= start:
        return jsonify({"error": "CO2 window must have non-zero width."}), 400

    # Snap to actual sample indices on the time axis
    i0 = int(np.searchsorted(state.time, start))
    i1 = int(np.searchsorted(state.time, end))
    i0 = max(0, min(i0, len(state.time) - 1))
    i1 = max(0, min(i1, len(state.time) - 1))
    if i1 <= i0:
        return jsonify({"error": "CO2 window snapped to zero samples."}), 400

    t0 = float(state.time[i0])
    t1 = float(state.time[i1])
    state.cvr_co2_window = {
        "start": t0, "end": t1,
        "i_start": i0, "i_end": i1,
    }
    state.log(
        f"CVR: CO2 search window set t={t0:.2f}..{t1:.2f} "
        f"({i1 - i0 + 1} samples)"
    )
    return jsonify({"start_time": t0, "end_time": t1, "n_samples": i1 - i0 + 1})


# ═══════════════════════════════════════════════════════════════════════
#  CVR – DETECT PEAK ETCO2
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/cvr/detect_peak", methods=["POST"])
def api_cvr_detect_peak():
    """
    Find the highest ETCO2 sample. Search range, in priority order:
      1. The user-selected CO2 window (set via /api/cvr/select_co2_window) —
         this is the gas-on → gas-off range and is the physiologically
         correct place to look for the peak.
      2. Falls back to the hypercapnia selection if no CO2 window is set,
         preserving the old behaviour.
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400

    source = None
    if state.cvr_co2_window is not None:
        i0 = state.cvr_co2_window["i_start"]
        i1 = state.cvr_co2_window["i_end"] + 1
        co2 = state.etco2[i0:i1]
        t   = state.time[i0:i1]
        source = "CO2 window"
    elif state.cvr_hypercap is not None:
        co2 = state.cvr_hypercap["co2"]
        t   = state.cvr_hypercap["time"]
        source = "hypercapnia window (fallback — set a CO2 window for the full gas-on→off range)"
    else:
        return jsonify({"error": "Set a CO2 window (or a hypercapnia selection) before detecting peak etCO2."}), 400

    if np.all(np.isnan(co2)):
        return jsonify({"error": "ETCO2 has no valid samples in the selected range."}), 400

    idx = int(np.nanargmax(co2))
    val = float(co2[idx])
    peak_time = float(t[idx])

    state.cvr_peak_etco2 = val
    state.cvr_peak_time  = peak_time
    state.log(f"CVR: peak etCO2 = {val:.2f} at t={peak_time:.2f} (source: {source})")

    return jsonify({
        "peak_value": round(val, 4),
        "peak_time":  round(peak_time, 4),
        "source": source,
    })


# ═══════════════════════════════════════════════════════════════════════
#  CVR – CLEAR
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/cvr/clear", methods=["POST"])
def api_cvr_clear():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    state.cvr_baseline = None
    state.cvr_hypercap = None
    state.cvr_co2_window = None
    state.cvr_peak_etco2 = None
    state.cvr_peak_time = None
    state.cvr_result = None
    state.log("CVR: selections cleared.")
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════
#  CVR – CALCULATE
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/cvr/calculate", methods=["POST"])
def api_cvr_calculate():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    if state.cvr_baseline is None or state.cvr_hypercap is None:
        return jsonify({"error": "Both baseline and hypercapnia selections are required."}), 400

    body = request.get_json() or {}
    vessel = body.get("vessel", "MCA")

    state.log("CVR: Beginning CVR calculation...")
    result = compute_cvr(
        state.cvr_baseline["mean"], state.cvr_baseline["env"], state.cvr_baseline["co2"],
        state.cvr_hypercap["mean"], state.cvr_hypercap["env"], state.cvr_hypercap["co2"],
        peak_co2=state.cvr_peak_etco2,
    )

    if "error" in result:
        state.log(f"CVR: calculation failed — {result['error']}")
        return jsonify(result), 400

    state.log(f"CVR: CVR calculation complete. MCVR = {result['mcvr']}, WCVR = {result['wcvr']}")
    # Surface the CO2 driver behind the CVR result in the activity log only
    # (not the CVR window): delta CO2 = hypercapnia CO2 - baseline CO2, where
    # the hypercapnia value is the detected peak ETCO2 if one was set.
    state.log(f"CVR: Delta CO2 (hypercapnia - baseline) = {result['delta_co2']} mmHg")
    state.cvr_result = result

    # Build export tables
    base_n = len(state.cvr_baseline["mean"])
    hyp_n = len(state.cvr_hypercap["mean"])
    export_data = {
        "baseline": {
            "Time_s": (np.arange(base_n) / SAMPLE_RATE).tolist(),
            "meanU": state.cvr_baseline["mean"].tolist(),
            "envU": state.cvr_baseline["env"].tolist(),
            "etco2": state.cvr_baseline["co2"].tolist(),
        },
        "hypercapnia": {
            "Time_s": (np.arange(hyp_n) / SAMPLE_RATE).tolist(),
            "meanU": state.cvr_hypercap["mean"].tolist(),
            "envU": state.cvr_hypercap["env"].tolist(),
            "etco2": state.cvr_hypercap["co2"].tolist(),
        },
        "summary": result,
    }

    state.results["CVR"][vessel] = export_data
    state.log(f"CVR: Stored CVR result for {vessel} into unified sessionResults.")

    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════
#  EDIT – REPLACE BRUSHED POINTS WITH NaN
# ═══════════════════════════════════════════════════════════════════════

# Whitelist of signals the user may edit. Maps the request name to the
# attribute name on SessionState.
EDITABLE_SIGNALS = {
    "mean_u": "mean_u",
    "env_u":  "env_u",
    "etco2":  "etco2",
    "co2":    "co2",
    "abp":    "abp",
}


@app.route("/api/edit/nan", methods=["POST"])
def api_edit_nan():
    """
    Replace samples inside one or more brushed rectangles with NaN.

    Body:
      {
        "signal": "env_u" | "mean_u" | "etco2" | "abp",
        "ranges": [{ "x_min": float, "x_max": float,
                     "y_min": float, "y_max": float }, ...]
      }

    For each rectangle, *all* full-resolution samples whose (time, value)
    falls inside it become NaN. This preserves the time axis (no row is
    removed) and propagates to plots, CA/CVR calculations, and exports.
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400

    body = request.get_json() or {}
    signal = body.get("signal")
    ranges = body.get("ranges") or []

    if signal not in EDITABLE_SIGNALS:
        return jsonify({"error": f"Unknown signal '{signal}'"}), 400
    if not isinstance(ranges, list) or not ranges:
        return jsonify({"error": "No ranges provided"}), 400

    arr = getattr(state, EDITABLE_SIGNALS[signal])
    if arr is None or len(arr) == 0:
        return jsonify({"error": f"Signal '{signal}' is empty"}), 400

    total_changed = 0
    for r in ranges:
        try:
            x_min = float(r["x_min"]); x_max = float(r["x_max"])
            y_min = float(r["y_min"]); y_max = float(r["y_max"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "Each range needs numeric x_min, x_max, y_min, y_max"}), 400
        if x_max < x_min: x_min, x_max = x_max, x_min
        if y_max < y_min: y_min, y_max = y_max, y_min

        mask = (
            (state.time >= x_min) & (state.time <= x_max) &
            (arr     >= y_min) & (arr     <= y_max) &
            ~np.isnan(arr)
        )
        n = int(mask.sum())
        if n > 0:
            arr[mask] = np.nan
            total_changed += n

    state.log(
        f"Edit: replaced {total_changed} sample(s) in {signal} with NaN "
        f"(across {len(ranges)} brush region(s))."
    )
    return jsonify({
        "signal": signal,
        "changed": total_changed,
        "total_nan": int(np.isnan(arr).sum()),
    })


# ═══════════════════════════════════════════════════════════════════════
#  PI – PULSATILITY INDEX (port of PI.m)
# ═══════════════════════════════════════════════════════════════════════

def _pi_payload():
    """Everything the PI tab needs to redraw itself after any mutation."""
    return {
        "epochs": pi_analysis.numbered(state.pi_epochs),
        "summary": pi_analysis.summarize(state.pi_epochs),
        "speed": state.pi_speed,
        "vessel": state.pi_vessel,
        "patient_id": state.patient_id,
        "base_name": state.base_name,
    }


def _pi_autosave():
    """
    Persist the current selections for the current speed.

    PI.m auto-saves on every change to `updateSelectionCount`, so a crash or a
    mis-click never costs an afternoon of brushing. Failures are logged, not
    raised — losing the auto-save must not fail the selection that triggered it.
    """
    if not state.base_name:
        return None
    payload = {
        "base_name": state.base_name,
        "patient_id": state.patient_id,
        "speed": state.pi_speed,
        "vessel": state.pi_vessel,
        "epochs": state.pi_epochs,
    }
    try:
        return pi_analysis.save_session(PI_SESSION_DIR, state.base_name, state.pi_speed, payload)
    except Exception as e:
        state.log(f"PI: WARNING — auto-save failed: {e}")
        return None


@app.route("/api/pi/trace")
def api_pi_trace():
    """Envelope trace for the visible window, decimated only as needed."""
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    start = float(request.args.get("start", 0))
    end = float(request.args.get("end", state.time[-1] if len(state.time) else 0))
    try:
        max_points = int(request.args.get("max_points", 8000))
    except ValueError:
        max_points = 8000
    max_points = max(500, min(max_points, 50000))
    return jsonify(state.get_pi_trace(start, end, max_points))


@app.route("/api/pi/state")
def api_pi_state():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    return jsonify(_pi_payload())


@app.route("/api/pi/meta", methods=["POST"])
def api_pi_meta():
    """Update the PI tab's Session/Speed and Vessel fields."""
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    body = request.get_json() or {}
    if "speed" in body:
        state.pi_speed = str(body["speed"]).strip()
    if "vessel" in body:
        state.pi_vessel = str(body["vessel"]).strip()
    return jsonify({"ok": True, "speed": state.pi_speed, "vessel": state.pi_vessel})


@app.route("/api/pi/select", methods=["POST"])
def api_pi_select():
    """
    Commit the brushed rectangle as one Native or Artificial epoch.

    The rectangle arrives in data coordinates; the samples are taken from the
    full-resolution envelope, not from whatever the chart happened to be
    displaying, so a decimated view never truncates an epoch.
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400

    body = request.get_json() or {}
    kind = str(body.get("type", "")).lower()
    if kind not in (pi_analysis.NATIVE, pi_analysis.ARTIFICIAL):
        return jsonify({"error": "type must be 'native' or 'artificial'"}), 400

    rect = body.get("rect") or {}
    try:
        {k: float(rect[k]) for k in ("x_min", "x_max", "y_min", "y_max")}
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "rect needs numeric x_min, x_max, y_min, y_max"}), 400

    t, a = pi_analysis.extract_rect(state.time, state.env_u, rect)
    if len(t) == 0:
        return jsonify({"error": "No envelope samples inside the brushed region."}), 400

    epoch = pi_analysis.make_epoch(state.pi_next_id, kind, t, a)
    state.pi_next_id += 1
    state.pi_epochs.append(epoch)
    state.log(
        f"PI: selected {kind} epoch #{epoch['id']} "
        f"t={epoch['t_start']:.2f}..{epoch['t_end']:.2f} "
        f"({epoch['n_samples']} samples, PI={epoch['pi']})"
    )
    _pi_autosave()
    return jsonify(_pi_payload())


@app.route("/api/pi/auto_select", methods=["POST"])
def api_pi_auto_select():
    """
    Lay down artificial epochs at a fixed cadence from the clicked point to the
    right edge of the current view — PI.m's autoSelectArtificial. Pump beats are
    metronomic, so one click plus the pump period captures them all.
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    if len(state.time) == 0:
        return jsonify({"error": "No data loaded"}), 400

    body = request.get_json() or {}
    try:
        start = float(body["start_time"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "start_time (number) is required"}), 400

    x_min = float(body.get("x_min", state.time[0]))
    x_max = float(body.get("x_max", state.time[-1]))
    interval = float(body.get("interval", pi_analysis.AUTO_INTERVAL_S))
    half_width = float(body.get("half_width", pi_analysis.AUTO_HALF_WIDTH_S))
    if interval <= 0 or half_width <= 0:
        return jsonify({"error": "interval and half_width must be positive"}), 400

    centres = pi_analysis.auto_mark_points(start, x_min, x_max, interval)
    added = 0
    for centre in centres:
        t, a = pi_analysis.extract_span(state.time, state.env_u,
                                        centre - half_width, centre + half_width)
        if len(t) == 0:
            continue
        state.pi_epochs.append(
            pi_analysis.make_epoch(state.pi_next_id, pi_analysis.ARTIFICIAL, t, a)
        )
        state.pi_next_id += 1
        added += 1

    if added == 0:
        return jsonify({"error": "No epochs found — try a start point inside the data."}), 400

    state.log(
        f"PI: auto-selected {added} artificial epoch(s) every {interval}s "
        f"(±{half_width}s) from t={start:.2f} to t={x_max:.2f}"
    )
    _pi_autosave()
    return jsonify({**_pi_payload(), "added": added})


@app.route("/api/pi/undo", methods=["POST"])
def api_pi_undo():
    """Remove the most recently added epoch."""
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    if not state.pi_epochs:
        return jsonify({"error": "No selections to undo."}), 400
    e = state.pi_epochs.pop()
    state.log(f"PI: undid {e['type']} epoch #{e['id']}")
    _pi_autosave()
    return jsonify(_pi_payload())


@app.route("/api/pi/deselect", methods=["POST"])
def api_pi_deselect():
    """Remove specific epochs by id (PI.m's multi-select Deselect dialog)."""
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    body = request.get_json() or {}
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids (non-empty list) is required"}), 400
    try:
        drop = {int(i) for i in ids}
    except (TypeError, ValueError):
        return jsonify({"error": "ids must be integers"}), 400

    before = len(state.pi_epochs)
    state.pi_epochs = [e for e in state.pi_epochs if e["id"] not in drop]
    removed = before - len(state.pi_epochs)
    if removed == 0:
        return jsonify({"error": "None of those epochs exist."}), 400
    state.log(f"PI: removed {removed} epoch(s).")
    _pi_autosave()
    return jsonify(_pi_payload())


@app.route("/api/pi/clear", methods=["POST"])
def api_pi_clear():
    """
    Drop every selection for the current speed.

    Deliberately does *not* auto-save: the saved session for this speed stays
    on disk so a mis-click here is recoverable via Load Speed.
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    state.pi_epochs = []
    state.log("PI: selections cleared (saved session left on disk).")
    return jsonify(_pi_payload())


@app.route("/api/pi/sessions")
def api_pi_sessions():
    """Saved per-speed selection files for the loaded recording."""
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    return jsonify({"sessions": pi_analysis.list_sessions(PI_SESSION_DIR, state.base_name)})


@app.route("/api/pi/load_session", methods=["POST"])
def api_pi_load_session():
    """Replace the current selections with a saved speed's."""
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    body = request.get_json() or {}
    fname = body.get("filename")
    if not fname:
        return jsonify({"error": "filename is required"}), 400
    try:
        data = pi_analysis.load_session(PI_SESSION_DIR, fname)
    except (OSError, ValueError) as e:
        return jsonify({"error": f"Could not load session: {e}"}), 400

    epochs = data.get("epochs", [])
    state.pi_epochs = epochs
    state.pi_next_id = max((e["id"] for e in epochs), default=0) + 1
    state.pi_speed = data.get("speed", "")
    state.pi_vessel = data.get("vessel", "") or state.pi_vessel
    state.log(
        f"PI: loaded session {fname} — "
        f"{sum(1 for e in epochs if e['type'] == pi_analysis.NATIVE)} native, "
        f"{sum(1 for e in epochs if e['type'] == pi_analysis.ARTIFICIAL)} artificial."
    )
    return jsonify(_pi_payload())


@app.route("/api/pi/load_all")
def api_pi_load_all():
    """
    Every saved speed at once, for the read-only overlay comparison view.
    Does not touch the working selections.
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    speeds = []
    for meta in pi_analysis.list_sessions(PI_SESSION_DIR, state.base_name):
        try:
            data = pi_analysis.load_session(PI_SESSION_DIR, meta["filename"])
        except (OSError, ValueError):
            continue
        speeds.append({**meta, "epochs": data.get("epochs", [])})
    if not speeds:
        return jsonify({"error": "No saved speed sessions found for this recording."}), 400
    return jsonify({"speeds": speeds})


@app.route("/api/pi/next_speed", methods=["POST"])
def api_pi_next_speed():
    """
    Move on to the next pump speed: clear the working selections and adopt the
    new speed label. Reports whether that speed already has saved selections so
    the UI can offer to load them, as PI.m's questdlg does.
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    body = request.get_json() or {}
    speed = str(body.get("speed", "")).strip()
    if not speed:
        return jsonify({"error": "A speed label is required."}), 400

    state.pi_epochs = []
    state.pi_speed = speed
    fname = pi_analysis.session_filename(state.base_name, speed)
    existing = os.path.isfile(os.path.join(PI_SESSION_DIR, fname))
    state.log(f"PI: ready for speed {speed!r}. Existing selections: {existing}")
    return jsonify({
        **_pi_payload(),
        "existing_session": fname if existing else None,
    })


@app.route("/api/pi/export", methods=["POST"])
def api_pi_export():
    """
    Append this speed's epoch metrics as one row of the "PI Demographics"
    sheet. A prior workbook may be uploaded to append to; without one the
    sheet is created from scratch.
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    if not state.pi_epochs:
        return jsonify({"error": "No epochs selected — nothing to export."}), 400

    patient_id = (request.form.get("patient_id") or state.patient_id or "").strip()
    speed = (request.form.get("speed") or state.pi_speed or "").strip()
    vessel = (request.form.get("vessel") or state.pi_vessel or "").strip()

    prior_path = None
    prior_name = None
    f = request.files.get("workbook")
    if f and f.filename:
        import re as _re
        prior_name = _re.sub(r"[^\w\s.\-]", "_", f.filename)
        prior_path = os.path.join(UPLOAD_DIR, prior_name)
        try:
            f.save(prior_path)
        except Exception as e:
            return jsonify({"error": f"Could not read the prior workbook: {e}"}), 400

    row = pi_analysis.build_excel_row(state.pi_epochs, patient_id, speed, vessel)
    stem = f"{patient_id or state.base_name or 'PI'}_PI"
    try:
        fname = save_pi_excel(row, EXPORT_DIR, prior_path=prior_path,
                              prior_filename=prior_name, fallback_stem=stem)
    except Exception as e:
        import traceback
        print(f"PI export error:\n{traceback.format_exc()}")
        state.log(f"PI: export failed — {e}")
        return jsonify({"error": f"Export failed: {e}"}), 400

    summary = pi_analysis.summarize(state.pi_epochs)
    state.log(
        f"PI: exported {summary['n_native']} native + {summary['n_artificial']} "
        f"artificial epoch(s) to {fname}"
    )
    return jsonify({"filename": fname, "summary": summary})


# ═══════════════════════════════════════════════════════════════════════
#  METADATA UPDATE
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/metadata", methods=["POST"])
def api_metadata():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    body = request.get_json()
    if "patient_id" in body:
        state.patient_id = body["patient_id"]
    if "session" in body:
        state.session = body["session"]
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════
#  LOG
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/log")
def api_log():
    if state is None:
        return jsonify({"lines": []})
    return jsonify({"lines": state.log_lines})


# ═══════════════════════════════════════════════════════════════════════
#  SAVE / EXPORT
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/save", methods=["POST"])
def api_save():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400

    # Nothing to write yet → openpyxl would raise "At least one sheet must be
    # visible". Give a friendly message instead.
    has_raw = bool(getattr(state, "raw_headers", None))
    has_results = any(state.results[a][v] for a in ("CA", "CVR") for v in ("MCA", "PCA"))
    if not has_raw and not has_results:
        return jsonify({"error": "Nothing to save yet — load a recording first."}), 400

    body = request.get_json() or {}
    fmt = body.get("format", "excel")

    try:
        if fmt == "json":
            fname = save_json(state, EXPORT_DIR)
            state.log(f"Saved JSON: {fname}")
        else:
            fname = save_excel(state, EXPORT_DIR)
            state.log(f"Saved Excel: {fname}")
    except Exception as e:
        import traceback
        print(f"Save error:\n{traceback.format_exc()}")
        state.log(f"Save failed: {e}")
        return jsonify({"error": f"Save failed: {e}"}), 500

    return jsonify({"filename": fname})


@app.route("/api/download/<filename>")
def api_download(filename):
    return send_from_directory(EXPORT_DIR, filename, as_attachment=True)


# ═══════════════════════════════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(debug=True, port=5000)
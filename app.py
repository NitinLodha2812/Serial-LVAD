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
from backend.export import save_excel, save_json, save_pi_excel, export_all_cacvr
from backend import pi_analysis, cacvr_sessions
from backend import export as export_backend


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
# CA/CVR results auto-save here, one file per (recording, session/speed).
CACVR_SESSION_DIR = os.path.join(tempfile.gettempdir(), "serial_lvad_cacvr_sessions")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(PI_SESSION_DIR, exist_ok=True)
os.makedirs(CACVR_SESSION_DIR, exist_ok=True)

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
    _cacvr_autosave()

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
    # BASELINE_SAMPLES of valid data. The window covers the TCD signals only
    # (meanU/envU) — CO2 is no longer averaged over a window, so it plays no
    # part in the validity test.
    base_valid = ~np.isnan(state.mean_u) & ~np.isnan(state.env_u)
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

    if len(sel_time) == 0 or np.all(np.isnan(sel_mean) & np.isnan(sel_env)):
        return jsonify({"error": "Baseline selection contains no valid TCD data."}), 400

    state.cvr_baseline = {"time": sel_time, "mean": sel_mean, "env": sel_env}
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
    # HYPERCAP_SAMPLES of valid TCD data (meanU/envU). CO2 is not averaged here.
    hyp_valid = ~np.isnan(state.mean_u) & ~np.isnan(state.env_u)
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

    if len(sel_time) == 0 or np.all(np.isnan(sel_mean) & np.isnan(sel_env)):
        return jsonify({"error": "Hypercapnia selection contains no valid TCD data."}), 400

    state.cvr_hypercap = {"time": sel_time, "mean": sel_mean, "env": sel_env}
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
#  CVR – SELECT A CO2 POINT (true end-tidal CO2, picked off the waveform)
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/cvr/select_co2_point", methods=["POST"])
def api_cvr_select_co2_point():
    """
    Record the true end-tidal CO2 for baseline or hypercapnia as a single
    operator-picked point on the CO2 waveform.

    The device's ETCO2 channel is invalid for this protocol (the mask feeds
    CO2-enriched air, so the sensor mixes inhaled and expired CO2), so the
    operator reads the real end-tidal value off the CO2 waveform by eye and
    clicks it. We take the CO2 sample nearest the clicked time; if that exact
    sample was brushed out (NaN), we snap to the nearest valid sample.

    Body: { "which": "baseline" | "hypercapnia", "time": <seconds> }
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400

    body = request.get_json() or {}
    which = str(body.get("which", "")).lower()
    if which not in ("baseline", "hypercapnia"):
        return jsonify({"error": "which must be 'baseline' or 'hypercapnia'"}), 400
    try:
        t_click = float(body["time"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "time (number) is required"}), 400

    if state.co2 is None or state.co2.size == 0 or np.all(np.isnan(state.co2)):
        return jsonify({"error": "No CO2 waveform is available in this recording."}), 400

    idx = int(np.searchsorted(state.time, t_click))
    idx = max(0, min(idx, len(state.time) - 1))

    # Snap to the nearest valid CO2 sample if the clicked one was removed.
    if np.isnan(state.co2[idx]):
        valid = np.where(~np.isnan(state.co2))[0]
        if valid.size == 0:
            return jsonify({"error": "CO2 waveform has no valid samples."}), 400
        idx = int(valid[np.argmin(np.abs(valid - idx))])

    value = float(state.co2[idx])
    t_snap = float(state.time[idx])
    point = {"time": t_snap, "value": value}
    if which == "baseline":
        state.cvr_co2_baseline = point
    else:
        state.cvr_co2_hypercap = point
    state.log(f"CVR: {which} CO2 point = {value:.2f} mmHg at t={t_snap:.2f}")

    return jsonify({"which": which, "time": round(t_snap, 4), "value": round(value, 4)})


# ═══════════════════════════════════════════════════════════════════════
#  CVR – CLEAR
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/cvr/clear", methods=["POST"])
def api_cvr_clear():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    state.cvr_baseline = None
    state.cvr_hypercap = None
    state.cvr_co2_baseline = None
    state.cvr_co2_hypercap = None
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
        return jsonify({"error": "Both baseline and hypercapnia TCD selections are required."}), 400
    if state.cvr_co2_baseline is None or state.cvr_co2_hypercap is None:
        return jsonify({"error": "Select a baseline and a hypercapnia CO2 point on the CO2 waveform first."}), 400

    body = request.get_json() or {}
    vessel = body.get("vessel", "MCA")

    state.log("CVR: Beginning CVR calculation...")
    result = compute_cvr(
        state.cvr_baseline["mean"], state.cvr_baseline["env"],
        state.cvr_hypercap["mean"], state.cvr_hypercap["env"],
        state.cvr_co2_baseline["value"], state.cvr_co2_hypercap["value"],
    )

    if "error" in result:
        state.log(f"CVR: calculation failed — {result['error']}")
        return jsonify(result), 400

    state.log(f"CVR: CVR calculation complete. MCVR = {result['mcvr']}, WCVR = {result['wcvr']}")
    state.log(
        f"CVR: Delta CO2 (hypercapnia - baseline) = {result['delta_co2']} mmHg "
        f"(baseline={result['base_co2']}, hypercapnia={result['hyp_co2']})"
    )
    result["base_co2_time"] = round(state.cvr_co2_baseline["time"], 4)
    result["hyp_co2_time"] = round(state.cvr_co2_hypercap["time"], 4)
    state.cvr_result = result

    # Build export tables. The TCD windows carry meanU/envU only; the CO2
    # numbers are the two picked points, recorded in the summary.
    base_n = len(state.cvr_baseline["mean"])
    hyp_n = len(state.cvr_hypercap["mean"])
    export_data = {
        "baseline": {
            "Time_s": (np.arange(base_n) / SAMPLE_RATE).tolist(),
            "meanU": state.cvr_baseline["mean"].tolist(),
            "envU": state.cvr_baseline["env"].tolist(),
        },
        "hypercapnia": {
            "Time_s": (np.arange(hyp_n) / SAMPLE_RATE).tolist(),
            "meanU": state.cvr_hypercap["mean"].tolist(),
            "envU": state.cvr_hypercap["env"].tolist(),
        },
        "summary": result,
    }

    state.results["CVR"][vessel] = export_data
    state.log(f"CVR: Stored CVR result for {vessel} into unified sessionResults.")
    _cacvr_autosave()

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
    # Cadence and window are fixed by pump-beat physiology — see the constants
    # in pi_analysis. They are not accepted from the request on purpose.
    interval = pi_analysis.AUTO_INTERVAL_S
    pre = pi_analysis.AUTO_PRE_S
    post = pi_analysis.AUTO_POST_S

    centres = pi_analysis.auto_mark_points(start, x_min, x_max, interval)
    added = 0
    for centre in centres:
        t, a = pi_analysis.extract_span(state.time, state.env_u,
                                        centre - pre, centre + post)
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
        f"(−{pre}s/+{post}s around each peak) from t={start:.2f} to t={x_max:.2f}"
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
#  CA/CVR – SESSION/SPEED ACCUMULATION  (tag → move on → export once)
# ═══════════════════════════════════════════════════════════════════════

def _cacvr_selection_ranges():
    """Time bounds of the current CA/CVR selections, so a reloaded session can
    redraw its shaded windows without re-storing the sample arrays."""
    def bounds(sel):
        if not sel or "time" not in sel or len(sel["time"]) == 0:
            return None
        return {"start": float(sel["time"][0]), "end": float(sel["time"][-1])}
    ranges = {
        "ca": bounds(state.ca_selection),
        "cvr_baseline": bounds(state.cvr_baseline),
        "cvr_hypercapnia": bounds(state.cvr_hypercap),
        "cvr_co2_baseline": state.cvr_co2_baseline,
        "cvr_co2_hypercapnia": state.cvr_co2_hypercap,
    }
    return ranges


def _cacvr_result_summary(results):
    """Just the numbers the result boxes show — no big tables."""
    ca = {}
    cvr = {}
    for v in ("MCA", "PCA"):
        r = (results.get("CA") or {}).get(v)
        if r:
            ca[v] = {"final_mx": r.get("final_mx"), "mean_mfv": r.get("mean_mfv")}
        c = (results.get("CVR") or {}).get(v)
        if c:
            s = c.get("summary") or {}
            cvr[v] = {k: s.get(k) for k in (
                "mcvr", "wcvr", "base_mcbf", "hyp_mcbf", "base_wcbf", "hyp_wcbf",
                "base_co2", "hyp_co2", "delta_co2", "delta_mcbf", "delta_wcbf")}
    return {"ca": ca, "cvr": cvr}


def _cacvr_has_results(results=None):
    results = results if results is not None else state.results
    return any((results.get(a) or {}).get(v) for a in ("CA", "CVR") for v in ("MCA", "PCA"))


def _cacvr_payload():
    return {
        "label": state.cacvr_speed,
        "summary": _cacvr_result_summary(state.results),
        "selections": _cacvr_selection_ranges(),
        "patient_id": state.patient_id,
        "base_name": state.base_name,
    }


def _cacvr_autosave():
    """Persist the current session's CA/CVR results under the current label.

    Runs after every CA/CVR calculation, so building up a study is just:
    calculate for a label, then move on — the disk copy is always current and
    the final Export All simply gathers them.
    """
    if not state.base_name or not _cacvr_has_results():
        return None
    payload = {
        "base_name": state.base_name,
        "patient_id": state.patient_id,
        "label": state.cacvr_speed,
        "results": export_backend.serialise(state.results),
        "selections": _cacvr_selection_ranges(),
    }
    try:
        return cacvr_sessions.save_session(
            CACVR_SESSION_DIR, state.base_name, state.cacvr_speed, payload)
    except Exception as e:
        state.log(f"CA/CVR: WARNING — session auto-save failed: {e}")
        return None


@app.route("/api/cacvr/state")
def api_cacvr_state():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    return jsonify(_cacvr_payload())


@app.route("/api/cacvr/meta", methods=["POST"])
def api_cacvr_meta():
    """Set the current CA/CVR session/speed label."""
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    body = request.get_json() or {}
    if "label" in body:
        state.cacvr_speed = str(body["label"]).strip()
        # Re-tag any already-computed results under the new label.
        if _cacvr_has_results():
            _cacvr_autosave()
    return jsonify({"ok": True, "label": state.cacvr_speed})


@app.route("/api/cacvr/sessions")
def api_cacvr_sessions():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    return jsonify({"sessions": cacvr_sessions.list_sessions(CACVR_SESSION_DIR, state.base_name)})


@app.route("/api/cacvr/next_speed", methods=["POST"])
def api_cacvr_next_speed():
    """
    Finish this session/speed and start a clean one: the current results are
    already auto-saved, so just clear the working set and adopt the new label.
    Reports whether that label already has saved results (offer to load them).
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    body = request.get_json() or {}
    label = str(body.get("label", "")).strip()
    if not label:
        return jsonify({"error": "A session/speed label is required."}), 400

    _cacvr_autosave()   # make sure the outgoing label is persisted
    # Clear the working results and all selections.
    state.results = {"CA": {"MCA": None, "PCA": None}, "CVR": {"MCA": None, "PCA": None}}
    state.ca_selection = state.ca_result = None
    state.cvr_baseline = state.cvr_hypercap = state.cvr_co2_window = None
    state.cvr_peak_etco2 = state.cvr_peak_time = state.cvr_result = None
    state.cacvr_speed = label

    fname = cacvr_sessions.session_filename(state.base_name, label)
    existing = os.path.isfile(os.path.join(CACVR_SESSION_DIR, fname))
    state.log(f"CA/CVR: ready for session/speed {label!r}. Existing results: {existing}")
    return jsonify({**_cacvr_payload(), "existing_session": fname if existing else None})


@app.route("/api/cacvr/load_session", methods=["POST"])
def api_cacvr_load_session():
    """Restore a saved session's CA/CVR results (for review or re-export)."""
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    body = request.get_json() or {}
    fname = body.get("filename")
    if not fname:
        return jsonify({"error": "filename is required"}), 400
    try:
        data = cacvr_sessions.load_session(CACVR_SESSION_DIR, fname)
    except (OSError, ValueError) as e:
        return jsonify({"error": f"Could not load session: {e}"}), 400

    loaded = data.get("results") or {"CA": {"MCA": None, "PCA": None},
                                     "CVR": {"MCA": None, "PCA": None}}
    # Normalise shape so downstream code can assume all four slots exist.
    for a in ("CA", "CVR"):
        loaded.setdefault(a, {})
        for v in ("MCA", "PCA"):
            loaded[a].setdefault(v, None)
    state.results = loaded
    state.cacvr_speed = data.get("label", cacvr_sessions.label_from_filename(state.base_name, fname))
    state.log(f"CA/CVR: loaded session {fname} (label {state.cacvr_speed!r}).")
    return jsonify({**_cacvr_payload(), "loaded_selections": data.get("selections", {})})


@app.route("/api/cacvr/export_all", methods=["POST"])
def api_cacvr_export_all():
    """
    One-shot study export: master workbook (once) + one workbook per saved
    session/speed + a JSON progress file, bundled into a single zip. PI is
    exported separately and is not included here.
    """
    if state is None:
        return jsonify({"error": "No data loaded"}), 400

    _cacvr_autosave()   # fold in the current working session
    sessions = cacvr_sessions.load_all(CACVR_SESSION_DIR, state.base_name)
    if not sessions:
        return jsonify({"error": (
            "No CA/CVR results saved yet. Calculate MX or CVR for at least one "
            "session/speed first."
        )}), 400

    try:
        zip_name, manifest = export_all_cacvr(state, sessions, EXPORT_DIR)
    except Exception as e:
        import traceback
        print(f"CA/CVR export error:\n{traceback.format_exc()}")
        state.log(f"CA/CVR: export failed — {e}")
        return jsonify({"error": f"Export failed: {e}"}), 500

    labels = [s.get("label") or "(no label)" for s in sessions]
    state.log(
        f"CA/CVR: exported study bundle {zip_name} — master + "
        f"{len(sessions)} session workbook(s) ({', '.join(labels)}) + JSON."
    )
    return jsonify({"filename": zip_name, "manifest": manifest, "n_sessions": len(sessions)})


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
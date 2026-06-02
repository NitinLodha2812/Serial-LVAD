"""
app.py  –  Flask server for Serial LVAD Study
"""

import os, json, tempfile, shutil
import numpy as np
from flask import Flask, render_template, request, jsonify, send_from_directory
from backend.session_state import SessionState, SAMPLE_RATE, FIVE_MIN_SAMPLES, BASELINE_SAMPLES, HYPERCAP_SAMPLES
from backend.calculations import compute_mx, compute_cvr
from backend.export import save_excel, save_json

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "serial_lvad_uploads")
EXPORT_DIR = os.path.join(tempfile.gettempdir(), "serial_lvad_exports")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

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

    try:
        f.save(filepath)
    except Exception as e:
        return jsonify({"error": f"File save failed: {e}"}), 400

    state = SessionState()
    try:
        state.load_txt(filepath, f.filename)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Load error:\n{tb}")
        return jsonify({"error": f"Parse error: {e}"}), 400

    try:
        overview = state.get_overview_json()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Overview error:\n{tb}")
        return jsonify({"error": f"Data processing error: {e}"}), 400

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
    idx_end = min(idx_start + FIVE_MIN_SAMPLES - 1, len(state.time) - 1)

    if idx_end - idx_start + 1 < FIVE_MIN_SAMPLES:
        state.log("CA: Not enough samples; using available samples")

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
    idx_end = min(idx + BASELINE_SAMPLES - 1, len(state.time) - 1)
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
    idx_end = min(idx + HYPERCAP_SAMPLES - 1, len(state.time) - 1)
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
#  CVR – DETECT PEAK ETCO2
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/cvr/detect_peak", methods=["POST"])
def api_cvr_detect_peak():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    if state.cvr_hypercap is None:
        return jsonify({"error": "Hypercapnia selection required before detecting peak etCO2."}), 400

    co2 = state.cvr_hypercap["co2"]
    t = state.cvr_hypercap["time"]
    idx = int(np.nanargmax(co2))
    val = float(co2[idx])
    peak_time = float(t[idx])

    state.cvr_peak_etco2 = val
    state.cvr_peak_time = peak_time
    state.log(f"CVR: peak etCO2 detected = {val:.2f} at t={peak_time:.2f}")

    return jsonify({"peak_value": round(val, 4), "peak_time": round(peak_time, 4)})


# ═══════════════════════════════════════════════════════════════════════
#  CVR – CLEAR
# ═══════════════════════════════════════════════════════════════════════

@app.route("/api/cvr/clear", methods=["POST"])
def api_cvr_clear():
    if state is None:
        return jsonify({"error": "No data loaded"}), 400
    state.cvr_baseline = None
    state.cvr_hypercap = None
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

    body = request.get_json() or {}
    fmt = body.get("format", "excel")

    if fmt == "json":
        fname = save_json(state, EXPORT_DIR)
        state.log(f"Saved JSON: {fname}")
    else:
        fname = save_excel(state, EXPORT_DIR)
        state.log(f"Saved Excel: {fname}")

    return jsonify({"filename": fname})


@app.route("/api/download/<filename>")
def api_download(filename):
    return send_from_directory(EXPORT_DIR, filename, as_attachment=True)


# ═══════════════════════════════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(debug=True, port=5000)
"""
pi_analysis.py
Port of the MATLAB `PI.m` epoch-selector GUI.

The MATLAB tool plots the TCD envelope (column 5, "1-1 Env U"), lets the
operator brush individual beats as **Native** (heart-driven) or **Artificial**
(pump-driven) epochs, and derives a pulsatility index per epoch:

    PI = (max - min) / mean          PW = t_last - t_first

Selections auto-save to a per-speed `.mat` file so an operator can step through
several pump speeds in one sitting and reload any of them later. Here the same
selections persist as JSON, keyed by (recording base name, session/speed).
"""

import glob
import json
import os
import re
import numpy as np


# Auto-select defaults, straight from PI.m: a mark every 2 s, each epoch
# spanning ±0.2 s around the mark.
AUTO_INTERVAL_S = 2.0
AUTO_HALF_WIDTH_S = 0.2

NATIVE = "native"
ARTIFICIAL = "artificial"


# ═══════════════════════════════════════════════════════════════════════
#  EPOCH CONSTRUCTION
# ═══════════════════════════════════════════════════════════════════════

def _safe_float(v):
    """NaN/Inf → None so the value survives strict JSON encoding."""
    f = float(v)
    return None if (np.isnan(f) or np.isinf(f)) else f


def epoch_metrics(times: np.ndarray, amps: np.ndarray) -> dict:
    """
    Per-epoch statistics, matching PI.m's addNewData:

        Hi   = max(amp)      Lo   = min(amp)      Mean = mean(amp)
        PW   = time(end) - time(1)
        PI   = (Hi - Lo) / Mean

    A zero mean would make PI infinite; that is reported as None rather than
    Inf so it lands in the spreadsheet as a blank instead of a bogus number.
    """
    hi = float(np.max(amps))
    lo = float(np.min(amps))
    mu = float(np.mean(amps))
    pw = float(times[-1] - times[0])
    pi = (hi - lo) / mu if mu != 0 else np.nan
    return {
        "max": _safe_float(hi),
        "min": _safe_float(lo),
        "mean": _safe_float(mu),
        "pw": _safe_float(pw),
        "pi": _safe_float(pi),
    }


def make_epoch(epoch_id: int, kind: str, times: np.ndarray, amps: np.ndarray) -> dict:
    """Bundle a selected span of samples into a serialisable epoch record."""
    order = np.argsort(times, kind="stable")
    times = np.asarray(times, dtype=np.float64)[order]
    amps = np.asarray(amps, dtype=np.float64)[order]
    return {
        "id": epoch_id,
        "type": kind,
        "t_start": float(times[0]),
        "t_end": float(times[-1]),
        "n_samples": int(len(times)),
        "time": [round(float(t), 4) for t in times],
        "amp": [round(float(a), 4) for a in amps],
        **epoch_metrics(times, amps),
    }


def extract_rect(time: np.ndarray, env: np.ndarray, rect: dict):
    """
    Samples inside a brush rectangle, in both time and amplitude — the same
    set MATLAB's `brush` would hand back via BrushData.

    NaN samples (points the operator previously removed on the CA/CVR tabs)
    are never plotted, so they can't be brushed; they are excluded here too.
    """
    x_min, x_max = sorted((float(rect["x_min"]), float(rect["x_max"])))
    y_min, y_max = sorted((float(rect["y_min"]), float(rect["y_max"])))
    mask = (
        (time >= x_min) & (time <= x_max)
        & (env >= y_min) & (env <= y_max)
        & ~np.isnan(env)
    )
    return time[mask], env[mask]


def extract_span(time: np.ndarray, env: np.ndarray, t0: float, t1: float):
    """Samples in a time span, ignoring amplitude (used by auto-select)."""
    mask = (time >= t0) & (time <= t1) & ~np.isnan(env)
    return time[mask], env[mask]


def auto_mark_points(start: float, x_min: float, x_max: float,
                     interval: float = AUTO_INTERVAL_S) -> np.ndarray:
    """
    Mark centres for auto-select, mirroring PI.m:

        numMarks   = floor((xMax - x) / markInterval)
        markPoints = x + (0:numMarks) * markInterval
        markPoints = markPoints(markPoints >= xMin & markPoints <= xMax)

    Walks forward from the clicked point to the right edge of the current
    view, so the operator zooms to the stretch they care about first.
    """
    if interval <= 0:
        return np.array([])
    num_marks = int(np.floor((x_max - start) / interval))
    if num_marks < 0:
        return np.array([])
    pts = start + np.arange(num_marks + 1) * interval
    return pts[(pts >= x_min) & (pts <= x_max)]


# ═══════════════════════════════════════════════════════════════════════
#  SUMMARY
# ═══════════════════════════════════════════════════════════════════════

def _mean_of(values):
    vals = [v for v in values if v is not None]
    return round(float(np.mean(vals)), 4) if vals else None


def summarize(epochs: list) -> dict:
    """Counts and mean PI per class — what the GUI's counter displays."""
    nat = [e for e in epochs if e["type"] == NATIVE]
    art = [e for e in epochs if e["type"] == ARTIFICIAL]
    return {
        "n_native": len(nat),
        "n_artificial": len(art),
        "mean_pi_native": _mean_of(e["pi"] for e in nat),
        "mean_pi_artificial": _mean_of(e["pi"] for e in art),
    }


def numbered(epochs: list) -> list:
    """
    Attach the 1-based per-class ordinal used in labels and column names
    (Native #1, NatPI_1, …). Insertion order defines the numbering.
    """
    counters = {NATIVE: 0, ARTIFICIAL: 0}
    out = []
    for e in epochs:
        counters[e["type"]] += 1
        out.append({**e, "ordinal": counters[e["type"]]})
    return out


# ═══════════════════════════════════════════════════════════════════════
#  EXCEL ROW
# ═══════════════════════════════════════════════════════════════════════

def _maybe_number(s):
    """
    PI.m calls str2double() on the three ID fields, turning anything
    non-numeric ("VAD056", "MCA") into NaN. Keep the text instead — a blank
    Patient ID column helps nobody — but still emit real numbers as numbers
    so the sheet stays sortable.
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return s


def build_excel_row(epochs: list, patient_id: str, session_speed: str, vessel: str) -> dict:
    """
    One spreadsheet row: the three IDs, then every native epoch's five
    metrics, then every artificial epoch's — the column layout PI.m writes
    into the "PI Demographics" sheet.
    """
    row = {
        "PatientID": _maybe_number(patient_id),
        "SessionSpeed": _maybe_number(session_speed),
        "Vessel": _maybe_number(vessel),
    }
    prefixes = {NATIVE: "Nat", ARTIFICIAL: "Art"}
    for kind in (NATIVE, ARTIFICIAL):
        p = prefixes[kind]
        items = [e for e in numbered(epochs) if e["type"] == kind]
        for e in items:
            i = e["ordinal"]
            row[f"{p}Hi_{i}"] = e["max"]
            row[f"{p}Lo_{i}"] = e["min"]
            row[f"{p}Mean_{i}"] = e["mean"]
            row[f"{p}PW_{i}"] = e["pw"]
            row[f"{p}PI_{i}"] = e["pi"]
    return row


# ═══════════════════════════════════════════════════════════════════════
#  PER-SPEED SESSION PERSISTENCE  (the .mat auto-save, as JSON)
# ═══════════════════════════════════════════════════════════════════════

def _safe(part: str) -> str:
    return re.sub(r"[^\w.\-]", "_", (part or "").strip())


def session_filename(base_name: str, speed: str) -> str:
    speed = _safe(speed)
    stem = _safe(base_name) or "recording"
    return f"{stem}__speed{speed}.json" if speed else f"{stem}__nospeed.json"


def speed_from_filename(base_name: str, filename: str) -> str:
    """Recover the speed label a session file was saved under."""
    stem = filename[:-5] if filename.endswith(".json") else filename
    prefix = f"{_safe(base_name)}__"
    if stem.startswith(prefix):
        stem = stem[len(prefix):]
    if stem == "nospeed":
        return ""
    return stem[5:] if stem.startswith("speed") else stem


def save_session(session_dir: str, base_name: str, speed: str, payload: dict) -> str:
    """Write selections for one speed. Called after every mutation (auto-save)."""
    os.makedirs(session_dir, exist_ok=True)
    fname = session_filename(base_name, speed)
    with open(os.path.join(session_dir, fname), "w") as f:
        json.dump(payload, f, indent=2, allow_nan=False)
    return fname


def load_session(session_dir: str, filename: str) -> dict:
    with open(os.path.join(session_dir, _safe(filename))) as f:
        return json.load(f)


def list_sessions(session_dir: str, base_name: str) -> list:
    """
    Every saved speed for this recording, newest-name-last. Each entry carries
    the speed label and epoch counts so the UI can label the picker without
    re-reading each file.
    """
    if not os.path.isdir(session_dir):
        return []
    pattern = os.path.join(session_dir, f"{_safe(base_name)}__*.json")
    out = []
    for path in sorted(glob.glob(pattern)):
        fname = os.path.basename(path)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        epochs = data.get("epochs", [])
        out.append({
            "filename": fname,
            "speed": data.get("speed", speed_from_filename(base_name, fname)),
            "vessel": data.get("vessel", ""),
            "n_native": sum(1 for e in epochs if e["type"] == NATIVE),
            "n_artificial": sum(1 for e in epochs if e["type"] == ARTIFICIAL),
        })
    return out

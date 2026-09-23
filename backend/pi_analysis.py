"""
pi_analysis.py
Port of the MATLAB `PI.m` epoch-selector GUI.

The MATLAB tool plots the TCD envelope (column 5, "1-1 Env U"), lets the
operator brush individual beats as **Native** (heart-driven) or **Artificial**
(pump-driven) epochs, and derives a pulsatility index per epoch:

    PI = (max - min) / mean          PW = t_last - t_first

PI beats now ride on the main-screen tag (Vessel for Serial LVAD, Speed for
RAMPs) alongside CA and CVR: they are saved, loaded, and exported with them,
so PI has no separate persistence of its own.
"""

import numpy as np


# Auto-select artificial epochs: a mark every 2 s. The epoch window around each
# mark is asymmetric because an artificial (pump) beat is not symmetric about
# its peak — the deceleration limb runs ~0.15 s before the peak and the
# acceleration limb ~0.20 s after it (0.35 s total). These are fixed by the
# beat physiology and are intentionally NOT user-tunable.
AUTO_INTERVAL_S = 2.0
AUTO_PRE_S = 0.15    # seconds captured BEFORE the peak (deceleration)
AUTO_POST_S = 0.20   # seconds captured AFTER the peak (acceleration)

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
    Per-epoch statistics:

        Hi       = max(amp)              Lo = min(amp)
        Mean     = (1/3)*Hi + (2/3)*Lo   (weighted, not the sample mean)
        PulseAmp = Hi - Lo
        PI       = PulseAmp / Mean

    Mean is the weighted estimate the group uses (one third of the peak plus two
    thirds of the trough). A zero mean would make PI infinite; that is reported
    as None so it lands in the sheet as a blank rather than a bogus number.
    `times` still fixes the epoch's span, but pulse width is no longer reported.
    """
    hi = float(np.max(amps))
    lo = float(np.min(amps))
    mu = (1.0 / 3.0) * hi + (2.0 / 3.0) * lo
    pulse_amp = hi - lo
    pi = pulse_amp / mu if mu != 0 else np.nan
    return {
        "max": _safe_float(hi),
        "min": _safe_float(lo),
        "mean": _safe_float(mu),
        "pulse_amp": _safe_float(pulse_amp),
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
#  ABP EPOCHS (TCD → ABP synchronisation)
# ═══════════════════════════════════════════════════════════════════════

def abp_window_metrics(time: np.ndarray, abp: np.ndarray,
                       t_start: float, t_end: float, shift: float):
    """
    ABP metrics over the beat window [t_start, t_end] in TCD time, read off the
    ABP tracing shifted by `shift` (ABP sample originally at t is aligned to
    t + shift, so the samples we want sit at original times [t_start - shift,
    t_end - shift]). Returns None if no valid ABP samples fall in the window.
    """
    if abp is None or len(abp) == 0:
        return None
    o0, o1 = t_start - shift, t_end - shift
    mask = (time >= o0) & (time <= o1) & ~np.isnan(abp)
    if not mask.any():
        return None
    amps = abp[mask]
    return {"n_samples": int(mask.sum()), **epoch_metrics(time[mask], amps)}


def abp_epochs_for(epochs: list, time: np.ndarray, abp: np.ndarray, shift: float) -> list:
    """One ABP epoch per TCD epoch (same beat, shifted ABP), numbered like the
    TCD table so the two tables line up row for row."""
    out = []
    for e in numbered(epochs):
        m = abp_window_metrics(time, abp, e["t_start"], e["t_end"], shift)
        rec = {"id": e["id"], "type": e["type"], "ordinal": e["ordinal"],
               "t_start": e["t_start"], "t_end": e["t_end"]}
        rec.update(m if m else {
            "n_samples": 0, "max": None, "min": None,
            "mean": None, "pulse_amp": None, "pi": None})
        out.append(rec)
    return out

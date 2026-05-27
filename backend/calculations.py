"""
calculations.py
Exact port of the MATLAB CA (MX) and CVR calculations.
"""

import numpy as np
from .session_state import SAMPLE_RATE, FIVE_MIN_SAMPLES, BASELINE_SAMPLES, HYPERCAP_SAMPLES


# ═══════════════════════════════════════════════════════════════════════
#  CA – MX INDEX
# ═══════════════════════════════════════════════════════════════════════

def compute_mx(abp: np.ndarray, env: np.ndarray):
    """
    Compute MX index from selected ABP and envU vectors.

    Steps (from MATLAB CAwindow_v10 'calculate'):
      1. Non-overlapping 3-second average (375 samples) → MAP and MFV
         - window must have ≥50 % valid data, else NaN
      2. Pearson correlation in sliding windows of 21 averaged-values,
         stepping by 20 → one coefficient per ~minute
      3. Final MX = mean of those correlation coefficients

    Returns dict with all intermediate and final values.
    """
    MAP_WINDOW = 375          # 3 s at 125 Hz
    CORR_WINDOW = 21          # 21 averaged values ≈ 63 s
    SHIFT_INTERVAL = 20       # step by 20 averaged values ≈ 60 s

    # --- Step 1: non-overlapping 3-s averages ---
    num_windows = len(abp) // MAP_WINDOW
    calc_map = np.full(num_windows, np.nan)
    calc_mfv = np.full(num_windows, np.nan)

    for i in range(num_windows):
        s = i * MAP_WINDOW
        e = s + MAP_WINDOW
        win_abp = abp[s:e]
        win_env = env[s:e]
        if np.count_nonzero(~np.isnan(win_abp)) >= 0.5 * MAP_WINDOW:
            calc_map[i] = np.nanmean(win_abp)
        if np.count_nonzero(~np.isnan(win_env)) >= 0.5 * MAP_WINDOW:
            calc_mfv[i] = np.nanmean(win_env)

    # --- Step 2: sliding Pearson correlation ---
    corr_coeffs = []
    if len(calc_map) >= CORR_WINDOW and len(calc_mfv) >= CORR_WINDOW:
        num_shifts = (len(calc_map) - CORR_WINDOW) // SHIFT_INTERVAL + 1
        for i in range(num_shifts):
            s = i * SHIFT_INTERVAL
            e = s + CORR_WINDOW
            seg_map = calc_map[s:e]
            seg_mfv = calc_mfv[s:e]
            valid_map = np.count_nonzero(~np.isnan(seg_map))
            valid_mfv = np.count_nonzero(~np.isnan(seg_mfv))
            if valid_map >= 0.5 * CORR_WINDOW and valid_mfv >= 0.5 * CORR_WINDOW:
                # Pearson on complete rows only (matching MATLAB 'rows','complete')
                mask = ~np.isnan(seg_map) & ~np.isnan(seg_mfv)
                if mask.sum() >= 2:
                    r = np.corrcoef(seg_map[mask], seg_mfv[mask])[0, 1]
                    corr_coeffs.append(abs(r))
                else:
                    corr_coeffs.append(np.nan)
            else:
                corr_coeffs.append(np.nan)
    else:
        return {"error": "Not enough data to compute correlation window."}

    corr_coeffs = np.array(corr_coeffs)
    final_mx = float(np.nanmean(corr_coeffs)) if len(corr_coeffs) > 0 else np.nan

    return {
        "MAP": calc_map.tolist(),
        "MFV": calc_mfv.tolist(),
        "corr_coeffs": corr_coeffs.tolist(),
        "final_mx": round(final_mx, 6),
    }


# ═══════════════════════════════════════════════════════════════════════
#  CVR – CEREBROVASCULAR REACTIVITY
# ═══════════════════════════════════════════════════════════════════════

def compute_cvr(base_mean, base_env, base_co2,
                hyp_mean, hyp_env, hyp_co2,
                peak_co2=None):
    """
    Compute CVR from baseline and hypercapnic selections.

    Averages of selected windows → deltas → MCVR & WCVR
    If peak_co2 is supplied it overrides the hypercapnic mean CO2.
    """
    base_mcbf = float(np.nanmean(base_mean))
    base_wcbf = float(np.nanmean(base_env))
    base_co2_v = float(np.nanmean(base_co2))

    hyp_mcbf = float(np.nanmean(hyp_mean))
    hyp_wcbf = float(np.nanmean(hyp_env))
    hyp_co2_v = float(peak_co2) if peak_co2 is not None else float(np.nanmean(hyp_co2))

    delta_mcbf = hyp_mcbf - base_mcbf
    delta_wcbf = hyp_wcbf - base_wcbf
    delta_co2 = hyp_co2_v - base_co2_v

    if delta_co2 == 0 or np.isnan(delta_co2):
        return {"error": "Delta CO2 is zero or NaN, cannot compute CVR."}

    mcvr = (delta_mcbf / base_mcbf) * (100.0 / delta_co2)
    wcvr = (delta_wcbf / base_wcbf) * (100.0 / delta_co2)

    return {
        "base_mcbf": round(base_mcbf, 4),
        "base_wcbf": round(base_wcbf, 4),
        "base_co2":  round(base_co2_v, 4),
        "hyp_mcbf":  round(hyp_mcbf, 4),
        "hyp_wcbf":  round(hyp_wcbf, 4),
        "hyp_co2":   round(hyp_co2_v, 4),
        "delta_mcbf": round(delta_mcbf, 4),
        "delta_wcbf": round(delta_wcbf, 4),
        "delta_co2":  round(delta_co2, 4),
        "mcvr": round(mcvr, 6),
        "wcvr": round(wcvr, 6),
    }

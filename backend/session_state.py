"""
session_state.py
Centralised session data container — mirrors the MATLAB setappdata/getappdata
pattern.  One global instance holds everything: raw data, parsed columns,
marks, CA/CVR selections and results.
"""

import numpy as np
import re
from datetime import datetime


SAMPLE_RATE = 125
FIVE_MIN_SAMPLES = 5 * 60 * SAMPLE_RATE   # 37 500
BASELINE_SAMPLES = 4000                     # ~32 s
HYPERCAP_SAMPLES = 1250                     # ~10 s


# ── Column-name aliases ────────────────────────────────────────────────
# Each signal lists normalized substring patterns to look for in the header.
# Headers are normalized to alphanumeric-lowercase (e.g. "1-1 Env U" → "11envu",
# "ETCO2(mmHg)" → "etco2mmhg") before matching. Patterns are tried in order, so
# the *first* (most specific) match wins — e.g. "fiabp" is preferred over a bare
# "abp" fallback that might otherwise match "reABP".
COLUMN_ALIASES = {
    "mean_u": ["meanu"],                      # matches "1-1 Mean U", "Mean U", "meanU"
    "env_u":  ["envu"],                       # matches "1-1 Env U", "Env U", "envU"
    "etco2":  ["etco2"],                      # matches "ETCO2(mmHg)", "ETCO2" (not "CO2")
    "co2":    ["co2"],                        # raw CO2 waveform — see COLUMN_EXCLUSIONS
    # ABP source: prefer fiABP (standard BP device); if that column is missing
    # or all-zero (no signal recorded), fall back to A-LINE (bedside arterial
    # line); finally accept any *abp* column. Data presence is checked at
    # resolution time so we never silently latch onto an empty channel.
    "abp":    ["fiabp", "aline", "abp"],
    "mark":   ["mark"],                       # marks/event column
}

# Substrings that disqualify a column from matching a given signal.
# The raw CO2 waveform column is named like "CO2(mmHg)", which would also
# match a bare "co2" pattern against "ETCO2(mmHg)". Excluding "etco2" here
# guarantees the waveform resolver picks the right column.
COLUMN_EXCLUSIONS = {
    "co2": ["etco2"],
}

# Fallback positions used only if a column cannot be located by name.
COLUMN_FALLBACK_POS = {
    "mean_u": 1,
    "env_u":  4,
    "etco2":  6,
    "abp":    8,
}

# Minimum fraction of the recording a candidate column must cover with finite,
# non-zero samples to count as a usable data source. This guards the ABP source
# selection against a channel (e.g. fiABP) that carries a few valid values at
# the very start of a session and then flat-lines to zero: such a column passes
# a naive "has any data" test but is not a usable signal, so resolution should
# fall through to the next candidate (A-LINE). Tune here if a legitimately
# sparse signal is being rejected.
MIN_DATA_COVERAGE = 0.5


def _normalize_header(s) -> str:
    """Lowercase and strip non-alphanumeric characters from a header string."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def window_end_for_valid_count(valid_mask: np.ndarray, start_idx: int, target_valid: int):
    """
    Find the inclusive end index of the smallest window starting at `start_idx`
    that contains `target_valid` valid samples, extending *past* removed/NaN
    samples to compensate for "bad" data that was brushed out.

    `valid_mask` is a boolean array (True = usable sample) over the full record.
    When the selection has no removed data this returns exactly
    `start_idx + target_valid - 1` (the original fixed-length behaviour). When
    samples inside the span were removed, the end is pushed out until the
    target number of valid samples has been collected.

    Returns (end_idx, valid_in_window, exhausted):
      end_idx        inclusive end index of the window (capped at the last sample)
      valid_in_window number of valid samples actually captured
      exhausted      True if the record ran out before the target was reached
    """
    n = len(valid_mask)
    if n == 0 or start_idx >= n:
        return max(n - 1, 0), 0, True
    if target_valid <= 0:
        return start_idx, int(bool(valid_mask[start_idx])), False

    cumsum = np.cumsum(valid_mask[start_idx:].astype(np.int64))
    total_valid = int(cumsum[-1])
    if total_valid < target_valid:
        # Not enough usable data left even using every remaining sample.
        return n - 1, total_valid, True

    # First offset where the cumulative valid count reaches the target.
    hit = int(np.searchsorted(cumsum, target_valid, side="left"))
    return start_idx + hit, target_valid, False


def find_column(headers, signal: str):
    """
    Return the index of the column matching `signal`, or None if not found.
    Tries each alias substring in order against normalized headers.
    Name-only (no data-presence check) — used for the marks column.
    """
    norm_headers = [_normalize_header(h) for h in headers]
    for pattern in COLUMN_ALIASES.get(signal, []):
        for i, nh in enumerate(norm_headers):
            if pattern in nh:
                return i
    return None


def _column_coverage(arr: np.ndarray) -> float:
    """Fraction of `arr` that is finite and non-zero (0.0 .. 1.0).

    Used to rank candidate data sources: a column that only carries data for a
    brief stretch (e.g. an fiABP trace that zeros out after the first few
    samples) has low coverage and should lose to a fuller alternative source.
    """
    if arr is None or len(arr) == 0:
        return 0.0
    nonzero_valid = ~np.isnan(arr) & (np.abs(arr) > 1e-12)
    return float(nonzero_valid.sum()) / float(len(arr))


def _column_has_data(arr: np.ndarray) -> bool:
    """True if `arr` has at least one finite, non-zero value."""
    return _column_coverage(arr) > 0.0


def find_data_column(df, signal: str):
    """
    Resolve `signal` to a column that both (a) matches an alias by name and
    (b) actually contains data (some non-zero, non-NaN sample).

    Patterns are tried in order; within each pattern, every matching column is
    inspected for data. A list of empty-but-name-matched headers is also
    returned so the caller can explain what was skipped (e.g. "fiABP was
    empty — used A-LINE instead").

    Returns: (index, header, skipped_empty_headers)
             (None, None, skipped_empty_headers) if nothing usable was found.
    """
    import pandas as pd
    headers = list(df.columns)
    norm = [_normalize_header(h) for h in headers]
    exclusions = COLUMN_EXCLUSIONS.get(signal, [])
    skipped = []
    best_partial = None  # (coverage, i, header) — best below-threshold candidate
    for pattern in COLUMN_ALIASES.get(signal, []):
        for i, nh in enumerate(norm):
            if pattern not in nh:
                continue
            if any(ex in nh for ex in exclusions):
                continue   # e.g. don't pick ETCO2 when looking for raw CO2
            arr = pd.to_numeric(df.iloc[:, i], errors="coerce").to_numpy(dtype=np.float64)
            cov = _column_coverage(arr)
            if cov >= MIN_DATA_COVERAGE:
                # Adequately-covered match: honour alias priority order and take
                # the first one (e.g. fiABP wins over A-LINE when both are good).
                return i, headers[i], skipped
            # Not enough coverage to be the preferred source. Remember it as a
            # last-resort fallback (keep the fullest one) and keep looking for a
            # better candidate further down the alias list.
            if cov > 0.0 and (best_partial is None or cov > best_partial[0]):
                best_partial = (cov, i, headers[i])
            if headers[i] not in skipped:
                skipped.append(headers[i])
    # No candidate cleared the coverage bar. Rather than drop the signal, fall
    # back to the fullest partial source so a uniformly-sparse-but-real channel
    # still resolves; don't report that one as skipped.
    if best_partial is not None:
        _, i, hdr = best_partial
        skipped = [s for s in skipped if s != hdr]
        return i, hdr, skipped
    return None, None, skipped


class SessionState:
    """Holds all state for a single loaded file."""

    def __init__(self):
        # metadata
        self.patient_id: str = ""
        self.session: str = ""
        self.raw_path: str = ""
        self.base_name: str = ""      # source filename without extension

        # raw table (list of dicts kept for master-sheet export)
        self.raw_headers: list = []
        self.raw_rows: list = []          # list[list] – preserves original file

        # column-name → index map resolved at load time
        self.column_positions: dict = {}  # e.g. {"mean_u": 1, "env_u": 4, ...}

        # parsed column vectors (numpy float64)
        self.time: np.ndarray = np.array([])
        self.mean_u: np.ndarray = np.array([])
        self.env_u: np.ndarray = np.array([])
        self.abp: np.ndarray = np.array([])
        self.etco2: np.ndarray = np.array([])
        self.co2: np.ndarray = np.array([])   # raw CO2 waveform (per-sample)

        # marks
        self.marks_labels: list = []
        self.marks_times: list = []       # float seconds

        # CA state
        self.ca_selection: dict | None = None   # {time, abp, env}
        self.ca_result: dict | None = None      # {MAP, MFV, corr, finalMX, table}

        # CVR state. Baseline/hypercapnia windows now cover the TCD signals
        # (meanU/envU) only — CO2 is no longer averaged over a window.
        self.cvr_baseline: dict | None = None   # {time, mean, env}
        self.cvr_hypercap: dict | None = None   # {time, mean, env}
        # True end-tidal CO2 is picked by hand off the CO2 waveform: one point
        # for baseline, one for hypercapnia. Each is {"time": s, "value": mmHg}.
        self.cvr_co2_baseline: dict | None = None
        self.cvr_co2_hypercap: dict | None = None
        self.cvr_result: dict | None = None

        # PI state — brushed beat epochs, in insertion order. Each carries its
        # own id so undo/deselect never depend on list positions.
        self.pi_epochs: list = []
        self.pi_next_id: int = 1
        self.pi_speed: str = ""       # "Session/Speed" field on the PI tab
        self.pi_vessel: str = ""

        # CA/CVR session accumulation. Results are tagged with a session/speed
        # label and auto-saved per label to disk, so a whole study is built up
        # across labels and exported once at the end (one master + one workbook
        # per label + one JSON) instead of re-writing the huge master each time.
        self.cacvr_speed: str = ""

        # unified results (per vessel)
        self.results = {
            "CA":  {"MCA": None, "PCA": None},
            "CVR": {"MCA": None, "PCA": None},
        }

        # log
        self.log_lines: list = []

    # ------------------------------------------------------------------ helpers
    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self.log_lines.append(line)
        print(line)

    # ------------------------------------------------------------------ load
    def load_txt(self, filepath: str, filename: str):
        """Parse a comma-delimited TXT/CSV — uses pandas for speed."""
        import pandas as pd

        self.raw_path = filepath

        df = pd.read_csv(filepath, encoding="utf-8-sig")
        self.raw_headers = list(df.columns)
        self.raw_rows = df.values.tolist()

        n = len(df)

        # build time vector from column 0
        time_col = df.iloc[:, 0].astype(str)
        time_vec = np.zeros(n, dtype=np.float64)
        try:
            for i, raw_t in enumerate(time_col):
                raw_t = raw_t.strip()
                parts = raw_t.split()
                hms = parts[0]
                millis = int(parts[1]) if len(parts) > 1 else 0
                h, m, s = [int(x) for x in hms.split(":")]
                time_vec[i] = h * 3600 + m * 60 + s + millis / 1000.0
        except Exception:
            time_vec = np.arange(n, dtype=np.float64) / SAMPLE_RATE

        time_vec = time_vec - time_vec[0]
        self.time = time_vec

        def safe_col(idx):
            if idx is None or idx >= len(df.columns):
                return np.full(n, np.nan)
            return pd.to_numeric(df.iloc[:, idx], errors='coerce').to_numpy(dtype=np.float64)

        # Resolve each signal by *column name and data presence*. The variable
        # order in the export can change between recordings, and for some
        # signals (notably ABP) a column may be present but empty — e.g. when
        # the bedside A-LINE was used instead of our fiABP device, the fiABP
        # column is all zeros. find_data_column() walks the alias list and
        # picks the first column that both matches by name AND has real data.
        resolved = {}
        for signal in ("mean_u", "env_u", "etco2", "co2", "abp"):
            idx, hdr, skipped = find_data_column(df, signal)
            if idx is not None:
                if skipped:
                    self.log(
                        f"Matched '{signal}' → column {idx} ({hdr!r}). "
                        f"Fell back from empty/low-data: {', '.join(repr(s) for s in skipped)}."
                    )
                else:
                    self.log(f"Matched '{signal}' → column {idx} ({hdr!r}).")
            else:
                # No name match with data. Decide between "all candidates empty"
                # (worth a loud warning) vs "no name match at all" (use the old
                # positional fallback so existing layouts still work).
                fb = COLUMN_FALLBACK_POS.get(signal)
                if skipped:
                    self.log(
                        f"WARNING: '{signal}' source unavailable — every candidate "
                        f"({', '.join(repr(s) for s in skipped)}) is empty, all-zero, "
                        f"or below the {MIN_DATA_COVERAGE:.0%} usable-data threshold. "
                        f"Calculations using {signal} will produce NaN."
                    )
                    # Keep the first empty match as the resolved column so the
                    # Excel Master sheet still has the right column populated.
                    idx = find_column(self.raw_headers, signal)
                elif fb is not None and fb < len(self.raw_headers):
                    self.log(
                        f"WARNING: '{signal}' column not found by name — "
                        f"falling back to position {fb} ({self.raw_headers[fb]!r})."
                    )
                    idx = fb
                else:
                    self.log(f"WARNING: '{signal}' column not found — using NaN.")
            resolved[signal] = idx

        self.column_positions = resolved
        self.mean_u = safe_col(resolved["mean_u"])
        self.env_u  = safe_col(resolved["env_u"])
        self.etco2  = safe_col(resolved["etco2"])
        self.co2    = safe_col(resolved.get("co2"))
        self.abp    = safe_col(resolved["abp"])

        # parse marks – locate by name ("mark"), else fall back to last column
        mark_col_idx = find_column(self.raw_headers, "mark")
        if mark_col_idx is None:
            mark_col_idx = len(self.raw_headers) - 1
            self.log(
                f"Marks column not found by name — using last column "
                f"({self.raw_headers[mark_col_idx]!r})."
            )
        else:
            self.log(f"Matched 'mark' → column {mark_col_idx} ({self.raw_headers[mark_col_idx]!r}).")

        self.marks_labels = []
        self.marks_times = []
        mark_col = df.iloc[:, mark_col_idx].astype(str)
        for i, val in enumerate(mark_col):
            val = val.strip()
            if val and val != "-" and val != "nan":
                self.marks_labels.append(val)
                self.marks_times.append(float(self.time[i]))

        if not self.marks_labels:
            self.marks_labels = ["No marks found"]
            self.marks_times = [float("nan")]

        # auto-fill patient / session from filename
        name_only = filename.rsplit(".", 1)[0] if "." in filename else filename
        self.base_name = name_only
        m = re.search(r"[Ll]?VAD0?(\d{2,3})", name_only)
        self.patient_id = f"VAD0{m.group(1)}" if m else "unknownPID"
        m2 = re.search(r"[Ss]ession\s*#?\s*(\d+)", name_only, re.IGNORECASE)
        if not m2:
            m2 = re.search(r"SESSION[_ ]*(\d+)", name_only, re.IGNORECASE)
        self.session = f"Session{m2.group(1)}" if m2 else "s0"

        self.log(f"Loaded {filename} — {n} rows, Patient={self.patient_id}, Session={self.session}")

    # ----- helpers to serialise numpy for JSON -----
    @staticmethod
    def _to_list(arr, decimate=1):
        """Convert numpy array to JSON-safe list, optionally decimating."""
        if arr is None or len(arr) == 0:
            return []
        if decimate > 1:
            arr = arr[::decimate]
        # vectorized round, then convert NaN to None
        rounded = np.round(arr, 2)
        return [None if np.isnan(v) else float(v) for v in rounded]

    def get_overview_json(self, decimate=50):
        """Return decimated data for initial overview plots."""
        # Surface the load-time log lines (column matches, fallbacks, warnings)
        # so the frontend can show them in the Activity Log immediately. The
        # client picks out lines tagged WARNING / Matched / Fell back.
        return {
            "time":   self._to_list(self.time, decimate),
            "env_u":  self._to_list(self.env_u, decimate),
            "abp":    self._to_list(self.abp, decimate),
            "mean_u": self._to_list(self.mean_u, decimate),
            "etco2":  self._to_list(self.etco2, decimate),
            "co2":    self._to_list(self.co2, decimate),
            "marks_labels": self.marks_labels,
            "marks_times":  [round(t, 4) if not np.isnan(t) else None for t in self.marks_times],
            "patient_id": self.patient_id,
            "session": self.session,
            "base_name": self.base_name,
            "duration_s": float(self.time[-1]) if len(self.time) else 0.0,
            "total_samples": len(self.time),
            "sample_rate": SAMPLE_RATE,
            "load_log": list(self.log_lines),
            "abp_source": (self.raw_headers[self.column_positions["abp"]]
                           if self.column_positions.get("abp") is not None
                           else None),
        }

    def get_range_json(self, start_sec: float, end_sec: float):
        """Return full-resolution data for a zoomed range."""
        mask = (self.time >= start_sec) & (self.time <= end_sec)
        return {
            "time":   self._to_list(self.time[mask]),
            "env_u":  self._to_list(self.env_u[mask]),
            "abp":    self._to_list(self.abp[mask]),
            "mean_u": self._to_list(self.mean_u[mask]),
            "etco2":  self._to_list(self.etco2[mask]),
            "co2":    self._to_list(self.co2[mask]) if self.co2.size else [],
        }

    def get_pi_trace(self, start_sec: float, end_sec: float, max_points: int = 8000):
        """
        TCD envelope over a time span, for the PI tab's single plot.

        Individual beats are ~0.5 s wide, so the operator has to see real
        samples to brush one — the fixed 50× decimation used for the overview
        plots would collapse a beat to a single point. Decimation here adapts
        to the zoom level: once the visible span holds fewer than `max_points`
        samples the trace is full-resolution, which is the regime any actual
        selection happens in. Times keep millisecond precision (the sample
        period is 8 ms) so beat shape survives the round-trip.
        """
        n_total = len(self.time)
        if n_total == 0:
            return {"time": [], "env_u": [], "step": 1, "full_res": True}

        i0 = int(np.searchsorted(self.time, start_sec, side="left"))
        i1 = int(np.searchsorted(self.time, end_sec, side="right"))
        i0 = max(0, min(i0, n_total - 1))
        i1 = max(i0 + 1, min(i1, n_total))

        step = max(1, -(-(i1 - i0) // max(1, max_points)))   # ceil division
        t = self.time[i0:i1:step]
        e = self.env_u[i0:i1:step]

        def col(arr):
            if arr is None or len(arr) == 0:
                return []
            s = arr[i0:i1:step]
            return [None if np.isnan(v) else round(float(v), 3) for v in s]

        return {
            "time":   [round(float(v), 3) for v in t],
            "env_u":  col(self.env_u),
            # fiABP/reABP alongside the envelope so the PI tab can show the
            # arterial waveform in the same window (X-synced to the TCD plot).
            "abp":    col(self.abp),
            "step":   int(step),
            "full_res": step == 1,
        }
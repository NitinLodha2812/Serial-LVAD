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


def _normalize_header(s) -> str:
    """Lowercase and strip non-alphanumeric characters from a header string."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


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


def _column_has_data(arr: np.ndarray) -> bool:
    """True if `arr` has at least one finite, non-zero value."""
    if arr is None or len(arr) == 0:
        return False
    valid = ~np.isnan(arr)
    if not valid.any():
        return False
    return bool((np.abs(arr[valid]) > 1e-12).any())


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
    for pattern in COLUMN_ALIASES.get(signal, []):
        for i, nh in enumerate(norm):
            if pattern not in nh:
                continue
            if any(ex in nh for ex in exclusions):
                continue   # e.g. don't pick ETCO2 when looking for raw CO2
            arr = pd.to_numeric(df.iloc[:, i], errors="coerce").to_numpy(dtype=np.float64)
            if _column_has_data(arr):
                return i, headers[i], skipped
            if headers[i] not in skipped:
                skipped.append(headers[i])
    return None, None, skipped


class SessionState:
    """Holds all state for a single loaded file."""

    def __init__(self):
        # metadata
        self.patient_id: str = ""
        self.session: str = ""
        self.raw_path: str = ""

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

        # CVR state
        self.cvr_baseline: dict | None = None   # {time, mean, env, co2}
        self.cvr_hypercap: dict | None = None   # {time, mean, env, co2}
        self.cvr_co2_window: dict | None = None # {start, end} — search window
                                                # for peak ETCO2 (gas-on → gas-off)
        self.cvr_peak_etco2: float | None = None
        self.cvr_peak_time: float | None = None
        self.cvr_result: dict | None = None

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
                        f"Fell back from empty: {', '.join(repr(s) for s in skipped)}."
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
                        f"({', '.join(repr(s) for s in skipped)}) is empty or all-zero. "
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
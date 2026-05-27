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

        # parsed column vectors (numpy float64)
        self.time: np.ndarray = np.array([])
        self.mean_u: np.ndarray = np.array([])
        self.env_u: np.ndarray = np.array([])
        self.abp: np.ndarray = np.array([])
        self.etco2: np.ndarray = np.array([])

        # marks
        self.marks_labels: list = []
        self.marks_times: list = []       # float seconds

        # CA state
        self.ca_selection: dict | None = None   # {time, abp, env}
        self.ca_result: dict | None = None      # {MAP, MFV, corr, finalMX, table}

        # CVR state
        self.cvr_baseline: dict | None = None   # {time, mean, env, co2}
        self.cvr_hypercap: dict | None = None   # {time, mean, env, co2}
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
            if idx < len(df.columns):
                return pd.to_numeric(df.iloc[:, idx], errors='coerce').to_numpy(dtype=np.float64)
            return np.full(n, np.nan)

        # Column mapping (0-indexed):
        # 0: time, 1: 1-1 Mean U, 4: 1-1 Env U, 6: ETCO2, 8: fiABP
        self.mean_u = safe_col(1)
        self.env_u  = safe_col(4)
        self.etco2  = safe_col(6)
        self.abp    = safe_col(8)

        # parse marks – last column
        mark_col_idx = len(self.raw_headers) - 1
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
        return {
            "time":   self._to_list(self.time, decimate),
            "env_u":  self._to_list(self.env_u, decimate),
            "abp":    self._to_list(self.abp, decimate),
            "mean_u": self._to_list(self.mean_u, decimate),
            "etco2":  self._to_list(self.etco2, decimate),
            "marks_labels": self.marks_labels,
            "marks_times":  [round(t, 4) if not np.isnan(t) else None for t in self.marks_times],
            "patient_id": self.patient_id,
            "session": self.session,
            "total_samples": len(self.time),
            "sample_rate": SAMPLE_RATE,
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
        }
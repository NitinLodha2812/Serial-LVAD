"""
cacvr_sessions.py
Per-session/speed persistence for CA and CVR results.

Mirrors the PI tab's auto-save model (see pi_analysis): each session/speed
label owns a JSON snapshot of its CA + CVR results and the selection ranges
needed to redraw the shaded windows. The whole study is accumulated across
labels and then exported in one shot (see export.export_all_cacvr), so the
enormous raw "Master" sheet is written once rather than once per save.
"""

import glob
import json
import os
import re


def _safe(part: str) -> str:
    return re.sub(r"[^\w.\-]", "_", (part or "").strip())


def session_filename(base_name: str, label: str) -> str:
    stem = _safe(base_name) or "recording"
    label = _safe(label)
    return f"{stem}__cacvr__{label}.json" if label else f"{stem}__cacvr__nolabel.json"


def label_from_filename(base_name: str, filename: str) -> str:
    stem = filename[:-5] if filename.endswith(".json") else filename
    marker = f"{_safe(base_name)}__cacvr__"
    if stem.startswith(marker):
        stem = stem[len(marker):]
    return "" if stem == "nolabel" else stem


def _summary(results: dict) -> dict:
    """Compact counts/values for the picker UI, without the full result tables."""
    ca = results.get("CA", {}) or {}
    cvr = results.get("CVR", {}) or {}
    out = {"ca_vessels": [], "cvr_vessels": []}
    for v in ("MCA", "PCA"):
        if ca.get(v):
            out["ca_vessels"].append(v)
        if cvr.get(v):
            out["cvr_vessels"].append(v)
    return out


def save_session(session_dir: str, base_name: str, label: str, payload: dict) -> str:
    os.makedirs(session_dir, exist_ok=True)
    fname = session_filename(base_name, label)
    with open(os.path.join(session_dir, fname), "w") as f:
        json.dump(payload, f, indent=2, allow_nan=False)
    return fname


def load_session(session_dir: str, filename: str) -> dict:
    with open(os.path.join(session_dir, _safe(filename))) as f:
        return json.load(f)


def list_sessions(session_dir: str, base_name: str) -> list:
    """Every saved CA/CVR label for this recording, with a compact summary."""
    if not os.path.isdir(session_dir):
        return []
    pattern = os.path.join(session_dir, f"{_safe(base_name)}__cacvr__*.json")
    out = []
    for path in sorted(glob.glob(pattern)):
        fname = os.path.basename(path)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        out.append({
            "filename": fname,
            "label": data.get("label", label_from_filename(base_name, fname)),
            **_summary(data.get("results", {})),
        })
    return out


def load_all(session_dir: str, base_name: str) -> list:
    """Full session payloads for the export, in label order."""
    sessions = []
    for meta in list_sessions(session_dir, base_name):
        try:
            data = load_session(session_dir, meta["filename"])
        except (OSError, ValueError):
            continue
        sessions.append(data)
    return sessions

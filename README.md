# Serial LVAD Study (v10)

A desktop-style web application for analyzing cerebral blood-flow physiology in
LVAD (Left Ventricular Assist Device) patients. It is a Python/Flask port of the
original MATLAB tool (`CAwindow_v10`).

From a raw recording sampled at **125 Hz**, the app computes two clinical
metrics, each for the **MCA** (middle cerebral artery) and **PCA** (posterior
cerebral artery) vessels:

- **CA / Mx index** — Cerebral Autoregulation, via the Mx correlation index.
- **CVR** — Cerebrovascular Reactivity to CO₂ (both **MCVR** and **WCVR**).

Results can be exported as a unified Excel workbook or a JSON progress file.

---

## Table of contents

1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Running the app](#running-the-app)
4. [Input data format](#input-data-format)
5. [Using the app](#using-the-app)
6. [Exported output](#exported-output)
7. [Project layout](#project-layout)
8. [Troubleshooting](#troubleshooting)

---

## Requirements

- **Python 3.10 or newer** (developed and tested on Python 3.13).
- A modern web browser (Chrome, Edge, Firefox, or Safari). Chart rendering uses
  Chart.js loaded from a CDN, so an **internet connection is required** the first
  time the page loads.

Python package dependencies (pinned in `requirements.txt`):

| Package    | Purpose                                  |
|------------|------------------------------------------|
| Flask      | Web server / API                         |
| numpy      | Numerical signal processing              |
| pandas     | CSV parsing and Excel export             |
| openpyxl   | Excel `.xlsx` writer engine used by pandas |

---

## Installation

These steps assume macOS/Linux (zsh/bash). Windows notes are included where they
differ.

### 1. Open a terminal in the project folder

```bash
cd /Users/nitinlodha/Desktop/Upenn_RA_Stroke
```

### 2. (Recommended) Create and activate a virtual environment

Using a virtual environment keeps these dependencies isolated from your system
Python.

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Once activated, your prompt will be prefixed with `(.venv)`. To leave the
environment later, run `deactivate`.

### 3. Upgrade pip (optional but recommended)

```bash
python -m pip install --upgrade pip
```

### 4. Install the dependencies

```bash
pip install -r requirements.txt
```

### 5. Verify the install

```bash
python -c "import flask, numpy, pandas, openpyxl; print('All dependencies OK')"
```

You should see `All dependencies OK`.

---

## Running the app

With the virtual environment active and dependencies installed:

```bash
python app.py
```

You should see Flask start up with output similar to:

```
 * Running on http://127.0.0.1:5000
 * Debug mode: on
```

Now open a browser and go to:

```
http://127.0.0.1:5000
```

To stop the server, return to the terminal and press **Ctrl+C**.

> **Note:** The app runs in Flask debug mode on port **5000** and stores
> uploaded files and exports in your operating system's temporary directory.
> Exports are downloaded to your browser immediately, but the temp copies are
> not permanent. The app keeps all state in memory for a **single user** — only
> one recording session at a time.

### Changing the port

If port 5000 is already in use (on macOS it is sometimes taken by AirPlay
Receiver), edit the last line of `app.py`:

```python
app.run(debug=True, port=5001)
```

…then browse to `http://127.0.0.1:5001`.

---

## Input data format

The app expects a **comma-delimited** `.txt` or `.csv` file (UTF-8, a leading
byte-order mark is fine). The first row is treated as a header. Columns are read
by **fixed position** (0-indexed):

| Column index | Contents                          |
|--------------|-----------------------------------|
| 0            | Timestamp (`HH:MM:SS` plus optional milliseconds, e.g. `10:42:03 250`) |
| 1            | TCD `1-1 Mean U` (meanU)          |
| 4            | TCD `1-1 Env U` (envU)            |
| 6            | `ETCO2`                           |
| 8            | `fiABP` (arterial blood pressure) |
| last column  | Event **marks** / labels          |

The time vector is rebuilt from column 0 and zeroed so the recording starts at
`t = 0`. If timestamps cannot be parsed, time falls back to sample index ÷ 125 Hz.

**Patient ID and session** are auto-detected from the file name (e.g.
`LVAD012_Session3.txt` → Patient `VAD012`, `Session3`). You can edit both fields
in the UI after loading.

Any non-empty value in the last column (other than `-` or `nan`) is recorded as a
timestamped **mark** and shown as a toggleable line on the plots.

---

## Using the app

The interface has three tabs.

### Main Screen

1. Click **Load File (.txt / .csv)** and choose your recording.
2. On success, the Patient/Session fields fill in, the status bar shows the
   sample count, and the **Activity Log** records each step.
3. Edit Patient ID / Session if needed.
4. Use **Save** (and the format dropdown) at any point to export results.

### CA (Mx) tab

1. Click **Select Start (5-min)**, then click on either plot to set the start of
   a 5-minute analysis window (37,500 samples at 125 Hz).
2. Pick the vessel (**MCA** or **PCA**) in the header dropdown.
3. Click **Calculate MX**. The Mx index appears in the result box.
   - Internally: non-overlapping 3-second averages of ABP/envU → MAP/MFV series,
     then a sliding Pearson correlation (21-value window, step 20) → **Mx = mean
     of the absolute correlation coefficients**.
4. **Clear Selection** removes the window. Use the zoom dropdown + **Apply Zoom**
   (or scroll to zoom, **Shift+drag** to pan) to navigate.

### CVR tab

1. Click **Select Baseline Start**, then click a plot to mark the baseline window
   (~32 s / 4000 samples).
2. Click **Select Hypercapnia Start**, then click a plot to mark the hypercapnia
   window (~10 s / 1250 samples).
3. (Optional) Click **Detect Peak etCO2** to find and mark the peak ETCO₂; this
   value overrides the hypercapnia mean CO₂ in the calculation.
4. Choose the vessel, then click **Calculate CVR**. **MCVR** and **WCVR** appear
   in the result boxes.
5. **Clear Selection** resets all CVR selections.

Repeat the CA and CVR steps for both **MCA** and **PCA** to capture a full
session before exporting.

---

## Exported output

Use **Save** on the Main tab. Two formats are available:

### Excel (Unified) — `*_Unified.xlsx`

A single workbook with these sheets:

- **Master** — the full raw data, reflecting any edits.
- **MCA_CA / PCA_CA** — the CA results tables (MAP, MFV, correlation
  coefficients, final Mx).
- **MCA_CVR_Base / MCA_CVR_Hyp / MCA_CVR_Summary** (and the PCA equivalents) —
  baseline window, hypercapnia window, and the CVR summary.
- **Marks** — mark labels with their timestamps.

### JSON (Progress) — `*.json`

A structured snapshot of patient/session metadata, all per-vessel CA/CVR results,
and the activity log.

Files are named `{patient}_{session}_{timestamp}` and download through the browser
automatically.

---

## Project layout

```
Upenn_RA_Stroke/
├── app.py                     # Flask server: page + JSON API routes
├── requirements.txt           # Python dependencies
├── README.md                  # this file
├── backend/
│   ├── session_state.py       # SessionState container + TXT/CSV parser
│   ├── calculations.py        # compute_mx (CA) and compute_cvr (CVR)
│   └── export.py              # Excel + JSON export
├── templates/
│   └── index.html             # 3-tab UI (Main / CA / CVR)
└── static/
    ├── css/style.css          # styling
    └── js/
        ├── app.js             # UI ↔ API ↔ charts wiring
        └── charts.js          # Chart.js wrappers (CA & CVR plots)
```

---

## Troubleshooting

**`python: command not found`**
Use `python3` instead of `python`. On Windows, install Python from python.org and
ensure "Add Python to PATH" was checked.

**`pip install` fails / permission errors**
Make sure your virtual environment is active (`source .venv/bin/activate`). Avoid
`sudo pip`.

**Port 5000 already in use / "Address already in use"**
Another process (or a previous run) holds the port. Stop it, or change the port
in `app.py` as shown above. On macOS, disable AirPlay Receiver in *System
Settings → General → AirDrop & Handoff* if it occupies 5000.

**Plots don't render / page looks unstyled**
Chart.js is loaded from a CDN; confirm you have an internet connection on first
load, and check the browser console for blocked scripts.

**"Parse error" when loading a file**
Confirm the file is comma-delimited and that the expected columns (see
[Input data format](#input-data-format)) are present at the right positions. The
server terminal prints a full traceback to help diagnose.

**Results look wrong / "Not enough data to compute"**
The CA window needs enough valid (non-NaN) samples to fill at least one
correlation window (~5 minutes). Make sure the selected region contains
continuous valid data.

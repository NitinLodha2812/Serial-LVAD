# Serial LVAD Study (v10)

A desktop-style web application for analyzing cerebral blood-flow physiology in
LVAD (Left Ventricular Assist Device) patients. It is a Python/Flask port of the
original MATLAB tool (`CAwindow_v10`).

From a raw recording sampled at **125 Hz**, the app computes clinical metrics
for the **MCA** (middle cerebral artery) and **PCA** (posterior cerebral
artery) vessels:

- **CA / Mx index** — Cerebral Autoregulation, via the Mx correlation index.
- **CVR** — Cerebrovascular Reactivity to CO₂ (both **MCVR** and **WCVR**).
- **PI** — Pulsatility Index per beat epoch, for **native** (heart-driven) vs
  **artificial** (pump-driven) beats. Ported from the separate MATLAB `PI.m`
  epoch-selector tool.

Results can be exported as a unified Excel workbook or a JSON progress file;
the PI tab writes its own `PI Demographics` spreadsheet.

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

The interface has four tabs.

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

The CVR tab has three plots: **TCD meanU**, **TCD envU**, and the **CO2
waveform**. There is no ETCO₂ plot. The device's end-tidal CO₂ channel is not
valid for this protocol: the mask feeds CO₂-enriched room air, so the sensor
sees the inhaled CO₂ along with the expired gas and reports a false end-tidal
value. The true end-tidal CO₂ is therefore read off the CO₂ waveform by hand.

The two selection surfaces are **decoupled**:

- **TCD windows** (blood-flow averages) — click **Select Baseline Start (TCD)**
  then a TCD plot to mark the baseline window (~32 s), and **Select Hypercapnia
  Start (TCD)** for the hypercapnia window (~10 s). Each window averages meanU
  (MCBF) and envU (WCBF) only. CO₂ is not averaged over these windows.
- **CO₂ points** — click **Select CO2 Baseline Point**, then click the true
  end-tidal point on the CO₂ waveform; then **Select CO2 Hypercapnia Point** and
  click the hypercapnic end-tidal point. The picked values and the live
  **ΔCO₂ = hypercapnia − baseline** appear in the sidebar readout. (If the exact
  clicked sample was brushed out, it snaps to the nearest valid sample.)

Then choose the vessel and click **Calculate CVR**:

```
MCVR = (ΔMCBF / baseline MCBF) x (100 / ΔCO2)
WCVR = (ΔWCBF / baseline WCBF) x (100 / ΔCO2)
```

**Clear Selection** resets the TCD windows and both CO₂ points.

Repeat the CA and CVR steps for both **MCA** and **PCA** to capture a full
session before exporting.

### CA/CVR sessions and one-shot study export

CA and CVR results are **accumulated per session/speed** (the same model as the
PI tab) so a whole study exports once at the end instead of re-writing the huge
raw data on every save:

1. Set the **Session / Speed** label (shared between the CA and CVR tabs).
2. Compute CA and CVR for that label — results auto-save the moment they're
   calculated.
3. **Save & Next Session** starts a clean label (offering to reload it if it was
   worked on before). **Load Session…** restores a saved label's results and
   selection windows for review.
4. When the whole study is done, **Export All Study (zip)** produces a single
   download containing:
   - one **`*_Master.xlsx`** — the raw data with all edits, written **once**;
   - one **`<patient>_<label>.xlsx`** per session/speed, each with the CA and
     CVR sheets in that one workbook;
   - one **`*_progress.json`** — a reloadable snapshot of everything (the redo
     safety net).

Because the giant Master sheet is written a single time rather than per save,
exporting a multi-session study is much faster. The PI tab keeps its **own**
separate `PI Demographics` export and is not part of this bundle.

**Linked deletions:** brushing a signal to NaN on any tab edits the one shared
copy of that signal, so the deletion applies to every tab's calculations *and*
is echoed visually onto the same signal's plot on the other tabs.

### Navigating the plots (all tabs)

Beyond scroll-to-zoom (X) and **Shift+drag** to pan, each tab has a control row:

- **Y ＋ / Y － / Y ⟲** — zoom the Y axis in/out, or reset it, **without changing
  the X window** (handy for making a beat taller to inspect it).
- **◀ / ▶** — slide the visible window left/right **keeping its width** (shift
  the zoomed-in region without zooming out and back in).

On the PI tab both plots stay X-synced as you zoom or pan.

### PI (Epochs) tab

Selects individual beats on the TCD envelope and reports a pulsatility index
for each. This is the Python port of the standalone MATLAB `PI.m` tool.

1. Fill in **Session / Speed** and **Vessel** in the sidebar. Both are written
   into the exported spreadsheet, and the speed also names the auto-save file.
2. Zoom in until you can see individual beats — the zoom dropdown has
   **Scale to 30 s**, or use the scroll wheel. The caption above the plot tells
   you whether you are looking at full-resolution samples; you cannot brush a
   beat accurately from a decimated view.
3. With **Brush: ON** (the default), drag a rectangle around one beat, then
   click **Select Native** or **Select Artificial**. Shift+drag pans instead of
   brushing, and the scroll wheel still zooms.
4. **Auto-Select Artificial** places an epoch every 2 s, each spanning
   **−0.15 s before to +0.20 s after** the peak (0.35 s total — the pump beat is
   asymmetric: ~0.15 s deceleration, ~0.20 s acceleration), marching from the
   point you click to the right edge of the current view. These are fixed by the
   beat physiology and are not adjustable. Pump beats are metronomic, so one
   click captures a whole run of them.
5. **Undo Last** drops the most recent epoch. Tick rows in the *Selected Epochs*
   table and click **Remove Checked** to delete specific ones. **Clear All**
   empties the working set but leaves the saved session on disk.
6. **Export to Excel** appends one row to a `PI Demographics` sheet. Attach a
   *prior workbook* to append to an existing sheet; leave it empty to create one.
7. **Save & Next Speed** exports, then clears the selections and switches to a
   new speed label — offering to reload that speed if you have worked on it
   before.

Every selection change **auto-saves** to a per-speed file, so a crash or a
mis-click never costs a session. **Load Speed…** restores one of them;
**Load All Speeds** overlays every saved speed at once, colour-coded, as a
read-only comparison view.

Per epoch the app reports, matching `PI.m`:

| Column | Meaning |
|--------|---------|
| `Hi` / `Lo` / `Mean` | max, min and mean of the envelope over the epoch |
| `PW`   | epoch duration, `t_last − t_first` (seconds) |
| `PI`   | pulsatility index, `(Hi − Lo) / Mean` |

Epochs are read from the **full-resolution** envelope regardless of the zoom
level, and the envelope column is located by header name — so a recording whose
`1-1 Env U` column sits somewhere other than column 5 still works.

The PI tab shows the **fiABP / reABP** pressure waveform beneath the TCD trace,
X-synced to it, with each selected beat shaded on the pressure plot too. Native
beats are drawn in **blue** and artificial in **red** (chosen to be
distinguishable for red-green colour-blind reviewers). **Note:** a fixed
TCD↔pressure timing offset is not yet applied — the shaded pressure span is the
same wall-clock window as the TCD beat, pending the agreed correction.

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
the PI epoch metrics, and the activity log. Per-sample epoch waveforms are left
out — they are recoverable from the raw recording.

Files are named `{patient}_{session}_{timestamp}` and download through the browser
automatically.

### PI Demographics — `*---{timestamp}.xlsx`

Written by **Export to Excel** on the PI tab, not by the Main-tab Save button.
One row per (patient, speed, vessel), with columns `PatientID`, `SessionSpeed`,
`Vessel`, then `NatHi_1 … NatPI_n` and `ArtHi_1 … ArtPI_n` for every selected
epoch. Uploading a prior workbook appends to its existing sheet, padding both
sides so old and new column sets line up; other sheets in that workbook are
carried across. The source file is never overwritten — the timestamped copy is
a new file.

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
│   ├── pi_analysis.py         # PI epochs, metrics, per-speed session store
│   ├── cacvr_sessions.py      # CA/CVR per-session/speed persistence
│   └── export.py              # Excel + JSON + PI Demographics + study bundle
├── templates/
│   └── index.html             # 4-tab UI (Main / CA / CVR / PI)
└── static/
    ├── css/style.css          # styling
    └── js/
        ├── app.js             # UI ↔ API ↔ charts wiring
        └── charts.js          # Chart.js wrappers (CA, CVR & PI plots)
```

PI selections auto-save to your system temp directory
(`serial_lvad_pi_sessions/`), one JSON file per recording and speed — the
web equivalent of `PI.m`'s `<basename>_speed<N>_selections.mat`.

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

**PI: "No envelope samples inside the brushed region."**
The rectangle you dragged contains no valid envelope samples — either it sits in
a gap you previously replaced with NaN, or its vertical extent misses the trace.
Zoom in (the plot caption confirms when you are at full resolution) and drag a
box that clearly encloses the beat.

**PI: my brushed epoch has only one or two samples**
You brushed while the plot was still decimated. The caption above the plot reads
*"1 in N samples shown"* until you zoom in far enough; **Scale to 30 s** always
gets you to full resolution.

**Results look wrong / "Not enough data to compute"**
The CA window needs enough valid (non-NaN) samples to fill at least one
correlation window (~5 minutes). Make sure the selected region contains
continuous valid data.

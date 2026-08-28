# LVAD GUI

A desktop-style web application for analyzing cerebral blood-flow physiology in
LVAD (Left Ventricular Assist Device) patients. It is a Python/Flask port of the
original MATLAB tools.

The same GUI serves **two studies**, picked by which load button you use:

- **Serial LVAD** — longitudinal, one visit per file over ~2 years, two vessels
  (**MCA** and **PCA**). Results are tagged by **Vessel**.
- **RAMPs** — one visit, many LVAD speeds in one file, **MCA** only. Results are
  tagged by **Speed**.

The title bar reads **LVAD GUI** until you load a study, then becomes
**Serial LVAD Study** or **RAMPs Study**.

From a raw recording sampled at **125 Hz**, the app computes:

- **CA / Mx index** — Cerebral Autoregulation, via the Mx correlation index
  (plus a standalone **MFV-only** 3-minute measurement).
- **CVR** — Cerebrovascular Reactivity to CO₂ (both **MCVR** and **WCVR**).
- **PI** — Pulsatility Index per beat epoch, for **native** (heart-driven) vs
  **artificial** (pump-driven) beats. Ported from the separate MATLAB `PI.m`.

CA and CVR are tagged, accumulated, and exported together from one place on the
Main screen. PI keeps its own separate export. A run can be saved to a JSON
progress file and reopened later to continue.

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

**Patient ID** is auto-detected from the file name, per study:

- **Serial LVAD** — `LVAD012_Session3.txt` → `VAD012`.
- **RAMPs** — `RAMPs001`, `RAMP012`, and `R025` all normalise to `R###`
  (`R001`, `R012`, `R025`) using the number after the R/RAMP(s) token.

You can edit the Patient field in the UI after loading.

Any non-empty value in the last column (other than `-` or `nan`) is recorded as a
timestamped **mark** and shown as a toggleable line on the plots.

---

## Using the app

The interface has four tabs.

### Main Screen

The Main screen is the single home for loading, tagging, saving, and exporting.

1. **Load Study** — click **Load Serial LVAD Data** or **Load RAMPs Data** and
   choose your `.txt`/`.csv` recording. The title bar and the tag vocabulary
   switch to match the study. The **Patient** field auto-fills (see
   [auto-naming](#input-data-format)); edit it if needed.
2. **Working Session (CA & CVR)** — set the tag (**Vessel** for Serial LVAD,
   e.g. `MCA`/`PCA`; **Speed** for RAMPs). Compute on the CA and CVR tabs; each
   tag auto-saves. **Save & Next** moves to a new tag; **Load…** restores a
   saved tag's results for review.
3. **Save & Export** — **Save Progress (JSON)** writes a reopenable snapshot of
   every tag's results. **Export All Study (zip)** is the final export (master
   data + one workbook per tag + the JSON). **Load Progress (JSON)…** reopens a
   saved progress file to continue later.

The **Activity Log** records every step, including the CVR values as they are
calculated.

### CA (Mx) tab

1. Click **Select Start (5-min)**, then click on either plot to set the start of
   a 5-minute analysis window (37,500 samples at 125 Hz).
2. Click **Calculate MX**. The **MX Index** and **Mean MFV** appear right under
   the buttons. The result is tagged automatically (the vessel is derived from
   the study/tag — RAMPs is always MCA — so there is no vessel dropdown).
   - Internally: non-overlapping 3-second averages of ABP/envU → MAP/MFV series,
     then a sliding Pearson correlation (21-value window, step 20) → **Mx = mean
     of the absolute correlation coefficients**.
3. **Calculate MFV only (3-min)** — click the button, then click the TCD plot to
   start a **3-minute** epoch. The app collects 3 minutes of valid (non-NaN)
   envelope samples, averages them, and prints the result in the **Mean MFV**
   box while the **MX** box stays empty. Use this when you only need MFV (a
   shorter, TCD-only measurement, no ABP).
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

Click **Calculate CVR** (the vessel is derived from the tag; no dropdown):

```
MCVR = (ΔMCBF / baseline MCBF) x (100 / ΔCO2)
WCVR = (ΔWCBF / baseline WCBF) x (100 / ΔCO2)
```

The printed calculation values (MCVR, WCVR, and the baseline/hypercapnia/delta
MCBF, WCBF and CO₂) appear in a small table **right under the Calculate CVR
button**.

**Selective clear** — the **Selections** panel lists each active selection
(baseline window, hypercapnia window, CO₂ baseline, CO₂ hypercapnia). Click the
**×** next to one to remove just that selection, or **Clear All Selections** to
remove them all (as on the PI tab). CA does not need this — it has only one
window.

### CA/CVR tags and one-shot study export

CA and CVR results are **accumulated per tag** so a whole study exports once at
the end instead of re-writing the huge raw data on every save. All of these
controls live in one place on the **Main screen** (not on the CA/CVR tabs):

1. Set the tag — **Vessel** (Serial LVAD, e.g. `MCA`/`PCA`) or **Speed** (RAMPs).
2. Compute CA and CVR for that tag — results auto-save the moment they're
   calculated.
3. **Save & Next Vessel/Speed** starts a clean tag (offering to reload it if it
   was worked on before). **Load Vessel/Speed…** restores a saved tag's results
   and selection windows for review.
4. **Save Progress (JSON)** at any time writes a small, reopenable snapshot of
   every tag's results.
5. When the whole study is done, **Export All Study (zip)** produces a single
   download containing:
   - one **`*_Master.xlsx`** — the raw data with all edits, written **once**;
   - one **`<patient>_<tag>.xlsx`** per tag, each with the CA and CVR sheets;
   - one **`*_progress.json`** — a reloadable snapshot of everything.

Because the giant Master sheet is written a single time rather than per save,
exporting a multi-tag study is much faster. The PI tab keeps its **own**
separate `PI Demographics` export and is not part of this bundle.

### Reopening a saved study

**Load Progress (JSON)…** on the Main screen reopens a previously saved progress
file (from **Save Progress** or the JSON inside an **Export All** zip) and
restores every tag's results, so you can review them, load a tag, or re-export —
the same idea as reopening in the PI GUI. The raw waveform is **not** stored in
the JSON, so the plots stay empty until you load the original recording; because
the restored tags are keyed by the recording's name, loading that recording
afterwards lines them straight back up. (Reopening a raw `.txt`/`.csv` still goes
through the two **Load … Data** buttons; the Excel files are not used for
reopening.)

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

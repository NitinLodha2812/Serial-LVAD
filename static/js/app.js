/* ═══════════════════════════════════════════════════════════════
   app.js
   Main application logic — wires UI ↔ API ↔ Charts.
   ═══════════════════════════════════════════════════════════════ */

const caCharts = new CACharts();
const cvrCharts = new CVRCharts();
let sessionData = null;   // overview data from server
let clickMode = null;     // 'ca_select' | 'cvr_base' | 'cvr_hyp' | null

/* ═══════════════════════ UTILITIES ═══════════════════════ */

function $(id) { return document.getElementById(id); }

function showLoading() { $('loadingOverlay').classList.add('active'); }
function hideLoading() { $('loadingOverlay').classList.remove('active'); }

function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  $('toastContainer').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function appendLog(msg) {
  const box = $('logBox');
  const ts = new Date().toLocaleTimeString();
  box.textContent += `\n[${ts}] ${msg}`;
  box.scrollTop = box.scrollHeight;
}

async function api(url, opts = {}) {
  let res;
  try {
    res = await fetch(url, opts);
  } catch (networkErr) {
    throw new Error('Network error — is the server running?');
  }
  let json;
  try {
    json = await res.json();
  } catch (parseErr) {
    const text = await res.text().catch(() => '');
    throw new Error(`Server error (${res.status}): ${text.substring(0, 200) || 'non-JSON response'}`);
  }
  if (!res.ok) throw new Error(json.error || `Server error (${res.status})`);
  return json;
}

function enableCA(yes) {
  ['caSelectBtn', 'caClearBtn', 'caCalcBtn', 'caZoomMenu', 'caZoomBtn',
   'caBrushBtn'].forEach(id => {
    $(id).disabled = !yes;
  });
}

function enableCVR(yes) {
  ['cvrSelectBaseBtn', 'cvrSelectHypBtn', 'cvrClearBtn', 'cvrCalcBtn',
   'cvrPeakBtn', 'cvrZoomMenu', 'cvrZoomBtn',
   'cvrBrushBtn', 'cvrCo2WinBtn'].forEach(id => {
    $(id).disabled = !yes;
  });
}

function updateStatus(loaded) {
  $('statusDot').classList.toggle('loaded', loaded);
  if (loaded && sessionData) {
    $('statusText').textContent =
      `${sessionData.patient_id} / ${sessionData.session} — ${sessionData.total_samples.toLocaleString()} samples`;
  } else {
    $('statusText').textContent = 'No file loaded';
  }
}

/* ═══════════════════════ TABS ═══════════════════════ */

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $('tab-' + btn.dataset.tab).classList.add('active');
  });
});


/* ═══════════════════════ FILE LOAD ═══════════════════════ */

$('loadBtn').addEventListener('click', () => {
  $('fileInput').click();
});

$('fileInput').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;

  showLoading();
  appendLog(`Loading file: ${file.name} ...`);

  const form = new FormData();
  form.append('file', file);

  try {
    sessionData = await api('/api/load', { method: 'POST', body: form });

    // fill metadata
    $('patientId').value = sessionData.patient_id;
    $('sessionId').value = sessionData.session;
    $('patientId').disabled = false;
    $('sessionId').disabled = false;
    $('vesselSelect').disabled = false;
    $('saveFormat').disabled = false;
    $('saveBtn').disabled = false;

    // init charts
    caCharts.init(sessionData);
    cvrCharts.init(sessionData);

    // populate marks
    populateMarks('ca', sessionData);
    populateMarks('cvr', sessionData);

    enableCA(true);
    enableCVR(true);
    updateStatus(true);

    appendLog(`Loaded successfully — ${sessionData.total_samples} samples`);

    // Surface the backend's load-time messages so the user sees which columns
    // were matched (and any fallbacks / warnings — e.g. ABP fell back to A-LINE).
    let warned = false;
    (sessionData.load_log || []).forEach(line => {
      if (/Matched|Fell back|WARNING/i.test(line)) {
        appendLog(line.replace(/^\[\d{2}:\d{2}:\d{2}\]\s*/, ''));
        if (/WARNING/i.test(line)) warned = true;
      }
    });
    if (sessionData.abp_source) {
      appendLog(`ABP source: ${sessionData.abp_source}`);
    }
    if (warned) {
      toast('Loaded with warnings — see Activity Log', 'info');
    } else {
      toast('File loaded successfully', 'success');
    }
  } catch (err) {
    toast(err.message, 'error');
    appendLog(`Load error: ${err.message}`);
  }

  hideLoading();
  e.target.value = '';  // allow re-selecting same file
});


/* ═══════════════════════ MARKS ═══════════════════════ */

function populateMarks(prefix, data) {
  const container = $(prefix + 'MarksList');
  container.innerHTML = '';
  data.marks_labels.forEach((label, i) => {
    const lbl = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = true;
    cb.dataset.index = i;
    cb.addEventListener('change', () => toggleMarks(prefix));
    const text = document.createTextNode(' ' + label + ' ');
    const timeSpan = document.createElement('span');
    timeSpan.className = 'mark-time';
    timeSpan.textContent = data.marks_times[i] != null ? data.marks_times[i].toFixed(1) + 's' : '';
    lbl.append(cb, text, timeSpan);
    container.appendChild(lbl);
  });
}

function toggleMarks(prefix) {
  const container = $(prefix + 'MarksList');
  const visible = new Set();
  container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    if (cb.checked) visible.add(parseInt(cb.dataset.index));
  });
  if (prefix === 'ca') caCharts.updateMarks(visible);
  else cvrCharts.updateMarks(visible);
}


/* ═══════════════════════ METADATA ═══════════════════════ */

$('patientId').addEventListener('change', async () => {
  try { await api('/api/metadata', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ patient_id: $('patientId').value }) }); } catch {}
  appendLog('Patient ID updated to: ' + $('patientId').value);
});

$('sessionId').addEventListener('change', async () => {
  try { await api('/api/metadata', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ session: $('sessionId').value }) }); } catch {}
  appendLog('Session updated to: ' + $('sessionId').value);
});


/* ═══════════════════════ CA — SELECT ═══════════════════════ */

$('caSelectBtn').addEventListener('click', () => {
  clickMode = 'ca_select';
  toast('Click on a plot to set the 5-minute start point', 'info');
  appendLog('CA: Click on ABP or envU plot to set start time.');
  // make canvases clickable
  $('caPlot1').parentElement.classList.add('clickable');
  $('caPlot2').parentElement.classList.add('clickable');
});

// listen for click on CA canvases
['caPlot1', 'caPlot2'].forEach(id => {
  $(id).addEventListener('click', async (e) => {
    if (clickMode !== 'ca_select') return;
    clickMode = null;
    $('caPlot1').parentElement.classList.remove('clickable');
    $('caPlot2').parentElement.classList.remove('clickable');

    const chart = id === 'caPlot1' ? caCharts.chart1 : caCharts.chart2;
    const clickX = caCharts.getClickX(chart, e);

    showLoading();
    try {
      const sel = await api('/api/ca/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start_time: clickX }),
      });
      caCharts.addSelection(sel);
      appendLog(`CA: Selection from t=${sel.start_time.toFixed(1)} to t=${sel.end_time.toFixed(1)}`);
      toast('5-minute window selected', 'success');
    } catch (err) {
      toast(err.message, 'error');
      appendLog('CA select error: ' + err.message);
    }
    hideLoading();
  });
});


/* ═══════════════════════ CA — CLEAR ═══════════════════════ */

$('caClearBtn').addEventListener('click', async () => {
  try {
    await api('/api/ca/clear', { method: 'POST' });
    caCharts.clearSelection();
    $('caResultBox').classList.add('hidden');
    appendLog('CA: Selection cleared.');
    toast('Selection cleared', 'info');
  } catch (err) { toast(err.message, 'error'); }
});


/* ═══════════════════════ CA — CALCULATE ═══════════════════════ */

$('caCalcBtn').addEventListener('click', async () => {
  showLoading();
  try {
    const vessel = $('vesselSelect').value;
    const result = await api('/api/ca/calculate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vessel }),
    });
    const mxTxt = Number.isFinite(result.final_mx) ? result.final_mx.toFixed(4) : '—';
    const mfv = result.mean_mfv;
    const mfvTxt = (mfv == null || !Number.isFinite(mfv)) ? '—' : mfv.toFixed(2);
    $('caResultValue').textContent = `${mxTxt} (${vessel})`;
    $('caMeanMfv').textContent     = `${mfvTxt} (${vessel})`;
    $('caResultBox').classList.remove('hidden');
    appendLog(`CA: MX = ${mxTxt}, Mean MFV = ${mfvTxt} (${vessel})`);
    toast(`MX = ${mxTxt}, Mean MFV = ${mfvTxt}`, 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('CA calculate error: ' + err.message);
  }
  hideLoading();
});


/* ═══════════════════════ CA — ZOOM ═══════════════════════ */

$('caZoomBtn').addEventListener('click', () => {
  const mode = $('caZoomMenu').value;
  if (mode === 'home') {
    caCharts.resetZoom();
  } else if (mode === 'scale5') {
    const r = caCharts.getXRange();
    caCharts.zoomToRange(r.min, 300);
  }
  // zoomX and zoomY are handled by Chart.js wheel zoom by default
});


/* ═══════════════════════ CVR — SELECT BASELINE ═══════════════════════ */

const CVR_PLOT_IDS = ['cvrPlot1', 'cvrPlot2', 'cvrPlot3', 'cvrPlot4'];
const cvrChartFor = (id) => ({
  cvrPlot1: cvrCharts.chart1, cvrPlot2: cvrCharts.chart2,
  cvrPlot3: cvrCharts.chart3, cvrPlot4: cvrCharts.chart4,
}[id]);

$('cvrSelectBaseBtn').addEventListener('click', () => {
  clickMode = 'cvr_base';
  toast('Click on a plot to set the baseline start point', 'info');
  appendLog('CVR: Click on plot to set baseline start.');
  CVR_PLOT_IDS.forEach(id => $(id).parentElement.classList.add('clickable'));
});

// Pending CO2-window start time captured between the two clicks.
let cvrCo2WinStart = null;

CVR_PLOT_IDS.forEach(id => {
  $(id).addEventListener('click', async (e) => {
    if (!['cvr_base', 'cvr_hyp', 'cvr_co2win_start', 'cvr_co2win_end'].includes(clickMode)) return;

    const chart = cvrChartFor(id);
    const clickX = cvrCharts.getClickX(chart, e);
    const mode = clickMode;

    // ── CO2 search window: two-click select (start, then end) ──
    if (mode === 'cvr_co2win_start') {
      cvrCo2WinStart = clickX;
      clickMode = 'cvr_co2win_end';
      appendLog(`CVR: CO2 window start = ${clickX.toFixed(1)} s — click again to set end.`);
      toast('Now click the end of the CO2 window', 'info');
      return;
    }
    if (mode === 'cvr_co2win_end') {
      const start = Math.min(cvrCo2WinStart, clickX);
      const end   = Math.max(cvrCo2WinStart, clickX);
      cvrCo2WinStart = null;
      clickMode = null;
      CVR_PLOT_IDS.forEach(i => $(i).parentElement.classList.remove('clickable'));
      showLoading();
      try {
        const sel = await api('/api/cvr/select_co2_window', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ start_time: start, end_time: end }),
        });
        cvrCharts.addCo2Window(sel);
        $('cvrCo2WinClearBtn').disabled = false;
        appendLog(`CVR: CO2 search window set t=${sel.start_time.toFixed(1)}..${sel.end_time.toFixed(1)} `
                + `(${sel.n_samples} samples).`);
        toast('CO2 search window set', 'success');
      } catch (err) {
        toast(err.message, 'error');
        appendLog('CVR CO2-window error: ' + err.message);
      }
      hideLoading();
      return;
    }

    clickMode = null;
    CVR_PLOT_IDS.forEach(i => $(i).parentElement.classList.remove('clickable'));

    showLoading();
    try {
      if (mode === 'cvr_base') {
        const sel = await api('/api/cvr/select_baseline', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ start_time: clickX }),
        });
        cvrCharts.addBaseline(sel);
        appendLog(`CVR: Baseline from t=${sel.start_time.toFixed(1)} to t=${sel.end_time.toFixed(1)}`);
        toast('Baseline selected', 'success');
      } else {
        const sel = await api('/api/cvr/select_hypercapnia', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ start_time: clickX }),
        });
        cvrCharts.addHypercapnia(sel);
        appendLog(`CVR: Hypercapnia from t=${sel.start_time.toFixed(1)} to t=${sel.end_time.toFixed(1)}`);
        toast('Hypercapnia selected', 'success');
      }
    } catch (err) {
      toast(err.message, 'error');
      appendLog(`CVR ${mode} error: ` + err.message);
    }
    hideLoading();
  });
});


/* ═══════════════════════ CVR — SELECT HYPERCAPNIA ═══════════════════════ */

$('cvrSelectHypBtn').addEventListener('click', () => {
  clickMode = 'cvr_hyp';
  toast('Click on a plot to set the hypercapnia start point', 'info');
  appendLog('CVR: Click on plot to set hypercapnia start.');
  CVR_PLOT_IDS.forEach(id => $(id).parentElement.classList.add('clickable'));
});

/* ── CVR — Select CO2 Window (two-click: start then end) ── */
$('cvrCo2WinBtn').addEventListener('click', () => {
  clickMode = 'cvr_co2win_start';
  cvrCo2WinStart = null;
  toast('Click the START of the CO2 search window (gas on)', 'info');
  appendLog('CVR: Click plot to set CO2 search window — start (gas on).');
  CVR_PLOT_IDS.forEach(id => $(id).parentElement.classList.add('clickable'));
});

$('cvrCo2WinClearBtn').addEventListener('click', () => {
  cvrCharts.clearCo2Window();
  cvrCo2WinStart = null;
  $('cvrCo2WinClearBtn').disabled = true;
  appendLog('CVR: CO2 search window cleared (peak-detect will fall back to hypercapnia window).');
  toast('CO2 window cleared', 'info');
});


/* ═══════════════════════ CVR — CLEAR ═══════════════════════ */

$('cvrClearBtn').addEventListener('click', async () => {
  try {
    await api('/api/cvr/clear', { method: 'POST' });
    cvrCharts.clearOverlays();
    $('cvrResultBox').classList.add('hidden');
    $('cvrCo2WinClearBtn').disabled = true;
    appendLog('CVR: Selections cleared.');
    toast('Selections cleared', 'info');
  } catch (err) { toast(err.message, 'error'); }
});


/* ═══════════════════════ CVR — DETECT PEAK ═══════════════════════ */

$('cvrPeakBtn').addEventListener('click', async () => {
  try {
    const result = await api('/api/cvr/detect_peak', { method: 'POST' });
    cvrCharts.addPeakMarker(result.peak_time, result.peak_value);
    appendLog(`CVR: Peak etCO2 = ${result.peak_value} at t=${result.peak_time}`);
    toast(`Peak etCO2 = ${result.peak_value.toFixed(2)}`, 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('CVR peak error: ' + err.message);
  }
});


/* ═══════════════════════ CVR — CALCULATE ═══════════════════════ */

$('cvrCalcBtn').addEventListener('click', async () => {
  showLoading();
  try {
    const vessel = $('vesselSelect').value;
    const result = await api('/api/cvr/calculate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vessel }),
    });
    $('cvrMCVR').textContent = result.mcvr.toFixed(4);
    $('cvrWCVR').textContent = result.wcvr.toFixed(4);
    $('cvrResultBox').classList.remove('hidden');
    appendLog(`CVR: MCVR = ${result.mcvr.toFixed(4)}, WCVR = ${result.wcvr.toFixed(4)} (${vessel})`);
    toast(`MCVR = ${result.mcvr.toFixed(4)}, WCVR = ${result.wcvr.toFixed(4)}`, 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('CVR calculate error: ' + err.message);
  }
  hideLoading();
});


/* ═══════════════════════ CVR — ZOOM ═══════════════════════ */

$('cvrZoomBtn').addEventListener('click', () => {
  const mode = $('cvrZoomMenu').value;
  if (mode === 'home') {
    cvrCharts.resetZoom();
  } else if (mode === 'scale5') {
    const r = cvrCharts.getXRange();
    cvrCharts.zoomToRange(r.min, 300);
  }
});


/* ═══════════════════════ BRUSH (NaN edit) ═══════════════════════
   Drag-select a rectangle on any plot → those samples in the underlying
   signal become NaN, preserving the time axis. */

const brushState = {
  ca:  { mode: false, hasBrush: false },
  cvr: { mode: false, hasBrush: false },
};

function updateBrushButtons(tab) {
  const s = brushState[tab];
  const prefix = tab;   // 'ca' or 'cvr'
  $(prefix + 'BrushBtn').textContent = 'Brush: ' + (s.mode ? 'ON' : 'OFF');
  $(prefix + 'BrushBtn').classList.toggle('btn-primary', s.mode);
  $(prefix + 'NanBtn').disabled = !s.hasBrush;
  $(prefix + 'BrushClearBtn').disabled = !s.hasBrush;
}

// Hook each chart-controller's brush-change callback to update the UI.
caCharts.onBrushChange  = (info) => {
  brushState.ca.hasBrush = !!info.hasBrush;
  updateBrushButtons('ca');
};
cvrCharts.onBrushChange = (info) => {
  brushState.cvr.hasBrush = !!info.hasBrush;
  updateBrushButtons('cvr');
};

function toggleBrushMode(tab) {
  const s = brushState[tab];
  s.mode = !s.mode;
  if (tab === 'ca')  caCharts.setBrushMode(s.mode);
  else               cvrCharts.setBrushMode(s.mode);
  // Cancel any pending click-to-select mode if brushing is being turned on.
  if (s.mode) {
    clickMode = null;
    ['caPlot1', 'caPlot2', 'cvrPlot1', 'cvrPlot2', 'cvrPlot3', 'cvrPlot4'].forEach(id => {
      const el = $(id);
      if (el && el.parentElement) el.parentElement.classList.remove('clickable');
    });
  } else {
    // Leaving brush mode also clears any pending brush rectangle.
    if (tab === 'ca')  caCharts.clearBrush();
    else               cvrCharts.clearBrush();
    s.hasBrush = false;
  }
  updateBrushButtons(tab);
  appendLog(`${tab.toUpperCase()}: Brush mode ${s.mode ? 'ON' : 'OFF'}.`);
}

$('caBrushBtn').addEventListener('click',  () => toggleBrushMode('ca'));
$('cvrBrushBtn').addEventListener('click', () => toggleBrushMode('cvr'));

$('caBrushClearBtn').addEventListener('click',  () => { caCharts.clearBrush(); });
$('cvrBrushClearBtn').addEventListener('click', () => { cvrCharts.clearBrush(); });

async function applyNaN(tab) {
  const charts = (tab === 'ca') ? caCharts : cvrCharts;
  const ab = charts.activeBrush();
  if (!ab) { toast('Nothing brushed yet', 'info'); return; }
  showLoading();
  try {
    const res = await api('/api/edit/nan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ signal: ab.signal, ranges: [ab.rect] }),
    });
    // Update chart locally so the user sees the gap immediately.
    charts.applyNaNLocally();
    brushState[tab].hasBrush = false;
    updateBrushButtons(tab);
    appendLog(`${tab.toUpperCase()}: Replaced ${res.changed} sample(s) in ${ab.signal} with NaN.`);
    toast(`Replaced ${res.changed} sample(s) with NaN`, 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog(`${tab.toUpperCase()} NaN-replace error: ${err.message}`);
  }
  hideLoading();
}

$('caNanBtn').addEventListener('click',  () => applyNaN('ca'));
$('cvrNanBtn').addEventListener('click', () => applyNaN('cvr'));


/* ═══════════════════════ SAVE ═══════════════════════ */

$('saveBtn').addEventListener('click', async () => {
  showLoading();
  const fmt = $('saveFormat').value;
  try {
    const result = await api('/api/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ format: fmt }),
    });
    // trigger download
    const a = document.createElement('a');
    a.href = '/api/download/' + result.filename;
    a.download = result.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    appendLog(`Saved: ${result.filename}`);
    toast('File saved & downloaded', 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('Save error: ' + err.message);
  }
  hideLoading();
});
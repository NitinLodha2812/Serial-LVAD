/* ═══════════════════════════════════════════════════════════════
   app.js
   Main application logic — wires UI ↔ API ↔ Charts.
   ═══════════════════════════════════════════════════════════════ */

const caCharts = new CACharts();
const cvrCharts = new CVRCharts();
const piChart = new PIChart();
let sessionData = null;   // overview data from server
let clickMode = null;     // 'ca_select' | 'cvr_base' | 'cvr_hyp' | 'pi_auto' | null

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

// Y-zoom / X-pan buttons shared by every tab.
const navIds = (p) => [`${p}YIn`, `${p}YOut`, `${p}YReset`, `${p}PanL`, `${p}PanR`];

function enableCA(yes) {
  ['caSelectBtn', 'caClearBtn', 'caCalcBtn', 'caZoomMenu', 'caZoomBtn',
   'caBrushBtn', 'caSessLabel', 'caLoadSessBtn', 'caNextSessBtn', 'caExportAllBtn',
   ...navIds('ca')].forEach(id => {
    $(id).disabled = !yes;
  });
}

function enableCVR(yes) {
  ['cvrSelectBaseBtn', 'cvrSelectHypBtn', 'cvrCo2BaseBtn', 'cvrCo2HypBtn',
   'cvrClearBtn', 'cvrCalcBtn', 'cvrZoomMenu', 'cvrZoomBtn',
   'cvrBrushBtn',
   'cvrSessLabel', 'cvrLoadSessBtn', 'cvrNextSessBtn', 'cvrExportAllBtn',
   ...navIds('cvr')].forEach(id => {
    $(id).disabled = !yes;
  });
}

function enablePI(yes) {
  ['piSpeed', 'piVessel', 'piBrushBtn', 'piAutoBtn',
   'piClearBtn', 'piZoomMenu', 'piZoomBtn', 'piLoadSpeedBtn', 'piLoadAllBtn',
   'piNextSpeedBtn', 'piWorkbook', 'piExportBtn',
   ...navIds('pi')].forEach(id => {
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
    piChart.init(sessionData);

    // populate marks
    populateMarks('ca', sessionData);
    populateMarks('cvr', sessionData);
    populateMarks('pi', sessionData);

    enableCA(true);
    enableCVR(true);
    enablePI(true);
    updateStatus(true);

    await initPITab();
    await initCacvrSession();

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
  ({ ca: caCharts, cvr: cvrCharts, pi: piChart })[prefix].updateMarks(visible);
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


/* ═══════════════════════ CVR — SELECTIONS ═══════════════════════
   Two decoupled selection surfaces:
     - TCD plots (meanU, envU): baseline / hypercapnia averaging windows.
     - CO2 waveform plot: two manually-picked true end-tidal CO2 points. */

const CVR_TCD_IDS = ['cvrPlot1', 'cvrPlot2'];   // meanU, envU
const CVR_CO2_ID  = 'cvrPlot3';                 // CO2 waveform
const cvrChartFor = (id) => ({
  cvrPlot1: cvrCharts.chart1, cvrPlot2: cvrCharts.chart2, cvrPlot3: cvrCharts.chart3,
}[id]);

// Track the two CO2 point values so the readout can show delta CO2 live.
let cvrCo2Base = null, cvrCo2Hyp = null;

function updateCvrCo2Readout() {
  $('cvrCo2BaseVal').textContent = cvrCo2Base == null ? '—' : cvrCo2Base.toFixed(2) + ' mmHg';
  $('cvrCo2HypVal').textContent  = cvrCo2Hyp == null ? '—' : cvrCo2Hyp.toFixed(2) + ' mmHg';
  $('cvrCo2Delta').textContent   =
    (cvrCo2Base == null || cvrCo2Hyp == null) ? '—' : (cvrCo2Hyp - cvrCo2Base).toFixed(2) + ' mmHg';
}

function armClick(mode, ids, msg) {
  clickMode = mode;
  toast(msg, 'info');
  appendLog('CVR: ' + msg);
  ids.forEach(id => $(id).parentElement.classList.add('clickable'));
}
function disarmClick() {
  clickMode = null;
  [...CVR_TCD_IDS, CVR_CO2_ID].forEach(id => $(id).parentElement.classList.remove('clickable'));
}

$('cvrSelectBaseBtn').addEventListener('click',
  () => armClick('cvr_base', CVR_TCD_IDS, 'Click a TCD plot to set the baseline window start'));
$('cvrSelectHypBtn').addEventListener('click',
  () => armClick('cvr_hyp', CVR_TCD_IDS, 'Click a TCD plot to set the hypercapnia window start'));
$('cvrCo2BaseBtn').addEventListener('click',
  () => armClick('cvr_co2_base', [CVR_CO2_ID], 'Click the true end-tidal CO2 point for BASELINE on the CO2 waveform'));
$('cvrCo2HypBtn').addEventListener('click',
  () => armClick('cvr_co2_hyp', [CVR_CO2_ID], 'Click the true end-tidal CO2 point for HYPERCAPNIA on the CO2 waveform'));

// ── TCD plots: baseline / hypercapnia window selection ──
CVR_TCD_IDS.forEach(id => {
  $(id).addEventListener('click', async (e) => {
    if (clickMode !== 'cvr_base' && clickMode !== 'cvr_hyp') return;
    const mode = clickMode;
    const clickX = cvrCharts.getClickX(cvrChartFor(id), e);
    disarmClick();
    showLoading();
    try {
      if (mode === 'cvr_base') {
        const sel = await api('/api/cvr/select_baseline', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ start_time: clickX }),
        });
        cvrCharts.addBaseline(sel);
        appendLog(`CVR: Baseline window t=${sel.start_time.toFixed(1)}..${sel.end_time.toFixed(1)}`);
        toast('Baseline window selected', 'success');
      } else {
        const sel = await api('/api/cvr/select_hypercapnia', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ start_time: clickX }),
        });
        cvrCharts.addHypercapnia(sel);
        appendLog(`CVR: Hypercapnia window t=${sel.start_time.toFixed(1)}..${sel.end_time.toFixed(1)}`);
        toast('Hypercapnia window selected', 'success');
      }
    } catch (err) {
      toast(err.message, 'error');
      appendLog(`CVR ${mode} error: ` + err.message);
    }
    hideLoading();
  });
});

// ── CO2 waveform plot: pick the two end-tidal CO2 points ──
$(CVR_CO2_ID).addEventListener('click', async (e) => {
  if (clickMode !== 'cvr_co2_base' && clickMode !== 'cvr_co2_hyp') return;
  const which = clickMode === 'cvr_co2_base' ? 'baseline' : 'hypercapnia';
  const clickX = cvrCharts.getClickX(cvrCharts.chart3, e);
  disarmClick();
  showLoading();
  try {
    const r = await api('/api/cvr/select_co2_point', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ which, time: clickX }),
    });
    cvrCharts.addCo2Point(which, r.time, r.value);
    if (which === 'baseline') cvrCo2Base = r.value; else cvrCo2Hyp = r.value;
    updateCvrCo2Readout();
    appendLog(`CVR: ${which} CO2 = ${r.value.toFixed(2)} mmHg at t=${r.time.toFixed(1)}`);
    toast(`${which === 'baseline' ? 'Baseline' : 'Hypercapnia'} CO2 = ${r.value.toFixed(2)} mmHg`, 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('CVR CO2-point error: ' + err.message);
  }
  hideLoading();
});


/* ═══════════════════════ CVR — CLEAR ═══════════════════════ */

$('cvrClearBtn').addEventListener('click', async () => {
  try {
    await api('/api/cvr/clear', { method: 'POST' });
    cvrCharts.clearOverlays();
    $('cvrResultBox').classList.add('hidden');
    cvrCo2Base = cvrCo2Hyp = null;
    updateCvrCo2Readout();
    disarmClick();
    appendLog('CVR: Selections cleared.');
    toast('Selections cleared', 'info');
  } catch (err) { toast(err.message, 'error'); }
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

const ALL_PLOT_IDS = ['caPlot1', 'caPlot2', 'cvrPlot1', 'cvrPlot2',
                      'cvrPlot3', 'piPlot', 'piPlotAbp'];

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
    ALL_PLOT_IDS.forEach(id => {
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

/* A deletion is on the shared server-side signal, so it already affects every
   tab's calculations. Echo the visual gap onto the SAME signal's plot on the
   other tabs too, so the data looks consistent everywhere at a glance. */
function broadcastNaN(originTab, signal, rect) {
  const controllers = { ca: caCharts, cvr: cvrCharts, pi: piChart };
  for (const [tab, ctrl] of Object.entries(controllers)) {
    if (tab === originTab) continue;
    if (ctrl && typeof ctrl.applyNaNToSignal === 'function') {
      ctrl.applyNaNToSignal(signal, rect);
    }
  }
}

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
    // Update this chart locally so the user sees the gap immediately, then
    // mirror it onto the same signal on the other tabs.
    charts.applyNaNLocally();
    broadcastNaN(tab, ab.signal, ab.rect);
    brushState[tab].hasBrush = false;
    updateBrushButtons(tab);
    appendLog(`${tab.toUpperCase()}: Replaced ${res.changed} sample(s) in ${ab.signal} with NaN (applied across all tabs).`);
    toast(`Replaced ${res.changed} sample(s) with NaN`, 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog(`${tab.toUpperCase()} NaN-replace error: ${err.message}`);
  }
  hideLoading();
}

$('caNanBtn').addEventListener('click',  () => applyNaN('ca'));
$('cvrNanBtn').addEventListener('click', () => applyNaN('cvr'));

/* ═══════════════════════ NAV CONTROLS (Y-zoom + X-pan) ═══════════════════════
   Wired identically on every tab. Y-zoom leaves X untouched; pan slides the
   window left/right keeping its width (the zoom level). */
const Y_ZOOM_IN = 0.8;    // shrink Y range to 80% → beats look ~25% taller
const Y_ZOOM_OUT = 1.25;  // inverse
const PAN_FRACTION = 0.25;

function wireNavControls(prefix, controller) {
  $(`${prefix}YIn`).addEventListener('click',  () => controller.zoomY(Y_ZOOM_IN));
  $(`${prefix}YOut`).addEventListener('click', () => controller.zoomY(Y_ZOOM_OUT));
  $(`${prefix}YReset`).addEventListener('click', () => controller.resetY());
  $(`${prefix}PanL`).addEventListener('click', () => controller.panX(-PAN_FRACTION));
  $(`${prefix}PanR`).addEventListener('click', () => controller.panX(PAN_FRACTION));
}

wireNavControls('ca', caCharts);
wireNavControls('cvr', cvrCharts);


/* ═══════════════════════════════════════════════════════════════
   PI TAB — beat-epoch selection (port of PI.m)
   ═══════════════════════════════════════════════════════════════ */

let piEpochs = [];          // working selections, in insertion order
let piTraceTimer = null;

const fmt = (v, d = 2) => (v == null || !Number.isFinite(v)) ? '—' : v.toFixed(d);

/* ── trace resolution follows the zoom level ── */
function schedulePITraceRefresh() {
  clearTimeout(piTraceTimer);
  piTraceTimer = setTimeout(refreshPITrace, 150);
}

async function refreshPITrace() {
  if (!sessionData) return;
  const { min, max } = piChart.getXRange();
  try {
    const d = await api(`/api/pi/trace?start=${min}&end=${max}`);
    piChart.setTrace(d.time, d.env_u, d.abp);
    $('piZoomHint').textContent = d.full_res
      ? `full resolution — ${d.time.length.toLocaleString()} samples · drag to brush`
      : `1 in ${d.step} samples shown · zoom in to brush individual beats`;
  } catch (err) {
    appendLog('PI trace error: ' + err.message);
  }
}

piChart.onViewChange = schedulePITraceRefresh;

piChart.onBrushChange = (info) => {
  const has = !!info.hasBrush;
  $('piNativeBtn').disabled = !has;
  $('piArtificialBtn').disabled = !has;
  $('piBrushClearBtn').disabled = !has;
};

/* ── render server state into chart, counters and table ── */
function renderPI(payload) {
  piEpochs = payload.epochs || [];
  // setEpochs also drops any "all speeds" overlay, so a mutation always
  // returns the plot to the working selections it is about to redraw.
  piChart.setEpochs(piEpochs);

  const s = payload.summary || {};
  $('piNativeCount').textContent = s.n_native ?? 0;
  $('piArtificialCount').textContent = s.n_artificial ?? 0;
  $('piNativePI').textContent = 'PI ' + fmt(s.mean_pi_native);
  $('piArtificialPI').textContent = 'PI ' + fmt(s.mean_pi_artificial);

  if (payload.speed != null && document.activeElement !== $('piSpeed')) {
    $('piSpeed').value = payload.speed;
  }
  if (payload.vessel && document.activeElement !== $('piVessel')) {
    $('piVessel').value = payload.vessel;
  }

  $('piUndoBtn').disabled = piEpochs.length === 0;
  renderPIEpochTable();
}

function renderPIEpochTable() {
  const body = $('piEpochBody');
  body.innerHTML = '';
  if (!piEpochs.length) {
    body.innerHTML = '<tr class="epoch-empty"><td colspan="9">No epochs selected yet.</td></tr>';
    $('piEpochAll').checked = false;
    $('piDeselectBtn').disabled = true;
    return;
  }
  for (const e of piEpochs) {
    const tr = document.createElement('tr');
    tr.className = e.type === 'native' ? 'row-native' : 'row-artificial';
    const label = (e.type === 'native' ? 'Native #' : 'Artificial #') + e.ordinal;
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.dataset.id = e.id;
    cb.addEventListener('change', updatePIDeselectState);

    const cells = [null, label, fmt(e.t_start), fmt(e.t_end),
                   fmt(e.max), fmt(e.min), fmt(e.mean), fmt(e.pw, 3), fmt(e.pi, 3)];
    cells.forEach((text, i) => {
      const td = document.createElement('td');
      if (i === 0) td.appendChild(cb); else td.textContent = text;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  }
  $('piEpochAll').checked = false;
  updatePIDeselectState();
}

function checkedEpochIds() {
  return [...$('piEpochBody').querySelectorAll('input[type="checkbox"]:checked')]
    .map(cb => parseInt(cb.dataset.id, 10));
}

function updatePIDeselectState() {
  $('piDeselectBtn').disabled = checkedEpochIds().length === 0;
}

$('piEpochAll').addEventListener('change', (e) => {
  $('piEpochBody').querySelectorAll('input[type="checkbox"]')
    .forEach(cb => { cb.checked = e.target.checked; });
  updatePIDeselectState();
});

/* ── first-load setup ── */
async function initPITab() {
  $('piVessel').value = $('vesselSelect').value;
  piChart.setBrushMode(true);
  $('piBrushBtn').textContent = 'Brush: ON';
  $('piBrushBtn').classList.add('btn-primary');
  ['piNativeBtn', 'piArtificialBtn', 'piBrushClearBtn'].forEach(id => { $(id).disabled = true; });

  try {
    await api('/api/pi/meta', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speed: $('piSpeed').value, vessel: $('piVessel').value }),
    });
    renderPI(await api('/api/pi/state'));
  } catch (err) {
    appendLog('PI init error: ' + err.message);
  }
  await refreshPITrace();
}

/* ── metadata fields ── */
async function pushPIMeta() {
  try {
    await api('/api/pi/meta', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speed: $('piSpeed').value, vessel: $('piVessel').value }),
    });
  } catch (err) { appendLog('PI meta error: ' + err.message); }
}
$('piSpeed').addEventListener('change', pushPIMeta);
$('piVessel').addEventListener('change', pushPIMeta);

/* ── brush toggle ── */
$('piBrushBtn').addEventListener('click', () => {
  const on = !piChart.chart.$brush.active;
  piChart.setBrushMode(on);
  $('piBrushBtn').textContent = 'Brush: ' + (on ? 'ON' : 'OFF');
  $('piBrushBtn').classList.toggle('btn-primary', on);
  if (!on) {
    piChart.clearBrush();
    ['piNativeBtn', 'piArtificialBtn', 'piBrushClearBtn'].forEach(id => { $(id).disabled = true; });
  }
  appendLog(`PI: Brush mode ${on ? 'ON' : 'OFF'}.`);
});

$('piBrushClearBtn').addEventListener('click', () => piChart.clearBrush());

/* ── commit the brushed rectangle as an epoch ── */
async function piSelect(kind) {
  const ab = piChart.activeBrush();
  if (!ab) { toast('Brush a region on the plot first', 'info'); return; }
  showLoading();
  try {
    const payload = await api('/api/pi/select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: kind, rect: ab.rect }),
    });
    piChart.clearBrush();
    renderPI(payload);
    const last = piEpochs[piEpochs.length - 1];
    appendLog(`PI: ${kind} epoch #${last.ordinal} — PI = ${fmt(last.pi, 3)}, PW = ${fmt(last.pw, 3)} s`);
    toast(`${kind === 'native' ? 'Native' : 'Artificial'} epoch added (PI = ${fmt(last.pi, 3)})`, 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('PI select error: ' + err.message);
  }
  hideLoading();
}

$('piNativeBtn').addEventListener('click', () => piSelect('native'));
$('piArtificialBtn').addEventListener('click', () => piSelect('artificial'));

/* ── auto-select artificial: click a start point, then march right ── */
$('piAutoBtn').addEventListener('click', () => {
  clickMode = 'pi_auto';
  $('piPlot').parentElement.classList.add('clickable');
  toast('Click the first artificial beat (e.g. peak of the first decel)', 'info');
  appendLog('PI: Click the plot to set the auto-select start point.');
});

$('piPlot').addEventListener('click', async (e) => {
  if (clickMode !== 'pi_auto') return;
  clickMode = null;
  $('piPlot').parentElement.classList.remove('clickable');

  const startX = piChart.getClickX(e);
  const { min, max } = piChart.getXRange();
  showLoading();
  try {
    const payload = await api('/api/pi/auto_select', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // Cadence (2 s) and window (−0.15 s / +0.20 s) are fixed server-side.
      body: JSON.stringify({ start_time: startX, x_min: min, x_max: max }),
    });
    renderPI(payload);
    appendLog(`PI: Auto-selected ${payload.added} artificial epoch(s) from t=${startX.toFixed(1)} to t=${max.toFixed(1)}.`);
    toast(`Auto-selected ${payload.added} artificial epoch(s)`, 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('PI auto-select error: ' + err.message);
  }
  hideLoading();
});

/* ── undo / deselect / clear ── */
$('piUndoBtn').addEventListener('click', async () => {
  try {
    renderPI(await api('/api/pi/undo', { method: 'POST' }));
    appendLog('PI: Undid last selection.');
    toast('Last selection undone', 'info');
  } catch (err) { toast(err.message, 'error'); }
});

$('piDeselectBtn').addEventListener('click', async () => {
  const ids = checkedEpochIds();
  if (!ids.length) return;
  try {
    renderPI(await api('/api/pi/deselect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    }));
    appendLog(`PI: Removed ${ids.length} epoch(s).`);
    toast(`Removed ${ids.length} epoch(s)`, 'info');
  } catch (err) { toast(err.message, 'error'); }
});

$('piClearBtn').addEventListener('click', async () => {
  if (piEpochs.length && !confirm(`Clear all ${piEpochs.length} selections for this speed?\n\nThe saved session on disk is kept — use "Load Speed…" to restore them.`)) return;
  try {
    renderPI(await api('/api/pi/clear', { method: 'POST' }));
    appendLog('PI: Cleared all selections.');
    toast('Selections cleared', 'info');
  } catch (err) { toast(err.message, 'error'); }
});

/* ── zoom ── */
$('piZoomBtn').addEventListener('click', () => {
  const mode = $('piZoomMenu').value;
  if (mode === 'home') piChart.resetZoom();
  else if (mode === 'scale30') piChart.zoomToRange(piChart.getXRange().min, 30);
  else if (mode === 'scale5') piChart.zoomToRange(piChart.getXRange().min, 300);
});

wireNavControls('pi', piChart);

/* ── saved per-speed sessions ── */
function openSpeedModal(sessions) {
  const list = $('piSpeedList');
  list.innerHTML = '';
  sessions.forEach(s => {
    const btn = document.createElement('button');
    btn.innerHTML = `<strong>${s.speed ? 'Speed ' + s.speed : '(no speed label)'}</strong>` +
      `<span class="speed-meta">${s.n_native} native · ${s.n_artificial} artificial · ${s.filename}</span>`;
    btn.addEventListener('click', () => { closeSpeedModal(); loadPISession(s.filename); });
    list.appendChild(btn);
  });
  $('piSpeedModal').classList.add('active');
}
function closeSpeedModal() { $('piSpeedModal').classList.remove('active'); }
$('piSpeedCancel').addEventListener('click', closeSpeedModal);
$('piSpeedModal').addEventListener('click', (e) => {
  if (e.target === $('piSpeedModal')) closeSpeedModal();
});

async function loadPISession(filename) {
  showLoading();
  try {
    renderPI(await api('/api/pi/load_session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename }),
    }));
    appendLog(`PI: Loaded saved session ${filename}.`);
    toast('Saved selections loaded', 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('PI load error: ' + err.message);
  }
  hideLoading();
}

$('piLoadSpeedBtn').addEventListener('click', async () => {
  try {
    const { sessions } = await api('/api/pi/sessions');
    if (!sessions.length) { toast('No saved sessions for this recording', 'info'); return; }
    openSpeedModal(sessions);
  } catch (err) { toast(err.message, 'error'); }
});

$('piLoadAllBtn').addEventListener('click', async () => {
  showLoading();
  try {
    const { speeds } = await api('/api/pi/load_all');
    piChart.setAllSpeeds(speeds);
    const totN = speeds.reduce((a, s) => a + s.n_native, 0);
    const totA = speeds.reduce((a, s) => a + s.n_artificial, 0);
    appendLog(`PI: Overlaid ${speeds.length} speed session(s) — ${totN} native, ${totA} artificial (read-only view).`);
    toast(`${speeds.length} speeds overlaid — read-only view`, 'info');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('PI load-all error: ' + err.message);
  }
  hideLoading();
});

/* ── export ── */
async function piExport() {
  const form = new FormData();
  form.append('patient_id', $('patientId').value || '');
  form.append('speed', $('piSpeed').value || '');
  form.append('vessel', $('piVessel').value || '');
  const wb = $('piWorkbook').files[0];
  if (wb) form.append('workbook', wb);

  const result = await api('/api/pi/export', { method: 'POST', body: form });
  const a = document.createElement('a');
  a.href = '/api/download/' + result.filename;
  a.download = result.filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  appendLog(`PI: Exported ${result.filename} ` +
            `(${result.summary.n_native} native, ${result.summary.n_artificial} artificial).`);
  return result;
}

$('piExportBtn').addEventListener('click', async () => {
  showLoading();
  try {
    await piExport();
    toast('PI Demographics exported & downloaded', 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('PI export error: ' + err.message);
  }
  hideLoading();
});

/* ── Save & Next Speed: export this speed, then start a clean one ── */
$('piNextSpeedBtn').addEventListener('click', async () => {
  showLoading();
  try {
    await piExport();
  } catch (err) {
    hideLoading();
    toast(err.message, 'error');
    appendLog('PI export error: ' + err.message);
    return;
  }
  hideLoading();

  // Cancelling the prompt leaves the selections intact, as PI.m does.
  const next = prompt('Enter the next speed value:', '');
  if (next == null || !next.trim()) {
    appendLog('PI: Next speed cancelled — selections kept.');
    return;
  }

  showLoading();
  try {
    const payload = await api('/api/pi/next_speed', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ speed: next.trim() }),
    });
    renderPI(payload);
    $('piSpeed').value = payload.speed;
    appendLog(`PI: Ready for speed ${payload.speed}.`);

    if (payload.existing_session &&
        confirm(`Found saved selections for speed ${payload.speed}. Load them?`)) {
      await loadPISession(payload.existing_session);
    } else {
      toast(`Ready for speed ${payload.speed}`, 'success');
    }
  } catch (err) {
    toast(err.message, 'error');
    appendLog('PI next-speed error: ' + err.message);
  }
  hideLoading();
});


/* ═══════════════════════════════════════════════════════════════
   CA/CVR SESSION/SPEED — tag results, move on, export the whole
   study once (master + per-session workbooks + JSON, zipped).
   ═══════════════════════════════════════════════════════════════ */

function syncCacvrLabels(label) {
  if (document.activeElement !== $('caSessLabel')) $('caSessLabel').value = label;
  if (document.activeElement !== $('cvrSessLabel')) $('cvrSessLabel').value = label;
}

async function pushCacvrLabel(label) {
  try {
    const r = await api('/api/cacvr/meta', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label }),
    });
    syncCacvrLabels(r.label);
  } catch (err) { appendLog('CA/CVR meta error: ' + err.message); }
}

function _pickVessel(map) {
  map = map || {};
  const v = $('vesselSelect').value;
  if (map[v]) return [v, map[v]];
  const k = Object.keys(map)[0];
  return k ? [k, map[k]] : [null, null];
}

/* Populate the CA + CVR result boxes from a session summary (used after
   loading a saved session). */
function renderCacvrResults(summary) {
  summary = summary || {};
  const [cv, ca] = _pickVessel(summary.ca);
  if (ca) {
    const mx = ca.final_mx, mfv = ca.mean_mfv;
    $('caResultValue').textContent = `${(mx == null || !Number.isFinite(mx)) ? '—' : mx.toFixed(4)} (${cv})`;
    $('caMeanMfv').textContent = `${(mfv == null || !Number.isFinite(mfv)) ? '—' : mfv.toFixed(2)} (${cv})`;
    $('caResultBox').classList.remove('hidden');
  } else {
    $('caResultBox').classList.add('hidden');
  }
  const [vv, cvr] = _pickVessel(summary.cvr);
  if (cvr) {
    $('cvrMCVR').textContent = (cvr.mcvr == null || !Number.isFinite(cvr.mcvr)) ? '—' : cvr.mcvr.toFixed(4);
    $('cvrWCVR').textContent = (cvr.wcvr == null || !Number.isFinite(cvr.wcvr)) ? '—' : cvr.wcvr.toFixed(4);
    $('cvrResultBox').classList.remove('hidden');
  } else {
    $('cvrResultBox').classList.add('hidden');
  }
}

/* Redraw the shaded selection windows and CO2 points from a loaded session. */
function redrawCacvrSelections(sel) {
  caCharts.clearSelection();
  cvrCharts.clearOverlays();
  cvrCo2Base = cvrCo2Hyp = null;
  if (!sel) { updateCvrCo2Readout(); return; }
  const box = (r) => ({ start_time: r.start, end_time: r.end });
  if (sel.ca) caCharts.addSelection(box(sel.ca));
  if (sel.cvr_baseline) cvrCharts.addBaseline(box(sel.cvr_baseline));
  if (sel.cvr_hypercapnia) cvrCharts.addHypercapnia(box(sel.cvr_hypercapnia));
  if (sel.cvr_co2_baseline) {
    cvrCharts.addCo2Point('baseline', sel.cvr_co2_baseline.time, sel.cvr_co2_baseline.value);
    cvrCo2Base = sel.cvr_co2_baseline.value;
  }
  if (sel.cvr_co2_hypercapnia) {
    cvrCharts.addCo2Point('hypercapnia', sel.cvr_co2_hypercapnia.time, sel.cvr_co2_hypercapnia.value);
    cvrCo2Hyp = sel.cvr_co2_hypercapnia.value;
  }
  updateCvrCo2Readout();
}

async function initCacvrSession() {
  try {
    const st = await api('/api/cacvr/state');
    // Default the label to the header Session on first load.
    const label = st.label || $('sessionId').value || '';
    syncCacvrLabels(label);
    if (label !== st.label) await pushCacvrLabel(label);
    renderCacvrResults(st.summary);
    redrawCacvrSelections(st.selections);
  } catch (err) {
    appendLog('CA/CVR session init error: ' + err.message);
  }
}

['caSessLabel', 'cvrSessLabel'].forEach(id => {
  $(id).addEventListener('change', () => {
    const label = $(id).value.trim();
    syncCacvrLabels(label);
    pushCacvrLabel(label);
    appendLog(`CA/CVR: session/speed label set to "${label}".`);
  });
});

/* ── load-session picker modal ── */
function openCacvrModal(sessions) {
  const list = $('cacvrSessList');
  list.innerHTML = '';
  sessions.forEach(s => {
    const btn = document.createElement('button');
    const ca = s.ca_vessels.length ? 'CA ' + s.ca_vessels.join('/') : '';
    const cvr = s.cvr_vessels.length ? 'CVR ' + s.cvr_vessels.join('/') : '';
    const bits = [ca, cvr].filter(Boolean).join(' · ') || 'no results';
    btn.innerHTML = `<strong>${s.label ? 'Session ' + s.label : '(no label)'}</strong>` +
      `<span class="speed-meta">${bits} · ${s.filename}</span>`;
    btn.addEventListener('click', () => { closeCacvrModal(); loadCacvrSession(s.filename); });
    list.appendChild(btn);
  });
  $('cacvrSessModal').classList.add('active');
}
function closeCacvrModal() { $('cacvrSessModal').classList.remove('active'); }
$('cacvrSessCancel').addEventListener('click', closeCacvrModal);
$('cacvrSessModal').addEventListener('click', (e) => {
  if (e.target === $('cacvrSessModal')) closeCacvrModal();
});

async function loadCacvrSession(filename) {
  showLoading();
  try {
    const r = await api('/api/cacvr/load_session', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename }),
    });
    syncCacvrLabels(r.label);
    renderCacvrResults(r.summary);
    redrawCacvrSelections(r.loaded_selections);
    appendLog(`CA/CVR: loaded session "${r.label}".`);
    toast('CA/CVR session loaded', 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('CA/CVR load error: ' + err.message);
  }
  hideLoading();
}

async function cacvrLoadSession() {
  try {
    const { sessions } = await api('/api/cacvr/sessions');
    if (!sessions.length) { toast('No saved CA/CVR sessions yet', 'info'); return; }
    openCacvrModal(sessions);
  } catch (err) { toast(err.message, 'error'); }
}
$('caLoadSessBtn').addEventListener('click', cacvrLoadSession);
$('cvrLoadSessBtn').addEventListener('click', cacvrLoadSession);

async function cacvrNextSession() {
  const next = prompt('Save this session/speed and start a new one.\nEnter the next session/speed label:', '');
  if (next == null || !next.trim()) {
    appendLog('CA/CVR: next session cancelled — nothing cleared.');
    return;
  }
  showLoading();
  try {
    const r = await api('/api/cacvr/next_speed', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: next.trim() }),
    });
    // Clear the working result boxes and selection overlays for the new label.
    syncCacvrLabels(r.label);
    $('caResultBox').classList.add('hidden');
    $('cvrResultBox').classList.add('hidden');
    caCharts.clearSelection();
    cvrCharts.clearOverlays();
    cvrCo2Base = cvrCo2Hyp = null;
    updateCvrCo2Readout();
    appendLog(`CA/CVR: saved previous session; ready for "${r.label}".`);
    if (r.existing_session &&
        confirm(`Session "${r.label}" already has saved results. Load them?`)) {
      await loadCacvrSession(r.existing_session);
    } else {
      toast(`Ready for session ${r.label}`, 'success');
    }
  } catch (err) {
    toast(err.message, 'error');
    appendLog('CA/CVR next-session error: ' + err.message);
  }
  hideLoading();
}
$('caNextSessBtn').addEventListener('click', cacvrNextSession);
$('cvrNextSessBtn').addEventListener('click', cacvrNextSession);

async function cacvrExportAll() {
  showLoading();
  try {
    const r = await api('/api/cacvr/export_all', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    const a = document.createElement('a');
    a.href = '/api/download/' + r.filename;
    a.download = r.filename;
    document.body.appendChild(a); a.click(); a.remove();
    appendLog(`CA/CVR: exported study bundle ${r.filename} (${r.n_sessions} session workbook(s) + master + JSON).`);
    toast(`Exported ${r.n_sessions} session(s) + master + JSON`, 'success');
  } catch (err) {
    toast(err.message, 'error');
    appendLog('CA/CVR export error: ' + err.message);
  }
  hideLoading();
}
$('caExportAllBtn').addEventListener('click', cacvrExportAll);
$('cvrExportAllBtn').addEventListener('click', cacvrExportAll);


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
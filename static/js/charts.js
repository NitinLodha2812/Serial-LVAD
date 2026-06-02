/* ═══════════════════════════════════════════════════════════════
   charts.js
   Chart.js wrappers for CA and CVR plots.
   Scroll-wheel to zoom, Shift+drag to pan, plain click to select.
   ═══════════════════════════════════════════════════════════════ */

const COLORS = {
  envU:     '#1A8A7D',
  abp:      '#2C2A26',
  meanU:    '#2980B9',
  etco2:    '#7A7568',
  co2wave:  '#D35400',
  selCA:    '#2980B9',
  selBase:  '#27AE60',
  selHyp:   '#8E44AD',
  selCo2W:  '#F39C12',   // CO2 search window
  peak:     '#C0392B',
  mark:     'rgba(192, 57, 43, .55)',
};

/* ── helper: build {x,y} data from two arrays, skipping nulls ── */
function xyData(timeArr, valArr) {
  const pts = [];
  for (let i = 0; i < timeArr.length; i++) {
    if (timeArr[i] != null && valArr[i] != null) {
      pts.push({ x: timeArr[i], y: valArr[i] });
    }
  }
  return pts;
}

/* ══════════════════ BRUSH PLUGIN (rectangle draw) ══════════════════ */
/* Draws the in-progress brush rectangle stored on `chart.$brush`. One
   instance registered globally; it ignores charts that aren't brushing. */
const brushDrawPlugin = {
  id: 'brushOverlay',
  afterDraw(chart) {
    const b = chart.$brush;
    if (!b || !b.dragging || !b.startPx || !b.endPx) return;
    const a = b.startPx, c = b.endPx;
    const x = Math.min(a.x, c.x), y = Math.min(a.y, c.y);
    const w = Math.abs(c.x - a.x), h = Math.abs(c.y - a.y);
    if (w < 1 && h < 1) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.fillStyle = 'rgba(231, 76, 60, 0.15)';
    ctx.strokeStyle = 'rgba(231, 76, 60, 0.85)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x, y, w, h);
    ctx.restore();
  },
};
if (typeof Chart !== 'undefined') Chart.register(brushDrawPlugin);

/* ══════════════════ BRUSH CONTROLLER ══════════════════ */
/* Attach drag-rectangle brushing to a single Chart.js chart.
   `signal` is the backend name (mean_u / env_u / etco2 / abp).
   `onChange(state)` fires whenever the brushed-points set changes,
   so the host app can enable/disable "Replace with NaN" buttons. */
function attachBrush(chart, signal, onChange) {
  if (!chart || chart.$brush) return;
  chart.$brush = {
    signal,
    active: false,        // brush-mode toggle
    dragging: false,
    startPx: null,        // canvas pixels at mousedown
    endPx: null,
    rect: null,           // data-coords {x_min, x_max, y_min, y_max}
    onChange: onChange || (() => {}),
  };
  const canvas = chart.canvas;

  function mousedown(e) {
    if (!chart.$brush.active) return;
    if (e.button !== 0) return;            // left click only
    e.preventDefault();
    const r = canvas.getBoundingClientRect();
    chart.$brush.dragging = true;
    chart.$brush.startPx = { x: e.clientX - r.left, y: e.clientY - r.top };
    chart.$brush.endPx   = { ...chart.$brush.startPx };
  }
  function mousemove(e) {
    if (!chart.$brush.dragging) return;
    const r = canvas.getBoundingClientRect();
    chart.$brush.endPx = { x: e.clientX - r.left, y: e.clientY - r.top };
    chart.draw();
  }
  function mouseup(e) {
    if (!chart.$brush.dragging) return;
    chart.$brush.dragging = false;
    const a = chart.$brush.startPx, c = chart.$brush.endPx;
    if (!a || !c) return;
    if (Math.abs(c.x - a.x) < 3 && Math.abs(c.y - a.y) < 3) {
      // click, not a drag — ignore
      chart.draw();
      return;
    }
    const xs = chart.scales.x, ys = chart.scales.y;
    let x_min = xs.getValueForPixel(Math.min(a.x, c.x));
    let x_max = xs.getValueForPixel(Math.max(a.x, c.x));
    let y_min = ys.getValueForPixel(Math.max(a.y, c.y));   // pixel-y inverted
    let y_max = ys.getValueForPixel(Math.min(a.y, c.y));
    if (x_max < x_min) [x_min, x_max] = [x_max, x_min];
    if (y_max < y_min) [y_min, y_max] = [y_max, y_min];
    chart.$brush.rect = { x_min, x_max, y_min, y_max };
    _refreshBrushedOverlay(chart);
    chart.$brush.onChange({ hasBrush: true, signal: chart.$brush.signal, rect: chart.$brush.rect });
  }
  function mouseleave() {
    if (chart.$brush.dragging) {
      chart.$brush.dragging = false;
      chart.draw();
    }
  }

  canvas.addEventListener('mousedown', mousedown);
  canvas.addEventListener('mousemove', mousemove);
  canvas.addEventListener('mouseup',   mouseup);
  canvas.addEventListener('mouseleave', mouseleave);
}

/* Rebuild the red-dot dataset showing which 'main' points fall in the
   current brush rectangle. Only the points from the chart's primary
   (non-overlay) line are eligible — selection/baseline/hypercapnia
   overlays are ignored. */
function _refreshBrushedOverlay(chart) {
  if (!chart || !chart.$brush) return;
  // Strip prior overlay
  chart.data.datasets = chart.data.datasets.filter(d => d.label !== '_brushed');
  const rect = chart.$brush.rect;
  if (!rect) { chart.update('none'); return; }
  // The chart's primary trace is whichever dataset was added first.
  const mainDs = chart.data.datasets[0];
  if (!mainDs || !Array.isArray(mainDs.data)) { chart.update('none'); return; }
  const inside = mainDs.data.filter(p =>
    p && p.y != null &&
    p.x >= rect.x_min && p.x <= rect.x_max &&
    p.y >= rect.y_min && p.y <= rect.y_max);
  chart.data.datasets.push({
    label: '_brushed',
    data: inside,
    pointRadius: 4,
    pointHoverRadius: 4,
    pointBackgroundColor: '#E74C3C',
    pointBorderColor: '#E74C3C',
    borderColor: 'rgba(231,76,60,0)',
    showLine: false,
    order: -1,     // drawn on top
  });
  chart.update('none');
}

/* Toggle brush mode on a chart. When ON, zoom-wheel & shift-pan are
   disabled so the drag interaction is unambiguous. */
function setChartBrushMode(chart, on) {
  if (!chart || !chart.$brush) return;
  chart.$brush.active = !!on;
  const z = chart.options?.plugins?.zoom;
  if (z) {
    z.zoom.wheel.enabled = !on;
    z.pan.enabled = !on;
  }
  chart.canvas.style.cursor = on ? 'crosshair' : '';
  chart.update('none');
}

function clearChartBrush(chart) {
  if (!chart || !chart.$brush) return;
  chart.$brush.rect = null;
  chart.$brush.startPx = chart.$brush.endPx = null;
  chart.data.datasets = chart.data.datasets.filter(d => d.label !== '_brushed');
  chart.update('none');
  chart.$brush.onChange({ hasBrush: false, signal: chart.$brush.signal, rect: null });
}

/* Locally apply NaN to the brushed points on the primary trace so the
   user sees the gap immediately. The backend has the authoritative copy. */
function applyNaNToChartLocally(chart) {
  if (!chart || !chart.$brush || !chart.$brush.rect) return 0;
  const rect = chart.$brush.rect;
  const mainDs = chart.data.datasets[0];
  if (!mainDs) return 0;
  let n = 0;
  for (const p of mainDs.data) {
    if (p && p.y != null &&
        p.x >= rect.x_min && p.x <= rect.x_max &&
        p.y >= rect.y_min && p.y <= rect.y_max) {
      p.y = null;   // null breaks the line at this x
      n++;
    }
  }
  clearChartBrush(chart);
  return n;
}

/* ── build annotation lines for marks ── */
function markAnnotations(labels, times, visible) {
  const ann = {};
  for (let i = 0; i < labels.length; i++) {
    if (times[i] == null) continue;
    ann['mark_' + i] = {
      type: 'line',
      xMin: times[i],
      xMax: times[i],
      borderColor: COLORS.mark,
      borderWidth: 1,
      borderDash: [4, 3],
      display: visible.has(i),
      label: {
        display: visible.has(i),
        content: labels[i],
        position: 'start',
        backgroundColor: 'rgba(192,57,43,.08)',
        color: '#C0392B',
        font: { size: 9 },
        padding: 3,
      },
    };
  }
  return ann;
}

/* ── common chart options factory ── */
function makeChartOptions(yLabel, annotations) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    parsing: false,
    normalized: true,
    elements: {
      point: { radius: 0, hitRadius: 0, hoverRadius: 0 },
      line: { borderWidth: 1.2, tension: 0 },
    },
    interaction: {
      mode: 'nearest',
      intersect: false,
    },
    plugins: {
      legend: { display: false },
      tooltip: { enabled: false },
      annotation: { annotations: annotations || {} },
      zoom: {
        zoom: {
          wheel: { enabled: true, speed: 0.08 },
          pinch: { enabled: true },
          mode: 'x',
        },
        pan: {
          enabled: true,
          mode: 'x',
          modifierKey: 'shift',
        },
      },
    },
    scales: {
      x: {
        type: 'linear',
        title: { display: true, text: 'Time (s)', font: { size: 11 } },
        ticks: { font: { size: 10 }, maxTicksLimit: 12 },
        grid: { color: 'rgba(0,0,0,.04)' },
      },
      y: {
        title: { display: true, text: yLabel, font: { size: 11 } },
        ticks: { font: { size: 10 }, maxTicksLimit: 8 },
        grid: { color: 'rgba(0,0,0,.04)' },
      },
    },
  };
}

/* ═══════════════════════════════════════════════════════════════
   CA Charts
   ═══════════════════════════════════════════════════════════════ */

class CACharts {
  constructor() {
    this.chart1 = null;
    this.chart2 = null;
    this.marksVisible = new Set();
    this.onBrushChange = () => {};   // host sets this to track brush state
  }

  init(data) {
    this.destroy();
    const envPts = xyData(data.time, data.env_u);
    const abpPts = xyData(data.time, data.abp);

    this.marksVisible = new Set(data.marks_labels.map((_, i) => i));
    const ann = markAnnotations(data.marks_labels, data.marks_times, this.marksVisible);

    this.chart1 = new Chart(document.getElementById('caPlot1'), {
      type: 'line',
      data: { datasets: [{ data: envPts, borderColor: COLORS.envU, label: 'envU' }] },
      options: makeChartOptions('cm/s²', { ...ann }),
    });

    this.chart2 = new Chart(document.getElementById('caPlot2'), {
      type: 'line',
      data: { datasets: [{ data: abpPts, borderColor: COLORS.abp, label: 'ABP' }] },
      options: makeChartOptions('mmHg', { ...ann }),
    });

    const onBrush = (active) => (s) => {
      // Only one chart may be brushed at a time — clear the others.
      if (s.hasBrush) {
        for (const c of [this.chart1, this.chart2]) {
          if (c && c !== active && c.$brush && c.$brush.rect) clearChartBrush(c);
        }
      }
      this.onBrushChange(s);
    };
    attachBrush(this.chart1, 'env_u', onBrush(this.chart1));
    attachBrush(this.chart2, 'abp',   onBrush(this.chart2));

    this._syncZoom();
    this.data = data;
  }

  // ── brush API used by app.js ──
  setBrushMode(on) {
    setChartBrushMode(this.chart1, on);
    setChartBrushMode(this.chart2, on);
  }
  activeBrush() {
    // Returns {chart, signal, rect} for whichever chart currently has a brush, or null.
    for (const c of [this.chart1, this.chart2]) {
      if (c && c.$brush && c.$brush.rect) return { chart: c, signal: c.$brush.signal, rect: c.$brush.rect };
    }
    return null;
  }
  clearBrush() {
    clearChartBrush(this.chart1);
    clearChartBrush(this.chart2);
  }
  applyNaNLocally() {
    const ab = this.activeBrush();
    if (!ab) return 0;
    return applyNaNToChartLocally(ab.chart);
  }

  _syncZoom() {
    const c1 = this.chart1, c2 = this.chart2;
    if (!c1 || !c2) return;
    const syncTo = (src, tgt) => {
      tgt.options.scales.x.min = src.scales.x.min;
      tgt.options.scales.x.max = src.scales.x.max;
      tgt.update('none');
    };
    c1.options.plugins.zoom.zoom.onZoomComplete = () => syncTo(c1, c2);
    c2.options.plugins.zoom.zoom.onZoomComplete = () => syncTo(c2, c1);
    c1.options.plugins.zoom.pan.onPanComplete = () => syncTo(c1, c2);
    c2.options.plugins.zoom.pan.onPanComplete = () => syncTo(c2, c1);
    c1.update('none');
    c2.update('none');
  }

  addSelection(selData) {
    // The selection mask is a *visual indicator only* — the MX calculation
    // runs on the underlying full-resolution samples stored server-side.
    // We render the mask as a shaded annotation box (not a duplicated line
    // trace), so the original signal trace is the only line on the plot and
    // there is no decimation mismatch between base and overlay.
    if (!this.chart1 || !this.chart2) return;
    [this.chart1, this.chart2].forEach(chart => {
      this._removeDataset(chart, 'selection');   // strip any old line-style overlay
      const ann = chart.options.plugins.annotation.annotations;
      ann.ca_selection = {
        type: 'box',
        xMin: selData.start_time,
        xMax: selData.end_time,
        backgroundColor: COLORS.selCA + '22',
        borderColor: COLORS.selCA,
        borderWidth: 1,
        drawTime: 'beforeDatasetsDraw',          // shade behind the trace
      };
      chart.update('none');
    });
  }

  clearSelection() {
    [this.chart1, this.chart2].forEach(chart => {
      if (!chart) return;
      this._removeDataset(chart, 'selection');
      const ann = chart.options.plugins.annotation.annotations;
      delete ann.ca_selection;
      chart.update('none');
    });
  }

  updateMarks(visibleSet) {
    this.marksVisible = visibleSet;
    [this.chart1, this.chart2].forEach(chart => {
      if (!chart) return;
      const ann = chart.options.plugins.annotation.annotations;
      for (const key in ann) {
        if (key.startsWith('mark_')) {
          const idx = parseInt(key.split('_')[1]);
          ann[key].display = visibleSet.has(idx);
          if (ann[key].label) ann[key].label.display = visibleSet.has(idx);
        }
      }
      chart.update('none');
    });
  }

  resetZoom() {
    if (this.chart1) this.chart1.resetZoom();
    if (this.chart2) this.chart2.resetZoom();
  }

  zoomToRange(start, duration) {
    [this.chart1, this.chart2].forEach(c => {
      if (!c) return;
      c.options.scales.x.min = start;
      c.options.scales.x.max = start + duration;
      c.update('none');
    });
  }

  getXRange() {
    if (!this.chart1) return { min: 0, max: 100 };
    return { min: this.chart1.scales.x.min, max: this.chart1.scales.x.max };
  }

  getClickX(chart, event) {
    const rect = chart.canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    return chart.scales.x.getValueForPixel(x);
  }

  _removeDataset(chart, label) {
    if (!chart) return;
    chart.data.datasets = chart.data.datasets.filter(d => d.label !== label);
  }

  destroy() {
    if (this.chart1) { this.chart1.destroy(); this.chart1 = null; }
    if (this.chart2) { this.chart2.destroy(); this.chart2 = null; }
  }
}


/* ═══════════════════════════════════════════════════════════════
   CVR Charts
   ═══════════════════════════════════════════════════════════════ */

class CVRCharts {
  constructor() {
    this.chart1 = null;
    this.chart2 = null;
    this.chart3 = null;
    this.chart4 = null;
    this.marksVisible = new Set();
    this.onBrushChange = () => {};
  }

  _allCharts() {
    return [this.chart1, this.chart2, this.chart3, this.chart4].filter(Boolean);
  }

  init(data) {
    this.destroy();
    const meanPts = xyData(data.time, data.mean_u);
    const envPts  = xyData(data.time, data.env_u);
    const etPts   = xyData(data.time, data.etco2);
    const co2Pts  = xyData(data.time, data.co2 || []);

    this.marksVisible = new Set(data.marks_labels.map((_, i) => i));
    const ann = markAnnotations(data.marks_labels, data.marks_times, this.marksVisible);

    this.chart1 = new Chart(document.getElementById('cvrPlot1'), {
      type: 'line',
      data: { datasets: [{ data: meanPts, borderColor: COLORS.meanU, label: 'main' }] },
      options: makeChartOptions('meanU', { ...ann }),
    });

    this.chart2 = new Chart(document.getElementById('cvrPlot2'), {
      type: 'line',
      data: { datasets: [{ data: envPts, borderColor: COLORS.envU, label: 'main' }] },
      options: makeChartOptions('envU', { ...ann }),
    });

    this.chart3 = new Chart(document.getElementById('cvrPlot3'), {
      type: 'line',
      data: { datasets: [{ data: etPts, borderColor: COLORS.etco2, label: 'main' }] },
      options: makeChartOptions('ETCO2', { ...ann }),
    });

    this.chart4 = new Chart(document.getElementById('cvrPlot4'), {
      type: 'line',
      data: { datasets: [{ data: co2Pts, borderColor: COLORS.co2wave, label: 'main' }] },
      options: makeChartOptions('CO2', { ...ann }),
    });

    const onBrush = (active) => (s) => {
      if (s.hasBrush) {
        for (const c of this._allCharts()) {
          if (c !== active && c.$brush && c.$brush.rect) clearChartBrush(c);
        }
      }
      this.onBrushChange(s);
    };
    attachBrush(this.chart1, 'mean_u', onBrush(this.chart1));
    attachBrush(this.chart2, 'env_u',  onBrush(this.chart2));
    attachBrush(this.chart3, 'etco2',  onBrush(this.chart3));
    attachBrush(this.chart4, 'co2',    onBrush(this.chart4));

    this._syncZoom();
    this.data = data;
  }

  // ── brush API used by app.js ──
  setBrushMode(on) {
    for (const c of this._allCharts()) setChartBrushMode(c, on);
  }
  activeBrush() {
    for (const c of this._allCharts()) {
      if (c.$brush && c.$brush.rect) return { chart: c, signal: c.$brush.signal, rect: c.$brush.rect };
    }
    return null;
  }
  clearBrush() {
    for (const c of this._allCharts()) clearChartBrush(c);
  }
  applyNaNLocally() {
    const ab = this.activeBrush();
    if (!ab) return 0;
    return applyNaNToChartLocally(ab.chart);
  }

  _syncZoom() {
    const charts = this._allCharts();
    charts.forEach(source => {
      const others = charts.filter(c => c !== source);
      source.options.plugins.zoom.zoom.onZoomComplete = () => {
        others.forEach(t => {
          t.options.scales.x.min = source.scales.x.min;
          t.options.scales.x.max = source.scales.x.max;
          t.update('none');
        });
      };
      source.options.plugins.zoom.pan.onPanComplete = () => {
        others.forEach(t => {
          t.options.scales.x.min = source.scales.x.min;
          t.options.scales.x.max = source.scales.x.max;
          t.update('none');
        });
      };
    });
    charts.forEach(c => c.update('none'));
  }

  // Render baseline / hypercapnia masks as translucent annotation boxes so
  // they shade only the time interval and don't introduce a second decimated
  // trace on top of the underlying signal. The CVR calculation reads the
  // full-resolution server-side selection, not these visual overlays.
  addBaseline(sel)    { this._addRegion('cvr_baseline',    sel, COLORS.selBase); }
  addHypercapnia(sel) { this._addRegion('cvr_hypercapnia', sel, COLORS.selHyp); }

  _addRegion(annKey, sel, color) {
    this._allCharts().forEach(chart => {
      // Strip any legacy line-style overlay
      const oldLabel = annKey === 'cvr_baseline' ? 'baseline' : 'hypercapnia';
      chart.data.datasets = chart.data.datasets.filter(d => d.label !== oldLabel);
      const ann = chart.options.plugins.annotation.annotations;
      ann[annKey] = {
        type: 'box',
        xMin: sel.start_time,
        xMax: sel.end_time,
        backgroundColor: color + '22',
        borderColor: color,
        borderWidth: 1,
        drawTime: 'beforeDatasetsDraw',
      };
      chart.update('none');
    });
  }

  // CO2 search window (gas-on → gas-off) — shaded across all 4 plots so the
  // operator can see at a glance what range peak-detect will use.
  addCo2Window(sel) {
    this._allCharts().forEach(chart => {
      const ann = chart.options.plugins.annotation.annotations;
      ann.cvr_co2_window = {
        type: 'box',
        xMin: sel.start_time,
        xMax: sel.end_time,
        backgroundColor: COLORS.selCo2W + '22',
        borderColor: COLORS.selCo2W,
        borderWidth: 1,
        borderDash: [5, 3],
        drawTime: 'beforeDatasetsDraw',
      };
      chart.update('none');
    });
  }
  clearCo2Window() {
    this._allCharts().forEach(chart => {
      const ann = chart.options.plugins.annotation.annotations;
      delete ann.cvr_co2_window;
      chart.update('none');
    });
  }

  addPeakMarker(peakTime, peakVal) {
    if (!this.chart3) return;
    this.chart3.data.datasets = this.chart3.data.datasets.filter(d => d.label !== 'peak');
    this.chart3.data.datasets.push({
      data: [{ x: peakTime, y: peakVal }],
      borderColor: COLORS.peak, backgroundColor: COLORS.peak,
      pointRadius: 7, pointStyle: 'rectRot', showLine: false, label: 'peak',
    });
    this.chart3.update('none');
  }

  clearOverlays() {
    this._removeOverlay('baseline');
    this._removeOverlay('hypercapnia');
    this._removeOverlay('peak');
    this._allCharts().forEach(c => {
      const ann = c.options.plugins.annotation.annotations;
      delete ann.cvr_baseline;
      delete ann.cvr_hypercapnia;
      delete ann.cvr_co2_window;
      c.update('none');
    });
  }

  _removeOverlay(name) {
    this._allCharts().forEach(c => {
      c.data.datasets = c.data.datasets.filter(d => d.label !== name);
      c.update('none');
    });
  }

  updateMarks(visibleSet) {
    this.marksVisible = visibleSet;
    this._allCharts().forEach(chart => {
      const ann = chart.options.plugins.annotation.annotations;
      for (const key in ann) {
        if (key.startsWith('mark_')) {
          const idx = parseInt(key.split('_')[1]);
          ann[key].display = visibleSet.has(idx);
          if (ann[key].label) ann[key].label.display = visibleSet.has(idx);
        }
      }
      chart.update('none');
    });
  }

  resetZoom() {
    this._allCharts().forEach(c => c.resetZoom());
  }

  zoomToRange(start, duration) {
    this._allCharts().forEach(c => {
      c.options.scales.x.min = start;
      c.options.scales.x.max = start + duration;
      c.update('none');
    });
  }

  getXRange() {
    if (!this.chart1) return { min: 0, max: 100 };
    return { min: this.chart1.scales.x.min, max: this.chart1.scales.x.max };
  }

  getClickX(chart, event) {
    const rect = chart.canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    return chart.scales.x.getValueForPixel(x);
  }

  destroy() {
    this._allCharts().forEach(c => c.destroy());
    this.chart1 = this.chart2 = this.chart3 = this.chart4 = null;
  }
}

window.CACharts = CACharts;
window.CVRCharts = CVRCharts;
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
  selCA:    '#2980B9',
  selBase:  '#27AE60',
  selHyp:   '#8E44AD',
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

    this._syncZoom();
    this.data = data;
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
    if (!this.chart1 || !this.chart2) return;
    this._removeDataset(this.chart1, 'selection');
    this._removeDataset(this.chart2, 'selection');

    const envPts = xyData(selData.time, selData.env);
    const abpPts = xyData(selData.time, selData.abp);

    this.chart1.data.datasets.push({
      data: envPts, borderColor: COLORS.selCA, backgroundColor: COLORS.selCA + '22',
      pointRadius: 0, showLine: true, borderWidth: 2.5, fill: true, label: 'selection',
    });
    this.chart2.data.datasets.push({
      data: abpPts, borderColor: COLORS.selCA, backgroundColor: COLORS.selCA + '22',
      pointRadius: 0, showLine: true, borderWidth: 2.5, fill: true, label: 'selection',
    });
    this.chart1.update('none');
    this.chart2.update('none');
  }

  clearSelection() {
    this._removeDataset(this.chart1, 'selection');
    this._removeDataset(this.chart2, 'selection');
    if (this.chart1) this.chart1.update('none');
    if (this.chart2) this.chart2.update('none');
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
    this.marksVisible = new Set();
  }

  init(data) {
    this.destroy();
    const meanPts = xyData(data.time, data.mean_u);
    const envPts  = xyData(data.time, data.env_u);
    const co2Pts  = xyData(data.time, data.etco2);

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
      data: { datasets: [{ data: co2Pts, borderColor: COLORS.etco2, label: 'main' }] },
      options: makeChartOptions('ETCO2', { ...ann }),
    });

    this._syncZoom();
    this.data = data;
  }

  _syncZoom() {
    const charts = [this.chart1, this.chart2, this.chart3].filter(Boolean);
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

  addBaseline(sel) {
    this._removeOverlay('baseline');
    const pairs = [
      [this.chart1, sel.time, sel.mean],
      [this.chart2, sel.time, sel.env],
      [this.chart3, sel.time, sel.co2],
    ];
    pairs.forEach(([chart, t, v]) => {
      if (!chart) return;
      chart.data.datasets.push({
        data: xyData(t, v), borderColor: COLORS.selBase, backgroundColor: COLORS.selBase + '22',
        pointRadius: 0, showLine: true, borderWidth: 2.5, fill: true, label: 'baseline',
      });
      chart.update('none');
    });
  }

  addHypercapnia(sel) {
    this._removeOverlay('hypercapnia');
    const pairs = [
      [this.chart1, sel.time, sel.mean],
      [this.chart2, sel.time, sel.env],
      [this.chart3, sel.time, sel.co2],
    ];
    pairs.forEach(([chart, t, v]) => {
      if (!chart) return;
      chart.data.datasets.push({
        data: xyData(t, v), borderColor: COLORS.selHyp, backgroundColor: COLORS.selHyp + '22',
        pointRadius: 0, showLine: true, borderWidth: 2.5, fill: true, label: 'hypercapnia',
      });
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
  }

  _removeOverlay(name) {
    [this.chart1, this.chart2, this.chart3].forEach(c => {
      if (!c) return;
      c.data.datasets = c.data.datasets.filter(d => d.label !== name);
      c.update('none');
    });
  }

  updateMarks(visibleSet) {
    this.marksVisible = visibleSet;
    [this.chart1, this.chart2, this.chart3].forEach(chart => {
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
    [this.chart1, this.chart2, this.chart3].forEach(c => { if (c) c.resetZoom(); });
  }

  zoomToRange(start, duration) {
    [this.chart1, this.chart2, this.chart3].forEach(c => {
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

  destroy() {
    [this.chart1, this.chart2, this.chart3].forEach(c => { if (c) c.destroy(); });
    this.chart1 = this.chart2 = this.chart3 = null;
  }
}

window.CACharts = CACharts;
window.CVRCharts = CVRCharts;
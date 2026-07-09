/* ============================================
   EEG Flanker Analysis — App Logic
   Wavelet-based pipeline with per-channel exclusion
   ============================================ */

const API = window.location.origin;
let currentResultId = null;
let currentData = null;              // last upload's full response
let uploadedSubjects = [];           // [{ result_id, filename }]

const SUBJECT_COLORS = [
    '#5eead4', '#f472b6', '#818cf8', '#fb923c', '#a3e635',
    '#38bdf8', '#e879f9', '#fbbf24', '#f87171', '#34d399',
];

// ── Plotly theme ──
const plotlyLayout = {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { family: 'JetBrains Mono, monospace', size: 11, color: '#6b7394' },
    margin: { t: 10, r: 20, b: 45, l: 55 },
    xaxis: {
        gridcolor: 'rgba(30,35,48,0.8)',
        zerolinecolor: 'rgba(94,234,212,0.2)',
        tickfont: { size: 10 },
        showspikes: true,
        spikemode: 'across',
        spikesnap: 'cursor',
        spikecolor: 'rgba(94,234,212,0.55)',
        spikethickness: 1,
        spikedash: 'dot',
    },
    yaxis: {
        gridcolor: 'rgba(30,35,48,0.8)',
        zerolinecolor: 'rgba(94,234,212,0.2)',
        tickfont: { size: 10 },
        showspikes: true,
        spikemode: 'across',
        spikesnap: 'cursor',
        spikecolor: 'rgba(94,234,212,0.55)',
        spikethickness: 1,
        spikedash: 'dot',
    },
    legend: {
        bgcolor: 'rgba(0,0,0,0)',
        font: { size: 10, color: '#6b7394' },
        orientation: 'h',
        x: 0.5, xanchor: 'center',
        y: 1.12,
    },
    hoverlabel: {
        bgcolor: '#12151c',
        bordercolor: '#2a3040',
        font: { family: 'JetBrains Mono', size: 11, color: '#d8dce6' },
    },
    hovermode: 'closest',
    hoverdistance: 50,
    spikedistance: -1,
};
const plotlyConfig = { displayModeBar: false, responsive: true };

const CON_COLOR = '#5eead4';
const INC_COLOR = '#f472b6';
const CON_COLOR_DIM = 'rgba(94,234,212,0.15)';
const INC_COLOR_DIM = 'rgba(244,114,182,0.15)';
const DIM_COLOR = 'rgba(160,160,180,0.35)';   // excluded trials

// ── Background wave animation ──
function initBgWave() {
    const canvas = document.getElementById('bg-wave');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    let w, h, t = 0;

    function resize() {
        w = canvas.width = window.innerWidth;
        h = canvas.height = window.innerHeight;
    }
    resize();
    window.addEventListener('resize', resize);

    function draw() {
        ctx.clearRect(0, 0, w, h);
        const lines = 4;
        for (let l = 0; l < lines; l++) {
            ctx.beginPath();
            ctx.strokeStyle = `rgba(94,234,212,${0.06 - l * 0.012})`;
            ctx.lineWidth = 1;
            const yBase = h * (0.3 + l * 0.15);
            const amp = 20 + l * 8;
            const freq = 0.003 - l * 0.0004;
            const speed = 0.008 + l * 0.003;
            for (let x = 0; x < w; x += 2) {
                const y = yBase + Math.sin(x * freq + t * speed) * amp
                    + Math.sin(x * freq * 2.3 + t * speed * 1.7) * (amp * 0.3);
                if (x === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            }
            ctx.stroke();
        }
        t++;
        requestAnimationFrame(draw);
    }
    draw();
}

// ── Upload ──
function initUpload() {
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');
    const progress = document.getElementById('upload-progress');
    const errorEl = document.getElementById('upload-error');

    dropZone.addEventListener('click', () => fileInput.click());
    dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('drag-over'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        const files = Array.from(e.dataTransfer.files).filter(f => f.name.endsWith('.txt'));
        if (files.length > 0) uploadFiles(files);
    });
    fileInput.addEventListener('change', () => {
        const files = Array.from(fileInput.files).filter(f => f.name.endsWith('.txt'));
        if (files.length > 0) uploadFiles(files);
    });

    async function uploadFiles(files) {
        errorEl.hidden = true;
        progress.hidden = false;
        const fill = progress.querySelector('.progress-fill');
        const progressText = progress.querySelector('.progress-text');
        fill.classList.add('indeterminate');

        let lastData = null;
        let count = 0;

        for (let i = 0; i < files.length; i++) {
            progressText.textContent = `Processing ${files[i].name} (${i + 1}/${files.length})...`;
            const formData = new FormData();
            formData.append('file', files[i]);

            try {
                const resp = await fetch(`${API}/api/upload`, { method: 'POST', body: formData });
                if (!resp.ok) {
                    const err = await resp.json();
                    throw new Error(`${files[i].name}: ${err.detail || 'Upload failed'}`);
                }
                const data = await resp.json();
                lastData = data;
                count++;

                if (!uploadedSubjects.find(s => s.result_id === data.result_id)) {
                    uploadedSubjects.push({
                        result_id: data.result_id,
                        filename: data.summary.filename,
                    });
                }
            } catch (err) {
                fill.classList.remove('indeterminate');
                progress.hidden = true;
                errorEl.textContent = err.message;
                errorEl.hidden = false;
                return;
            }
        }

        fill.classList.remove('indeterminate');
        fill.style.width = '100%';
        progressText.textContent = `Done! ${count} file${count > 1 ? 's' : ''} processed.`;

        setTimeout(() => {
            if (count === 1) {
                showResults(lastData);
            } else {
                document.getElementById('upload-section').hidden = true;
                showComparison();
            }
        }, 600);
    }
}

// ── Individual results ──
function showResults(data) {
    currentResultId = data.result_id;
    currentData = data;
    const s = data.summary;

    document.getElementById('upload-section').hidden = true;
    document.getElementById('results-section').hidden = false;

    // Info bar
    document.getElementById('info-filename').textContent = s.filename;
    document.getElementById('info-date').textContent = s.recording_date;
    document.getElementById('info-srate').textContent = `${s.sampling_rate} Hz`;
    document.getElementById('info-blocks').textContent = s.n_blocks;
    document.getElementById('info-trials').textContent =
        `${s.n_trials} · ${s.n_congruent} con / ${s.n_incongruent} inc`;

    // Channel names
    document.getElementById('ch1-name').textContent = s.theta.channel;
    document.getElementById('ch2-name').textContent = s.beta.channel;

    // Theta card
    setCard('theta', s.theta);
    // Beta card
    setCard('beta', s.beta);

    // Charts
    // Precompute per-trial condition + exclusion arrays (aligned to spectra rows)
    const condPerTrial = data.trials.map(t => t.cond);
    const fzExcludeArr = data.trials.map(t => t.fz_exclude);
    const c3ExcludeArr = data.trials.map(t => t.c3_exclude);

    renderSpectrumChart('chart-spec-theta', {
        freqs: data.spectra.freqs,
        conData: data.spectra.theta_congruent,
        incData: data.spectra.theta_incongruent,
        excData: data.spectra.theta_excluded,
        perTrial: data.spectra.theta_per_trial,
        exclusionFlags: fzExcludeArr,
        condPerTrial,
    }, 'Wavelet power (µV²)', s.config.theta_band);
    renderSpectrumChart('chart-spec-beta', {
        freqs: data.spectra.freqs,
        conData: data.spectra.beta_congruent,
        incData: data.spectra.beta_incongruent,
        excData: data.spectra.beta_excluded,
        perTrial: data.spectra.beta_per_trial,
        exclusionFlags: c3ExcludeArr,
        condPerTrial,
    }, 'Wavelet power (µV²)', s.config.beta_band);
    renderPerTrialChart('chart-trials-theta', data.trials, 'theta_rel', 'fz_exclude', 'Trial θ relative power');
    renderPerTrialChart('chart-trials-beta',  data.trials, 'beta_rel',  'c3_exclude', 'Trial β relative power');
    renderExclusionChart('chart-exclusion', data.trials, s.theta.channel, s.beta.channel);

    // Excluded trials table
    populateExcludedTable(data.trials);

    // Track this subject
    if (!uploadedSubjects.find(x => x.result_id === data.result_id)) {
        uploadedSubjects.push({ result_id: data.result_id, filename: s.filename });
    }
    updateSubjectList();
    updateCompareButton();
}

function setCard(prefix, band) {
    const relCon = band.rel_median_con ?? 0;
    const relInc = band.rel_median_inc ?? 0;
    const absCon = band.abs_median_con ?? 0;
    const absInc = band.abs_median_inc ?? 0;

    document.getElementById(`${prefix}-rel-con`).textContent = relCon.toFixed(3);
    document.getElementById(`${prefix}-rel-inc`).textContent = relInc.toFixed(3);
    document.getElementById(`${prefix}-abs-con`).textContent = `abs ${absCon.toFixed(2)}`;
    document.getElementById(`${prefix}-abs-inc`).textContent = `abs ${absInc.toFixed(2)}`;

    const relMax = Math.max(relCon, relInc, 0.001);
    document.getElementById(`${prefix}-bar-con`).style.width = `${(relCon / relMax) * 50}%`;
    document.getElementById(`${prefix}-bar-inc`).style.width = `${(relInc / relMax) * 50}%`;

    document.getElementById(`${prefix}-survival`).textContent =
        `${band.surviving}/${band.surviving + band.excluded} surviving · ` +
        `con ${band.exclusion_pct_con}% / inc ${band.exclusion_pct_inc}% excluded`;

    const flag = document.getElementById(`${prefix}-balance`);
    flag.hidden = !band.balance_flag;
}

// ── Wavelet spectrum chart ──
// Builds two views (Avg and All-trials) and stores the data on the container
// so the header toggle can switch between them without a re-fetch.
function renderSpectrumChart(containerId, spectrumData, yLabel, bandRange) {
    const el = document.getElementById(containerId);
    el.__spectrum = { ...spectrumData, yLabel, bandRange };

    // Default mode: whatever the panel had before, else 'avg'
    const panel = el.closest('.chart-panel');
    const currentMode = (panel && panel.dataset.mode) || 'avg';
    drawSpectrumChart(containerId, currentMode);
}

function drawSpectrumChart(containerId, mode) {
    const el = document.getElementById(containerId);
    const data = el.__spectrum;
    if (!data) return;
    const { freqs, conData, incData, excData,
            perTrial, exclusionFlags, condPerTrial,
            yLabel, bandRange } = data;

    const traces = [];
    if (mode === 'all' && perTrial && perTrial.length) {
        // Excluded first (dimmed, behind)
        for (let i = 0; i < perTrial.length; i++) {
            if (!exclusionFlags[i]) continue;
            traces.push({
                x: freqs, y: perTrial[i],
                type: 'scatter', mode: 'lines',
                line: { color: DIM_COLOR, width: 1 },
                opacity: 0.35,
                hoverinfo: 'skip',
                showlegend: false,
            });
        }
        // Surviving con/inc trials on top
        let sawCon = false, sawInc = false;
        for (let i = 0; i < perTrial.length; i++) {
            if (exclusionFlags[i]) continue;
            const isCon = condPerTrial[i] === 'con';
            const color = isCon ? CON_COLOR : INC_COLOR;
            const name  = isCon ? 'Congruent' : 'Incongruent';
            const showLegend = isCon ? !sawCon : !sawInc;
            if (isCon) sawCon = true; else sawInc = true;
            traces.push({
                x: freqs, y: perTrial[i],
                type: 'scatter', mode: 'lines',
                line: { color, width: 1 },
                opacity: 0.18,
                name, legendgroup: isCon ? 'con' : 'inc',
                showlegend: showLegend,
                hovertemplate: `<b>${name}</b> · trial ${i + 1}<br>%{x:.1f} Hz → %{y:.2f}<extra></extra>`,
            });
        }
        // Add a bold median line per condition on top for reference
        traces.push({
            x: freqs, y: conData, name: 'Congruent · avg', legendgroup: 'con-avg',
            type: 'scatter', mode: 'lines',
            line: { color: CON_COLOR, width: 2.2 },
        });
        traces.push({
            x: freqs, y: incData, name: 'Incongruent · avg', legendgroup: 'inc-avg',
            type: 'scatter', mode: 'lines',
            line: { color: INC_COLOR, width: 2.2 },
        });
    } else {
        // AVG mode (default)
        if (excData && excData.some(v => v > 0)) {
            traces.push({
                x: freqs, y: excData, name: 'Excluded (avg)',
                type: 'scatter', mode: 'lines',
                line: { color: DIM_COLOR, width: 1.2, dash: 'dot' },
            });
        }
        traces.push({
            x: freqs, y: conData, name: 'Congruent',
            type: 'scatter', mode: 'lines',
            line: { color: CON_COLOR, width: 1.6 },
            fill: 'tozeroy', fillcolor: CON_COLOR_DIM,
        });
        traces.push({
            x: freqs, y: incData, name: 'Incongruent',
            type: 'scatter', mode: 'lines',
            line: { color: INC_COLOR, width: 1.6 },
            fill: 'tozeroy', fillcolor: INC_COLOR_DIM,
        });
    }

    const layout = {
        ...plotlyLayout,
        xaxis: { ...plotlyLayout.xaxis, title: { text: 'Frequency (Hz)', font: { size: 10 } }, range: [1, 40] },
        yaxis: { ...plotlyLayout.yaxis, title: { text: yLabel, font: { size: 10 } } },
        hovermode: mode === 'all' ? 'closest' : 'x unified',
        showlegend: true,
        shapes: [{
            type: 'rect',
            x0: bandRange[0], x1: bandRange[1],
            y0: 0, y1: 1, yref: 'paper',
            fillcolor: 'rgba(255,255,255,0.04)',
            line: { color: 'rgba(255,255,255,0.12)', width: 1 },
        }],
    };
    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

// ── Per-trial power scatter ──
function renderPerTrialChart(containerId, trials, powerKey, excludeKey, yLabel) {
    // Split by condition × surviving/excluded → four traces
    function subset(cond, excluded) {
        return trials.filter(t => t.cond === cond && !!t[excludeKey] === excluded);
    }
    const survCon = subset('con', false);
    const survInc = subset('first', false);
    const excCon  = subset('con', true);
    const excInc  = subset('first', true);

    const trace = (data, name, color, opacity) => ({
        x: data.map(t => t.trial),
        y: data.map(t => t[powerKey]),
        text: data.map(t => `#${t.trial} · b${t.block} · RT ${t.rt_ms}ms${t.reason ? '<br>' + t.reason : ''}`),
        hovertemplate: `<b>${name}</b><br>%{text}<br>power=%{y:.3f}<extra></extra>`,
        name, type: 'scatter', mode: 'markers',
        marker: { color, size: 6, opacity, line: { width: 0 } },
    });

    const traces = [
        trace(survCon, 'Congruent (kept)',   CON_COLOR, 0.85),
        trace(survInc, 'Incongruent (kept)', INC_COLOR, 0.85),
        trace(excCon,  'Congruent (excluded)',   DIM_COLOR, 0.6),
        trace(excInc,  'Incongruent (excluded)', DIM_COLOR, 0.6),
    ];

    const layout = {
        ...plotlyLayout,
        xaxis: { ...plotlyLayout.xaxis, title: { text: 'Trial number', font: { size: 10 } } },
        yaxis: { ...plotlyLayout.yaxis, title: { text: yLabel, font: { size: 10 } } },
    };
    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

// ── Exclusion breakdown ──
function renderExclusionChart(containerId, trials, ch1Label, ch2Label) {
    // Bucket reasons per channel
    function count(exKey, patterns) {
        return trials.reduce((acc, t) => {
            if (!t[exKey]) return acc;
            const r = t.reason || '';
            for (const [label, re] of patterns) {
                if (re.test(r)) { acc[label] = (acc[label] || 0) + 1; }
            }
            return acc;
        }, {});
    }
    const patterns = [
        ['Blink',                /blink/],
        ['Coincident transient', /coincident/],
        ['Gross EMG',            /gross EMG/],
        ['C3 burst',             /burst/],
    ];
    const fzCounts = count('fz_exclude', patterns);
    const c3Counts = count('c3_exclude', patterns);
    const labels = patterns.map(p => p[0]);

    const traces = [
        {
            name: ch1Label, type: 'bar',
            x: labels, y: labels.map(l => fzCounts[l] || 0),
            marker: { color: '#5eead4', opacity: 0.85 },
        },
        {
            name: ch2Label, type: 'bar',
            x: labels, y: labels.map(l => c3Counts[l] || 0),
            marker: { color: '#f472b6', opacity: 0.85 },
        },
    ];
    const layout = {
        ...plotlyLayout,
        barmode: 'group', bargap: 0.35, bargroupgap: 0.15,
        xaxis: { ...plotlyLayout.xaxis, tickfont: { size: 10 } },
        yaxis: { ...plotlyLayout.yaxis, title: { text: 'Excluded trials', font: { size: 10 } } },
    };
    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

// ── Excluded trials table ──
function populateExcludedTable(trials) {
    const excluded = trials.filter(t => t.fz_exclude || t.c3_exclude);
    const section = document.getElementById('excluded-section');
    const countEl = document.getElementById('excluded-count');
    const tbody = document.querySelector('#excluded-table tbody');

    if (excluded.length === 0) {
        section.hidden = true;
        return;
    }
    section.hidden = false;
    countEl.textContent = `(${excluded.length})`;

    tbody.innerHTML = excluded.map(t => {
        const mm = Math.floor(t.onset / 60);
        const ss = (t.onset - mm * 60).toFixed(2).padStart(5, '0');
        const onsetLabel = `${mm}:${ss}`;
        const onsetRaw = t.onset.toFixed(4);
        return `
        <tr>
            <td>${t.trial}</td>
            <td>${t.block}</td>
            <td title="Raw seconds from LabChart file: ${onsetRaw}"><span class="onset-mm">${onsetLabel}</span><span class="onset-raw">${onsetRaw}s</span></td>
            <td>${t.condition}</td>
            <td>${t.rt_ms} ms</td>
            <td>${t.fz_ptp.toFixed(1)} µV</td>
            <td>${t.maxz.toFixed(2)}</td>
            <td>${t.impact.toFixed(1)}%</td>
            <td>${t.coinc.toFixed(2)}</td>
            <td class="${t.fz_exclude ? 'flag-yes' : 'flag-no'}">${t.fz_exclude ? '✕' : '·'}</td>
            <td class="${t.c3_exclude ? 'flag-yes' : 'flag-no'}">${t.c3_exclude ? '✕' : '·'}</td>
            <td>${t.reason}</td>
        </tr>`;
    }).join('');
}

// ── Subject list ──
function updateSubjectList() {
    const listEl = document.getElementById('subject-list');
    const itemsEl = document.getElementById('subject-items');
    if (uploadedSubjects.length > 1) {
        listEl.hidden = false;
        itemsEl.innerHTML = uploadedSubjects.map((s, i) => `
            <span class="subject-chip">
                <span class="chip-dot" style="background:${SUBJECT_COLORS[i % SUBJECT_COLORS.length]}"></span>
                ${s.filename}
                <button class="chip-remove" data-id="${s.result_id}" title="Remove">×</button>
            </span>
        `).join('');
        itemsEl.querySelectorAll('.chip-remove').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const id = e.target.dataset.id;
                await fetch(`${API}/api/subjects/${id}`, { method: 'DELETE' });
                uploadedSubjects = uploadedSubjects.filter(s => s.result_id !== id);
                updateSubjectList();
                updateCompareButton();
            });
        });
    } else {
        listEl.hidden = true;
    }
}

function updateCompareButton() {
    const btn = document.getElementById('btn-compare');
    btn.disabled = uploadedSubjects.length < 2;
    btn.textContent = uploadedSubjects.length < 2
        ? 'Compare Subjects'
        : `Compare ${uploadedSubjects.length} Subjects`;
}

// ── Group comparison ──
async function showComparison() {
    try {
        const resp = await fetch(`${API}/api/compare`);
        if (!resp.ok) {
            const err = await resp.json();
            alert(err.detail || 'Comparison failed');
            return;
        }
        const data = await resp.json();
        const subjects = data.subjects;

        document.getElementById('results-section').hidden = true;
        document.getElementById('compare-section').hidden = false;

        // Table
        const thead = document.querySelector('#compare-table thead');
        const tbody = document.querySelector('#compare-table tbody');
        thead.innerHTML = `<tr>
            <th></th><th>Subject</th><th>Date</th>
            <th>θ rel con</th><th>θ rel inc</th><th>θ Δ</th>
            <th>β rel con</th><th>β rel inc</th>
            <th>θ surv</th><th>β surv</th>
        </tr>`;
        tbody.innerHTML = subjects.map((subj, i) => {
            const s = subj.summary;
            const dTheta = s.theta.rel_median_inc - s.theta.rel_median_con;
            const dcol = dTheta > 0 ? 'val-inc' : 'val-con';
            const balTh = s.theta.balance_flag ? ' ⚠' : '';
            const balBe = s.beta.balance_flag ? ' ⚠' : '';
            return `<tr>
                <td><span class="chip-dot" style="background:${SUBJECT_COLORS[i % SUBJECT_COLORS.length]};display:inline-block;width:8px;height:8px;border-radius:50%"></span></td>
                <td>${s.filename}</td>
                <td>${s.recording_date}</td>
                <td class="val-con">${s.theta.rel_median_con.toFixed(3)}</td>
                <td class="val-inc">${s.theta.rel_median_inc.toFixed(3)}</td>
                <td class="${dcol}">${dTheta >= 0 ? '+' : ''}${dTheta.toFixed(3)}</td>
                <td class="val-con">${s.beta.rel_median_con.toFixed(3)}</td>
                <td class="val-inc">${s.beta.rel_median_inc.toFixed(3)}</td>
                <td>${s.theta.surviving}/${s.n_trials}${balTh}</td>
                <td>${s.beta.surviving}/${s.n_trials}${balBe}</td>
            </tr>`;
        }).join('');

        // Grouped bars: relative power
        renderGroupedBar('chart-compare-theta', subjects,
            s => s.summary.theta.rel_median_con, s => s.summary.theta.rel_median_inc,
            'θ relative power');
        renderGroupedBar('chart-compare-beta', subjects,
            s => s.summary.beta.rel_median_con, s => s.summary.beta.rel_median_inc,
            'β relative power');

        // Exclusion rate stacked chart
        renderExclusionCompareChart('chart-compare-exclusion', subjects);

        // Congruency effect (θ inc - con)
        renderEffectChart('chart-compare-effect', subjects);

    } catch (err) {
        alert('Error loading comparison: ' + err.message);
    }
}

function renderGroupedBar(containerId, subjects, conFn, incFn, yLabel) {
    const names = subjects.map(s => s.summary.filename);
    const traces = [
        { x: names, y: subjects.map(conFn), name: 'Congruent',   type: 'bar', marker: { color: CON_COLOR, opacity: 0.85 } },
        { x: names, y: subjects.map(incFn), name: 'Incongruent', type: 'bar', marker: { color: INC_COLOR, opacity: 0.85 } },
    ];
    const layout = {
        ...plotlyLayout,
        barmode: 'group', bargap: 0.3, bargroupgap: 0.1,
        xaxis: { ...plotlyLayout.xaxis, tickfont: { size: 9 } },
        yaxis: { ...plotlyLayout.yaxis, title: { text: yLabel, font: { size: 10 } } },
    };
    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

function renderExclusionCompareChart(containerId, subjects) {
    const names = subjects.map(s => s.summary.filename);
    const traces = [
        {
            x: names,
            y: subjects.map(s => 100 * s.summary.theta.excluded / s.summary.n_trials),
            name: 'θ excl % (Fz-Pz)', type: 'bar',
            marker: { color: '#5eead4', opacity: 0.85 },
        },
        {
            x: names,
            y: subjects.map(s => 100 * s.summary.beta.excluded / s.summary.n_trials),
            name: 'β excl % (C3-C4)', type: 'bar',
            marker: { color: '#f472b6', opacity: 0.85 },
        },
    ];
    const layout = {
        ...plotlyLayout,
        barmode: 'group', bargap: 0.3, bargroupgap: 0.1,
        xaxis: { ...plotlyLayout.xaxis, tickfont: { size: 9 } },
        yaxis: { ...plotlyLayout.yaxis, title: { text: '% excluded', font: { size: 10 } }, rangemode: 'tozero' },
    };
    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

function renderEffectChart(containerId, subjects) {
    const names = subjects.map(s => s.summary.filename);
    const effects = subjects.map(s => s.summary.theta.rel_median_inc - s.summary.theta.rel_median_con);
    const colors  = effects.map(e => e >= 0 ? INC_COLOR : DIM_COLOR);
    const traces = [{
        x: names, y: effects, name: 'θ inc − con',
        type: 'bar', marker: { color: colors, opacity: 0.85 },
    }];
    const layout = {
        ...plotlyLayout,
        showlegend: false,
        xaxis: { ...plotlyLayout.xaxis, tickfont: { size: 9 } },
        yaxis: {
            ...plotlyLayout.yaxis,
            title: { text: 'Δ theta relative power', font: { size: 10 } },
            zeroline: true, zerolinecolor: 'rgba(255,255,255,0.15)', zerolinewidth: 1,
        },
    };
    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

// ── Navigation ──
function goToUpload() {
    document.getElementById('results-section').hidden = true;
    document.getElementById('compare-section').hidden = true;
    document.getElementById('upload-section').hidden = false;
    document.getElementById('upload-progress').hidden = true;
    document.getElementById('file-input').value = '';
}

// ── Buttons ──
function initActions() {
    document.getElementById('btn-download').addEventListener('click', () => {
        if (currentResultId) window.location.href = `${API}/api/download-csv/${currentResultId}`;
    });
    document.getElementById('btn-download-trials').addEventListener('click', () => {
        if (currentResultId) window.location.href = `${API}/api/download-csv-trials/${currentResultId}`;
    });
    document.getElementById('btn-download-exclusions').addEventListener('click', () => {
        if (currentResultId) window.location.href = `${API}/api/download-csv-exclusions/${currentResultId}`;
    });
    document.getElementById('btn-add-subject').addEventListener('click', () => goToUpload());
    document.getElementById('btn-compare').addEventListener('click', () => showComparison());

    document.getElementById('btn-new').addEventListener('click', async () => {
        for (const s of uploadedSubjects) {
            await fetch(`${API}/api/subjects/${s.result_id}`, { method: 'DELETE' });
        }
        uploadedSubjects = [];
        currentResultId = null;
        currentData = null;
        goToUpload();
    });

    document.getElementById('btn-back-individual').addEventListener('click', () => {
        document.getElementById('compare-section').hidden = true;
        document.getElementById('results-section').hidden = false;
    });

    document.getElementById('btn-download-all').addEventListener('click', () => {
        window.location.href = `${API}/api/download-csv-all`;
    });
    document.getElementById('btn-download-trials-all').addEventListener('click', () => {
        window.location.href = `${API}/api/download-csv-trials-all`;
    });
    document.getElementById('btn-add-more').addEventListener('click', () => {
        document.getElementById('compare-section').hidden = true;
        goToUpload();
    });

    document.getElementById('header-home-link').addEventListener('click', (e) => {
        e.preventDefault();
        document.getElementById('compare-section').hidden = true;
        document.getElementById('results-section').hidden = true;
        goToUpload();
    });
}

document.addEventListener('DOMContentLoaded', () => {
    initBgWave();
    initUpload();
    initActions();
    initChartExpand();
});

// ── Chart expand / collapse ──
function initChartExpand() {
    // Inject an "Expand" button into every chart panel header, and wire it up.
    // Works for both individual and compare screens because we run this once at
    // startup and again after each render pass (see enhanceChartPanels()).
    enhanceChartPanels();

    // Escape closes any expanded panel
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') collapseAllCharts();
    });
}

function enhanceChartPanels() {
    document.querySelectorAll('.chart-panel').forEach(panel => {
        const h3 = panel.querySelector('h3');
        if (!h3) return;

        // Avg / All trials toggle — only for the spectrum panels
        const container = panel.querySelector('.chart-container');
        const isSpectrum = container && (container.id === 'chart-spec-theta' || container.id === 'chart-spec-beta');
        if (isSpectrum && !panel.querySelector('.chart-mode-btn')) {
            panel.dataset.mode = panel.dataset.mode || 'avg';
            const toggle = document.createElement('button');
            toggle.className = 'chart-mode-btn';
            toggle.type = 'button';
            toggle.textContent = 'All trials';
            toggle.title = 'Switch between averaged and per-trial view';
            toggle.addEventListener('click', (e) => {
                e.stopPropagation();
                const next = panel.dataset.mode === 'all' ? 'avg' : 'all';
                panel.dataset.mode = next;
                toggle.textContent = next === 'all' ? 'Avg' : 'All trials';
                drawSpectrumChart(container.id, next);
            });
            h3.appendChild(toggle);
        }

        // Expand button on every panel
        if (!panel.querySelector('.chart-expand-btn')) {
            const btn = document.createElement('button');
            btn.className = 'chart-expand-btn';
            btn.type = 'button';
            btn.textContent = '⤢ Expand';
            btn.title = 'Expand chart (Esc to collapse)';
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                toggleChartExpand(panel, btn);
            });
            h3.appendChild(btn);
        }
    });
}

function toggleChartExpand(panel, btn) {
    const expanded = panel.classList.toggle('expanded');
    btn.textContent = expanded ? '× Collapse' : '⤢ Expand';

    // Backdrop
    let backdrop = document.querySelector('.chart-backdrop');
    if (expanded) {
        if (!backdrop) {
            backdrop = document.createElement('div');
            backdrop.className = 'chart-backdrop';
            backdrop.addEventListener('click', () => collapseAllCharts());
            document.body.appendChild(backdrop);
        }
        // Collapse any other expanded panel first
        document.querySelectorAll('.chart-panel.expanded').forEach(p => {
            if (p !== panel) {
                p.classList.remove('expanded');
                const b = p.querySelector('.chart-expand-btn');
                if (b) b.textContent = '⤢ Expand';
            }
        });
    } else if (backdrop) {
        backdrop.remove();
    }

    // Ask Plotly to resize the chart to fit the new container size
    const container = panel.querySelector('.chart-container');
    if (container && window.Plotly) {
        setTimeout(() => Plotly.Plots.resize(container), 50);
    }
}

function collapseAllCharts() {
    document.querySelectorAll('.chart-panel.expanded').forEach(panel => {
        panel.classList.remove('expanded');
        const btn = panel.querySelector('.chart-expand-btn');
        if (btn) btn.textContent = '⤢ Expand';
        const container = panel.querySelector('.chart-container');
        if (container && window.Plotly) {
            setTimeout(() => Plotly.Plots.resize(container), 50);
        }
    });
    const backdrop = document.querySelector('.chart-backdrop');
    if (backdrop) backdrop.remove();
}

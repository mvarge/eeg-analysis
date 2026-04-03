/* ============================================
   EEG Flanker Analysis — App Logic
   ============================================ */

const API = window.location.origin;
let currentResultId = null;
let uploadedSubjects = [];  // Track all uploaded subjects

// Subject color palette for comparison charts
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
    },
    yaxis: {
        gridcolor: 'rgba(30,35,48,0.8)',
        zerolinecolor: 'rgba(94,234,212,0.2)',
        tickfont: { size: 10 },
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
    hovermode: 'x unified',
    spikedistance: -1,
};

const plotlyConfig = {
    displayModeBar: false,
    responsive: true,
};

const CON_COLOR = '#5eead4';
const INC_COLOR = '#f472b6';
const CON_COLOR_DIM = 'rgba(94,234,212,0.15)';
const INC_COLOR_DIM = 'rgba(244,114,182,0.15)';

// ── Background wave animation ──
function initBgWave() {
    const canvas = document.getElementById('bg-wave');
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

// ── Upload handling ──
function initUpload() {
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');
    const progress = document.getElementById('upload-progress');
    const errorEl = document.getElementById('upload-error');

    dropZone.addEventListener('click', () => fileInput.click());

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('drag-over');
    });

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

        const results = [];
        let lastData = null;

        for (let i = 0; i < files.length; i++) {
            progressText.textContent = `Processing ${files[i].name} (${i + 1}/${files.length})...`;

            const formData = new FormData();
            formData.append('file', files[i]);

            try {
                const resp = await fetch(`${API}/api/upload`, {
                    method: 'POST',
                    body: formData,
                });

                if (!resp.ok) {
                    const err = await resp.json();
                    throw new Error(`${files[i].name}: ${err.detail || 'Upload failed'}`);
                }

                const data = await resp.json();
                results.push(data);
                lastData = data;

                // Track subject
                const s = data.summary;
                if (!uploadedSubjects.find(sub => sub.result_id === data.result_id)) {
                    uploadedSubjects.push({
                        result_id: data.result_id,
                        filename: s.filename,
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
        progressText.textContent = `Done! ${results.length} file${results.length > 1 ? 's' : ''} processed.`;

        setTimeout(() => {
            if (results.length === 1) {
                // Single file → individual view
                showResults(lastData);
            } else {
                // Multiple files → straight to comparison
                document.getElementById('upload-section').hidden = true;
                showComparison();
            }
        }, 600);
    }
}

// ── Results display ──
function showResults(data) {
    const s = data.summary;
    currentResultId = data.result_id;

    // Hide upload, show results
    document.getElementById('upload-section').hidden = true;
    document.getElementById('results-section').hidden = false;

    // Info bar
    document.getElementById('info-filename').textContent = s.filename;
    document.getElementById('info-date').textContent = s.recording_date;
    document.getElementById('info-srate').textContent = `${s.sampling_rate} Hz`;
    document.getElementById('info-epochs').textContent = `${s.n_epochs_congruent + s.n_epochs_incongruent} (${s.n_epochs_congruent} con / ${s.n_epochs_incongruent} inc)`;

    // Channel names
    const ch1 = s.channel_names[0] || 'Ch1';
    const ch2 = s.channel_names[1] || 'Ch2';
    document.getElementById('ch1-name').textContent = ch1;
    document.getElementById('ch2-name').textContent = ch2;
    document.getElementById('chart1-ch').textContent = ch1;
    document.getElementById('chart2-ch').textContent = ch2;
    document.getElementById('chart3-ch').textContent = ch1;
    document.getElementById('chart4-ch').textContent = ch2;

    // Power cards
    document.getElementById('theta-con').textContent = s.theta_power_congruent.toFixed(4);
    document.getElementById('theta-inc').textContent = s.theta_power_incongruent.toFixed(4);
    document.getElementById('beta-con').textContent = s.beta_power_congruent.toFixed(4);
    document.getElementById('beta-inc').textContent = s.beta_power_incongruent.toFixed(4);

    // Power bars
    const thetaMax = Math.max(s.theta_power_congruent, s.theta_power_incongruent);
    const betaMax = Math.max(s.beta_power_congruent, s.beta_power_incongruent);
    document.getElementById('theta-bar-con').style.width = `${(s.theta_power_congruent / thetaMax) * 50}%`;
    document.getElementById('theta-bar-inc').style.width = `${(s.theta_power_incongruent / thetaMax) * 50}%`;
    document.getElementById('beta-bar-con').style.width = `${(s.beta_power_congruent / betaMax) * 50}%`;
    document.getElementById('beta-bar-inc').style.width = `${(s.beta_power_incongruent / betaMax) * 50}%`;

    // Charts
    renderERPChart('chart-erp-ch1', data.waveforms.times_ms, data.waveforms.ch1_congruent, data.waveforms.ch1_incongruent, 'Amplitude (µV)');
    renderERPChart('chart-erp-ch2', data.waveforms.times_ms, data.waveforms.ch2_congruent, data.waveforms.ch2_incongruent, 'Amplitude (µV)');
    renderSpectrumChart('chart-spec-ch1', data.spectra.freqs, data.spectra.ch1_congruent, data.spectra.ch1_incongruent, 'Power (µV²/Hz)', [4, 8]);
    renderSpectrumChart('chart-spec-ch2', data.spectra.freqs, data.spectra.ch2_congruent, data.spectra.ch2_incongruent, 'Power (µV²/Hz)', [13, 30]);
    renderDistributionChart('chart-distribution', data.epoch_powers);

    // Track this subject
    if (!uploadedSubjects.find(s => s.result_id === data.result_id)) {
        uploadedSubjects.push({
            result_id: data.result_id,
            filename: s.filename,
        });
    }
    updateSubjectList();
    updateCompareButton();
}

function renderERPChart(containerId, times, conData, incData, yLabel) {
    const traces = [
        {
            x: times, y: conData,
            name: 'Congruent', type: 'scatter', mode: 'lines',
            line: { color: CON_COLOR, width: 1.5 },
        },
        {
            x: times, y: incData,
            name: 'Incongruent', type: 'scatter', mode: 'lines',
            line: { color: INC_COLOR, width: 1.5 },
        },
    ];

    const layout = {
        ...plotlyLayout,
        xaxis: { ...plotlyLayout.xaxis, title: { text: 'Time (ms)', font: { size: 10 } },
            showspikes: true, spikemode: 'across', spikethickness: 0.5,
            spikecolor: 'rgba(94,234,212,0.2)', spikedash: 'dot' },
        yaxis: { ...plotlyLayout.yaxis, title: { text: yLabel, font: { size: 10 } } },
        shapes: [{
            type: 'line', x0: 0, x1: 0, y0: 0, y1: 1, yref: 'paper',
            line: { color: 'rgba(94,234,212,0.3)', width: 1, dash: 'dot' },
        }],
    };

    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

function renderSpectrumChart(containerId, freqs, conData, incData, yLabel, bandRange) {
    const traces = [
        {
            x: freqs, y: conData,
            name: 'Congruent', type: 'scatter', mode: 'lines',
            line: { color: CON_COLOR, width: 1.5 },
            fill: 'tozeroy', fillcolor: CON_COLOR_DIM,
        },
        {
            x: freqs, y: incData,
            name: 'Incongruent', type: 'scatter', mode: 'lines',
            line: { color: INC_COLOR, width: 1.5 },
            fill: 'tozeroy', fillcolor: INC_COLOR_DIM,
        },
    ];

    const layout = {
        ...plotlyLayout,
        xaxis: { ...plotlyLayout.xaxis, title: { text: 'Frequency (Hz)', font: { size: 10 } }, range: [0, 45],
            showspikes: true, spikemode: 'across', spikethickness: 0.5,
            spikecolor: 'rgba(94,234,212,0.2)', spikedash: 'dot' },
        yaxis: { ...plotlyLayout.yaxis, title: { text: yLabel, font: { size: 10 } } },
        shapes: [{
            type: 'rect',
            x0: bandRange[0], x1: bandRange[1],
            y0: 0, y1: 1, yref: 'paper',
            fillcolor: 'rgba(255,255,255,0.03)',
            line: { color: 'rgba(255,255,255,0.1)', width: 1 },
        }],
    };

    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

function renderDistributionChart(containerId, epochPowers) {
    const conTheta = epochPowers.filter(e => e.condition === 'congruent').map(e => e.theta_power);
    const incTheta = epochPowers.filter(e => e.condition === 'incongruent').map(e => e.theta_power);
    const conBeta = epochPowers.filter(e => e.condition === 'congruent').map(e => e.beta_power);
    const incBeta = epochPowers.filter(e => e.condition === 'incongruent').map(e => e.beta_power);

    const traces = [
        {
            y: conTheta, name: 'θ Congruent', type: 'violin',
            side: 'negative', line: { color: CON_COLOR, width: 1.5 },
            fillcolor: CON_COLOR_DIM, meanline: { visible: true },
            scalemode: 'width', width: 1.8, x0: 'Theta',
        },
        {
            y: incTheta, name: 'θ Incongruent', type: 'violin',
            side: 'positive', line: { color: INC_COLOR, width: 1.5 },
            fillcolor: INC_COLOR_DIM, meanline: { visible: true },
            scalemode: 'width', width: 1.8, x0: 'Theta',
        },
        {
            y: conBeta, name: 'β Congruent', type: 'violin',
            side: 'negative', line: { color: CON_COLOR, width: 1.5 },
            fillcolor: CON_COLOR_DIM, meanline: { visible: true },
            scalemode: 'width', width: 1.8, x0: 'Beta',
        },
        {
            y: incBeta, name: 'β Incongruent', type: 'violin',
            side: 'positive', line: { color: INC_COLOR, width: 1.5 },
            fillcolor: INC_COLOR_DIM, meanline: { visible: true },
            scalemode: 'width', width: 1.8, x0: 'Beta',
        },
    ];

    const layout = {
        ...plotlyLayout,
        yaxis: { ...plotlyLayout.yaxis, title: { text: 'Power (µV²/Hz)', font: { size: 10 } } },
        violinmode: 'overlay',
    };

    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

// ── Subject list management ──
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

// ── Comparison view ──
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

        // Build comparison table
        const thead = document.querySelector('#compare-table thead');
        const tbody = document.querySelector('#compare-table tbody');
        thead.innerHTML = `<tr>
            <th></th><th>Subject</th><th>Date</th>
            <th>θ Con</th><th>θ Inc</th>
            <th>β Con</th><th>β Inc</th>
            <th>Epochs</th>
        </tr>`;
        tbody.innerHTML = subjects.map((s, i) => `<tr>
            <td><span class="chip-dot" style="background:${SUBJECT_COLORS[i % SUBJECT_COLORS.length]};display:inline-block;width:8px;height:8px;border-radius:50%"></span></td>
            <td>${s.filename}</td>
            <td>${s.recording_date}</td>
            <td class="val-con">${s.theta_power_congruent.toFixed(2)}</td>
            <td class="val-inc">${s.theta_power_incongruent.toFixed(2)}</td>
            <td class="val-con">${s.beta_power_congruent.toFixed(2)}</td>
            <td class="val-inc">${s.beta_power_incongruent.toFixed(2)}</td>
            <td>${s.n_epochs_congruent + s.n_epochs_incongruent}</td>
        </tr>`).join('');

        // Theta grouped bar chart
        renderGroupedBar('chart-compare-theta', subjects, 'theta_power_congruent', 'theta_power_incongruent', 'θ Power (µV²/Hz)');
        // Beta grouped bar chart
        renderGroupedBar('chart-compare-beta', subjects, 'beta_power_congruent', 'beta_power_incongruent', 'β Power (µV²/Hz)');
        // Overlaid ERPs
        renderOverlaidERP('chart-compare-erp-ch1-con', subjects, 'ch1_congruent', 'Congruent — Amplitude (µV)');
        renderOverlaidERP('chart-compare-erp-ch1-inc', subjects, 'ch1_incongruent', 'Incongruent — Amplitude (µV)');

    } catch (err) {
        alert('Error loading comparison: ' + err.message);
    }
}

function renderGroupedBar(containerId, subjects, conKey, incKey, yLabel) {
    const names = subjects.map(s => s.result_id);
    const traces = [
        {
            x: names, y: subjects.map(s => s[conKey]),
            name: 'Congruent', type: 'bar',
            marker: { color: CON_COLOR, opacity: 0.8 },
        },
        {
            x: names, y: subjects.map(s => s[incKey]),
            name: 'Incongruent', type: 'bar',
            marker: { color: INC_COLOR, opacity: 0.8 },
        },
    ];
    const layout = {
        ...plotlyLayout,
        barmode: 'group',
        bargap: 0.3,
        bargroupgap: 0.1,
        xaxis: { ...plotlyLayout.xaxis, tickfont: { size: 9 } },
        yaxis: { ...plotlyLayout.yaxis, title: { text: yLabel, font: { size: 10 } } },
        hovermode: 'closest',
    };
    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

function renderOverlaidERP(containerId, subjects, waveKey, yLabel) {
    const traces = subjects.map((s, i) => ({
        x: s.waveforms.times_ms,
        y: s.waveforms[waveKey],
        name: s.result_id,
        type: 'scatter', mode: 'lines',
        line: { color: SUBJECT_COLORS[i % SUBJECT_COLORS.length], width: 1.5 },
    }));
    const layout = {
        ...plotlyLayout,
        xaxis: { ...plotlyLayout.xaxis, title: { text: 'Time (ms)', font: { size: 10 } },
            showspikes: true, spikemode: 'across', spikethickness: 0.5,
            spikecolor: 'rgba(94,234,212,0.2)', spikedash: 'dot' },
        yaxis: { ...plotlyLayout.yaxis, title: { text: yLabel, font: { size: 10 } } },
        shapes: [{
            type: 'line', x0: 0, x1: 0, y0: 0, y1: 1, yref: 'paper',
            line: { color: 'rgba(94,234,212,0.3)', width: 1, dash: 'dot' },
        }],
    };
    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

// ── Navigation helpers ──
function goToUpload() {
    document.getElementById('results-section').hidden = true;
    document.getElementById('compare-section').hidden = true;
    document.getElementById('upload-section').hidden = false;
    document.getElementById('upload-progress').hidden = true;
    document.getElementById('file-input').value = '';
}

// ── Actions ──
function initActions() {
    document.getElementById('btn-download').addEventListener('click', () => {
        if (currentResultId) {
            window.location.href = `${API}/api/download-csv/${currentResultId}`;
        }
    });

    document.getElementById('btn-add-subject').addEventListener('click', () => goToUpload());

    document.getElementById('btn-compare').addEventListener('click', () => showComparison());

    document.getElementById('btn-new').addEventListener('click', async () => {
        // Clear all subjects from server
        for (const s of uploadedSubjects) {
            await fetch(`${API}/api/subjects/${s.result_id}`, { method: 'DELETE' });
        }
        uploadedSubjects = [];
        currentResultId = null;
        goToUpload();
    });

    document.getElementById('btn-back-individual').addEventListener('click', () => {
        document.getElementById('compare-section').hidden = true;
        document.getElementById('results-section').hidden = false;
    });

    document.getElementById('btn-download-all').addEventListener('click', () => {
        window.location.href = `${API}/api/download-csv-all`;
    });

    document.getElementById('btn-add-more').addEventListener('click', () => {
        document.getElementById('compare-section').hidden = true;
        goToUpload();
    });
}

// ── Init ──
document.addEventListener('DOMContentLoaded', () => {
    initBgWave();
    initUpload();
    initActions();
});

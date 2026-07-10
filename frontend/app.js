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

    // Demographics CSV
    initDemographicsUpload();
    initBehaviouralUpload();
    refreshDemographicsStatus();

    async function uploadFiles(files) {
        errorEl.hidden = true;
        progress.hidden = false;
        const fill = progress.querySelector('.progress-fill');
        const progressText = progress.querySelector('.progress-text');
        fill.classList.add('indeterminate');

        // Group files by canonical subject ID so that e.g. S8P025(1).txt and
        // S8P025(2).txt are uploaded together as one subject. Anything that
        // doesn't match `S<n>P<nn>` falls back to its filename stem.
        const groups = new Map();  // subject_id -> File[]
        for (const f of files) {
            const sid = subjectIdFromFilename(f.name);
            if (!groups.has(sid)) groups.set(sid, []);
            groups.get(sid).push(f);
        }

        let lastData = null;
        let count = 0;
        const groupList = Array.from(groups.entries());

        for (let i = 0; i < groupList.length; i++) {
            const [sid, group] = groupList[i];
            const label = group.length === 1
                ? group[0].name
                : `${sid} (${group.length} files)`;
            progressText.textContent = `Processing ${label} (${i + 1}/${groupList.length})...`;
            const formData = new FormData();
            for (const f of group) formData.append('files', f);

            try {
                const resp = await fetch(`${API}/api/upload`, { method: 'POST', body: formData });
                if (!resp.ok) {
                    const err = await resp.json();
                    throw new Error(`${label}: ${err.detail || 'Upload failed'}`);
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
        progressText.textContent = `Done! ${count} subject${count > 1 ? 's' : ''} processed.`;

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

// Canonical subject ID from an uploaded filename.
// "S1P002.txt" → "S1P002", "S8P025(1).txt" → "S8P025",
// "S3P006 (flanker-partial).csv" → "S3P006",
// falls back to the extensionless stem if nothing matches.
function subjectIdFromFilename(name) {
    const base = name.replace(/^.*[\\/]/, '');
    const m = base.match(/^\s*(S\d+P\d+)\s*/i);
    if (m) return m[1].toUpperCase();
    const dot = base.lastIndexOf('.');
    return (dot > 0 ? base.slice(0, dot) : base);
}

// ── Demographics upload ──
let demographicsLoaded = false;

function initDemographicsUpload() {
    const input = document.getElementById('demo-file-input');
    const clearBtn = document.getElementById('demo-clear-btn');
    if (!input) return;

    input.addEventListener('change', async () => {
        if (!input.files || !input.files.length) return;
        const file = input.files[0];
        const formData = new FormData();
        formData.append('file', file);
        setDemographicsStatus('Uploading…', false);
        try {
            const resp = await fetch(`${API}/api/demographics/upload`, { method: 'POST', body: formData });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({ detail: 'Upload failed' }));
                setDemographicsStatus(`⚠ ${err.detail || 'Upload failed'}`, false);
                return;
            }
            const data = await resp.json();
            demographicsLoaded = true;
            const label = data.csv_source || file.name;
            const matched = data.matches.length;
            const unmatched = data.unmatched.length;
            let msg = `${data.n_participants} participants loaded from ${label}`;
            if (matched + unmatched > 0) {
                msg += ` · ${matched} matched, ${unmatched} unmatched`;
            }
            setDemographicsStatus(msg, true);
            clearBtn.hidden = false;
            document.getElementById('demo-upload-label').textContent = 'Replace CSV';
            // Refresh whatever view is currently visible
            if (currentData) refreshCurrentResultDemographics();
        } catch (err) {
            setDemographicsStatus(`⚠ ${err.message || 'Upload failed'}`, false);
        } finally {
            input.value = '';   // let the user upload the same file again
        }
    });

    clearBtn.addEventListener('click', async () => {
        try {
            await fetch(`${API}/api/demographics`, { method: 'DELETE' });
        } catch (_) { /* ignore */ }
        demographicsLoaded = false;
        setDemographicsStatus('No file loaded — analysis runs without demographics', false);
        clearBtn.hidden = true;
        document.getElementById('demo-upload-label').textContent = 'Upload CSV';
        if (currentData) refreshCurrentResultDemographics();
    });
}

function setDemographicsStatus(msg, loaded) {
    const el = document.getElementById('demo-status');
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle('loaded', !!loaded);
}

async function refreshDemographicsStatus() {
    // Check backend state on load (in case demographics were left in memory)
    try {
        const resp = await fetch(`${API}/api/demographics`);
        if (!resp.ok) return;
        const d = await resp.json();
        if (d.n_participants > 0 && d.csv_source) {
            demographicsLoaded = true;
            setDemographicsStatus(`${d.n_participants} participants loaded from ${d.csv_source}`, true);
            document.getElementById('demo-clear-btn').hidden = false;
            document.getElementById('demo-upload-label').textContent = 'Replace CSV';
        }
    } catch (_) { /* backend down, ignore */ }
}

// Re-fetch the current subject so its demographics section refreshes when
// the CSV state changes on the fly.
async function refreshCurrentResultDemographics() {
    if (!currentResultId) return;
    try {
        // Cheapest way: hit /api/subjects and then /api/compare (single-subject compare
        // is blocked, so use the raw endpoint). Easiest: re-render existing summary
        // by re-uploading? no. Just render from the currentData for now — the demo
        // payload is baked in at upload time. To *live* refresh, we'd need a
        // dedicated endpoint. For now: prompt the user to re-upload if they change
        // the CSV mid-session.
        renderDemographicsPanel(currentData.summary.demographics);
    } catch (_) { /* ignore */ }
}

function renderDemographicsPanel(demo) {
    const panel = document.getElementById('demographics-panel');
    if (!panel) return;
    if (!demo || !demo.matched) {
        panel.hidden = true;
        return;
    }
    panel.hidden = false;

    // Header
    const subjEl = document.getElementById('demo-subject');
    subjEl.textContent = `S${demo.session}P${String(demo.participant).padStart(3, '0')} · Participant ${demo.participant} · Session ${demo.session}`;
    document.getElementById('demo-aborted').hidden = !demo.aborted;

    // Fields
    const grid = document.getElementById('demo-fields');
    grid.innerHTML = demo.fields.map(f => {
        const value = f.value || '—';
        const empty = !f.value ? ' empty' : '';
        return `<div class="demo-field">
            <span class="demo-field-label">${escapeHtml(f.label)}</span>
            <span class="demo-field-value${empty}" title="${escapeHtml(String(value))}">${escapeHtml(String(value))}</span>
        </div>`;
    }).join('');
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// ── Behavioural alignment ──

// Render the alignment panel from a payload like data.alignment
// (list of per-block objects). Passing null/[] hides the panel.
// `accuracy` is the parallel per-block accuracy summary; may be null.
function renderAlignmentPanel(alignment, accuracy) {
    const panel = document.getElementById('alignment-panel');
    if (!panel) return;
    if (!alignment || !alignment.length) {
        panel.hidden = true;
        return;
    }
    panel.hidden = false;
    const src = document.getElementById('alignment-source');
    if (src) src.textContent = `${alignment.length} block${alignment.length > 1 ? 's' : ''} aligned`;

    // Index accuracy by block for O(1) lookup.
    const accByBlock = new Map();
    if (accuracy) for (const a of accuracy) accByBlock.set(a.block, a);

    const grid = document.getElementById('alignment-fields');
    grid.innerHTML = alignment.map(a => {
        const gates = alignmentGateStatus(a);
        const pillCls = `pill ${gates.severity}`;
        const acc = accByBlock.get(a.block);
        const accLine = acc
            ? `<span>accuracy <b>${fmtPct(acc.accuracy)}</b>${acc.n_errors ? ` · <b>${acc.n_errors}</b> error${acc.n_errors > 1 ? 's' : ''}` : ''}${
                acc.eeg_error_trials_dropped.theta || acc.eeg_error_trials_dropped.beta
                    ? ` · would drop θ <b>${acc.eeg_error_trials_dropped.theta}</b> / β <b>${acc.eeg_error_trials_dropped.beta}</b> from surviving`
                    : ''
              }</span>`
            : '';
        return `<div class="demo-field alignment-field">
            <span class="demo-field-label">Block ${a.block} <span class="${pillCls}">${gates.label}</span></span>
            <span class="demo-field-value alignment-value">
                <span>matched <b>${a.matched}</b>${a.unmatched_eeg ? ` · ${a.unmatched_eeg} EEG missing` : ''}${a.unmatched_beh ? ` · ${a.unmatched_beh} behavioural missing` : ''}</span>
                <span>offset <b>${fmtMs(a.eeg_offset_ms)}</b> · r <b>${fmtR(a.rt_correlation)}</b> · congruency <b>${fmtPct(a.congruency_agreement)}</b></span>
                ${accLine}
                ${gates.detail ? `<span class="alignment-detail">${escapeHtml(gates.detail)}</span>` : ''}
            </span>
        </div>`;
    }).join('');
}

// Apply the docs/DATA_VALIDITY_CHECKING.md §7 J-code gates and return a
// UI-friendly severity + label + detail message.
function alignmentGateStatus(a) {
    const problems = [];
    if (a.matched < 10) problems.push({ sev: 'halt', msg: 'J001 fewer than 10 matched trials' });
    if (Number.isFinite(a.rt_correlation) && a.rt_correlation < 0.99) {
        problems.push({ sev: 'halt', msg: `J002 RT correlation ${a.rt_correlation.toFixed(4)} < 0.99` });
    }
    if (Number.isFinite(a.congruency_agreement) && a.congruency_agreement < 1.0) {
        problems.push({ sev: 'halt', msg: `J003 congruency agreement ${(a.congruency_agreement * 100).toFixed(1)}% < 100%` });
    }
    if (Number.isFinite(a.eeg_offset_ms) && (a.eeg_offset_ms < 0 || a.eeg_offset_ms > 100)) {
        problems.push({ sev: 'warn', msg: `J004 offset ${a.eeg_offset_ms.toFixed(1)} ms outside 0–100 ms` });
    }
    if (a.unmatched_eeg || a.unmatched_beh) {
        problems.push({ sev: 'warn', msg: `J006 EEG/beh trial count mismatch` });
    }
    if (problems.some(p => p.sev === 'halt')) {
        return { severity: 'halt', label: 'HALT', detail: problems.map(p => p.msg).join(' · ') };
    }
    if (problems.length) {
        return { severity: 'warn', label: 'WARN', detail: problems.map(p => p.msg).join(' · ') };
    }
    return { severity: 'pass', label: 'PASS', detail: '' };
}

function fmtMs(x) { return Number.isFinite(x) ? `${x >= 0 ? '+' : ''}${x.toFixed(1)} ms` : '—'; }
function fmtR(x)  { return Number.isFinite(x) ? x.toFixed(4) : '—'; }
function fmtPct(x) { return Number.isFinite(x) ? `${(x * 100).toFixed(1)}%` : '—'; }

// ── Validity checks panel ──
let checksShowInfo = false;

function renderChecksPanel(checks) {
    const panel = document.getElementById('checks-panel');
    if (!panel) return;
    if (!checks || !checks.length) {
        panel.hidden = true;
        return;
    }
    panel.hidden = false;
    // Summary counts
    const halt = checks.filter(c => c.level === 'HALT').length;
    const warn = checks.filter(c => c.level === 'WARN').length;
    const info = checks.filter(c => c.level === 'INFO').length;
    const parts = [];
    if (halt) parts.push(`${halt} HALT`);
    if (warn) parts.push(`${warn} WARN`);
    if (info) parts.push(`${info} INFO`);
    document.getElementById('checks-summary').textContent = parts.join(' · ') || 'no checks';

    // Toggle logic
    const toggle = document.getElementById('checks-toggle');
    toggle.textContent = checksShowInfo ? 'Hide INFO' : 'Show INFO';
    toggle.onclick = () => { checksShowInfo = !checksShowInfo; renderChecksPanel(checks); };

    // Rows — highest severity first, but keep grouped by code family after that
    const rows = [...checks];
    const levelOrder = { HALT: 0, WARN: 1, INFO: 2 };
    rows.sort((a, b) => (levelOrder[a.level] - levelOrder[b.level]) || a.code.localeCompare(b.code));

    const visible = checksShowInfo ? rows : rows.filter(c => c.level !== 'INFO');
    const list = document.getElementById('checks-list');
    if (!visible.length) {
        list.innerHTML = `<div class="check-empty">No issues at this severity level (${info} INFO hidden — click Show INFO).</div>`;
        return;
    }
    list.innerHTML = visible.map(c => {
        const cls = c.level.toLowerCase();
        return `<div class="check-row check-${cls}">
            <span class="pill ${cls}">${c.level}</span>
            <span class="check-code">${escapeHtml(c.code)}</span>
            <span class="check-message">${escapeHtml(c.message)}</span>
        </div>`;
    }).join('');
}

// Fetch and render the current subject's alignment (if any behavioural
// data has been uploaded server-side but the current page doesn't have
// it in memory yet).
async function refreshAlignmentForCurrentSubject() {
    if (!currentResultId) return;
    try {
        const resp = await fetch(`${API}/api/behavioural/${encodeURIComponent(currentResultId)}`);
        if (!resp.ok) return;
        const d = await resp.json();
        renderAlignmentPanel(d.alignment, d.accuracy);
        renderChecksPanel(d.checks);
    } catch (_) { /* backend unreachable — leave panel hidden */ }
}

function setBehaviouralStatus(msg, loaded) {
    const el = document.getElementById('beh-status');
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle('loaded', !!loaded);
}

function initBehaviouralUpload() {
    const input = document.getElementById('beh-file-input');
    if (!input) return;

    input.addEventListener('change', async () => {
        if (!input.files || !input.files.length) return;
        const files = Array.from(input.files);

        // Group by canonical subject_id (same rule as EEG upload). Fall
        // back to a `subject-NNN` group key for files whose names carry
        // OpenSesame's `subject-NNN` convention; the backend will
        // resolve those to `S<n>P<nn>` via subject_nr.
        const groups = new Map();
        for (const f of files) {
            let sid = subjectIdFromFilename(f.name);
            // If the filename didn't yield a real S<n>P<nn>, try to pull
            // a subject-NNN grouping so we still submit multi-part files
            // together.
            if (!/^S\d+P\d+$/i.test(sid)) {
                const m = f.name.match(/subject[-_ ]?(\d+)/i);
                sid = m ? `subject-${m[1]}` : sid;
            }
            if (!groups.has(sid)) groups.set(sid, []);
            groups.get(sid).push(f);
        }

        let uploaded = 0;
        let matched = 0;
        setBehaviouralStatus('Uploading…', false);
        for (const [sid, group] of groups.entries()) {
            const fd = new FormData();
            for (const f of group) fd.append('files', f);
            try {
                const resp = await fetch(`${API}/api/behavioural/upload`, { method: 'POST', body: fd });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({ detail: 'Upload failed' }));
                    setBehaviouralStatus(`⚠ ${sid}: ${err.detail || 'Upload failed'}`, false);
                    continue;
                }
                const data = await resp.json();
                uploaded++;
                if (data.alignment && data.alignment.length) matched++;
                // If this behavioural session is for the currently-displayed
                // subject, refresh the alignment panel immediately.
                if (currentResultId && data.subject_id === currentResultId) {
                    renderAlignmentPanel(data.alignment, data.accuracy);
                    renderChecksPanel(data.checks);
                }
            } catch (err) {
                setBehaviouralStatus(`⚠ ${sid}: ${err.message || 'Upload failed'}`, false);
            }
        }

        setBehaviouralStatus(
            `${uploaded} subject${uploaded !== 1 ? 's' : ''} loaded · ${matched} aligned to EEG`,
            uploaded > 0,
        );
        document.getElementById('beh-upload-label').textContent = 'Upload more CSVs';
        input.value = '';
    });
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
    // Blocks — show refresh-rate labels if we know them
    const bo = s.demographics && s.demographics.block_order || {};
    const blockLabel = (bo['1'] || bo['2'])
        ? `${s.n_blocks} · ${bo['1'] || '?'} / ${bo['2'] || '?'}`
        : `${s.n_blocks}`;
    document.getElementById('info-blocks').textContent = blockLabel;
    document.getElementById('info-trials').textContent =
        `${s.n_trials} · ${s.n_congruent} con / ${s.n_incongruent} inc`;

    // Per-recording adaptive thresholds (Tasks 4/5) — show the values THIS
    // recording self-calibrated to, so the analyst sees the pipeline adapted.
    const adaptiveItem = document.getElementById('info-adaptive-item');
    const adaptiveEl = document.getElementById('info-adaptive');
    if (adaptiveItem && adaptiveEl && s.adaptive) {
        const blink = s.adaptive.blink_threshold_uv;
        const emg = s.adaptive.emg_threshold;
        if (blink || emg) {
            adaptiveEl.textContent =
                `blink ${Number(blink).toFixed(0)}µV · EMG ${Number(emg).toLocaleString()}µV²`;
            adaptiveItem.hidden = false;
        } else {
            adaptiveItem.hidden = true;
        }
    }

    // Channel names
    document.getElementById('ch1-name').textContent = s.theta.channel;
    document.getElementById('ch2-name').textContent = s.beta.channel;

    // Demographics panel (only shown if matched)
    renderDemographicsPanel(s.demographics);

    // Behavioural-alignment panel (only shown if an OpenSesame CSV has been
    // aligned to this subject's EEG). The upload endpoint returns alignment
    // on demand; results endpoint doesn't yet include it, so we pull it.
    renderAlignmentPanel(data.alignment, data.accuracy);
    refreshAlignmentForCurrentSubject();

    // Validity checks (always emitted by the backend when EEG is loaded)
    renderChecksPanel(data.checks);

    // Theta card
    setCard('theta', s.theta, s.demographics);
    // Beta card
    setCard('beta', s.beta, s.demographics);

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
    const blockOrder = (s.demographics && s.demographics.block_order) || {};
    renderPerTrialChart('chart-trials-theta', data.trials, 'theta_rel', 'fz_exclude', 'Trial θ relative power', blockOrder);
    renderPerTrialChart('chart-trials-beta',  data.trials, 'beta_rel',  'c3_exclude', 'Trial β relative power', blockOrder);
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

function renderExcludedCard(prefix, band, card) {
    // Show survival bookkeeping (so the reader sees WHY) but no power values.
    const survEl = document.getElementById(`${prefix}-survival`);
    if (survEl) {
        survEl.textContent =
            `${band.surviving}/${band.surviving + band.excluded} trials — power not reported`;
    }
    // Blank the numeric readouts.
    for (const id of [`${prefix}-rel-con`, `${prefix}-rel-inc`]) {
        const el = document.getElementById(id); if (el) el.textContent = '—';
    }
    for (const id of [`${prefix}-abs-con`, `${prefix}-abs-inc`]) {
        const el = document.getElementById(id); if (el) el.textContent = 'abs —';
    }
    for (const id of [`${prefix}-bar-con`, `${prefix}-bar-inc`]) {
        const el = document.getElementById(id); if (el) el.style.width = '0%';
    }
    const blocksEl = document.getElementById(`${prefix}-blocks`);
    if (blocksEl) blocksEl.innerHTML = '';
    const flag = document.getElementById(`${prefix}-balance`);
    if (flag) flag.hidden = true;
    if (!card) return;
    card.classList.add('channel-excluded');
    let notice = card.querySelector('.channel-excluded-notice');
    if (!notice) {
        notice = document.createElement('div');
        notice.className = 'channel-excluded-notice';
        card.insertBefore(notice, card.firstChild);
    }
    const code = band.exclusion_code || 'excluded';
    const reason = band.exclusion_reason || 'contamination';
    notice.innerHTML =
        `<span class="pill warn">${escapeHtml(code)}</span> ` +
        `Channel excluded — ${escapeHtml(reason)}. Power not reported.`;
}

function setCard(prefix, band, demographics) {
    // Channel-scoped exclusion (Task 7): when this derivation/band was
    // invalidated (e.g. S005 on C3-C4 beta), the backend nulls all power
    // values. Show the exclusion notice instead of NaN and stop.
    const card = document.getElementById(`${prefix}-rel-con`)?.closest('.power-card');
    if (band.channel_excluded) {
        renderExcludedCard(prefix, band, card);
        return;
    }
    if (card) card.classList.remove('channel-excluded');
    const oldNotice = card && card.querySelector('.channel-excluded-notice');
    if (oldNotice) oldNotice.remove();

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

    // Per-block breakdown (only render when 2+ blocks)
    const blocksEl = document.getElementById(`${prefix}-blocks`);
    if (!blocksEl) return;
    const byBlock = band.by_block || {};
    const blockKeys = Object.keys(byBlock).sort((a, b) => Number(a) - Number(b));
    if (blockKeys.length < 2) {
        blocksEl.innerHTML = '';
        return;
    }
    const blockOrder = (demographics && demographics.block_order) || {};
    const overallMax = Math.max(
        ...blockKeys.flatMap(k => [byBlock[k].rel_median_con, byBlock[k].rel_median_inc]),
        0.001
    );
    blocksEl.innerHTML = `
        <div class="card-blocks-header">Per-block breakdown</div>
        ${blockKeys.map(k => {
            const b = byBlock[k];
            const label = blockOrder[k] ? `Block ${k} · ${escapeHtml(blockOrder[k])}` : `Block ${k}`;
            const nSurv = b.n_surv_con + b.n_surv_inc;
            const nTot = b.n_total_con + b.n_total_inc;
            const cWidth = (b.rel_median_con / overallMax) * 50;
            const iWidth = (b.rel_median_inc / overallMax) * 50;
            return `
                <div class="card-block-row">
                    <div class="card-block-label">${label}</div>
                    <div class="card-block-values">
                        <div class="val-group congruent">
                            <span class="val-label">Con</span>
                            <span class="val-number">${b.rel_median_con.toFixed(3)}</span>
                            <span class="val-secondary">abs ${b.abs_median_con.toFixed(2)}</span>
                        </div>
                        <div class="val-divider"></div>
                        <div class="val-group incongruent">
                            <span class="val-label">Inc</span>
                            <span class="val-number">${b.rel_median_inc.toFixed(3)}</span>
                            <span class="val-secondary">abs ${b.abs_median_inc.toFixed(2)}</span>
                        </div>
                    </div>
                    <div class="card-block-bar">
                        <div class="bar-con" style="width:${cWidth}%"></div>
                        <div class="bar-inc" style="width:${iWidth}%"></div>
                    </div>
                    <div class="card-block-footer">
                        ${nSurv}/${nTot} surviving · con ${b.exclusion_pct_con}% / inc ${b.exclusion_pct_inc}% excluded
                    </div>
                </div>
            `;
        }).join('')}
    `;
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
function renderPerTrialChart(containerId, trials, powerKey, excludeKey, yLabel, blockOrder = {}) {
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

    // Find first trial of block 2 (if any) to draw a divider
    const shapes = [];
    const annotations = [];
    const block2First = trials.find(t => t.block === 2);
    if (block2First) {
        const xBoundary = block2First.trial - 0.5;
        shapes.push({
            type: 'line',
            x0: xBoundary, x1: xBoundary,
            y0: 0, y1: 1, yref: 'paper',
            line: { color: 'rgba(255,255,255,0.35)', width: 1, dash: 'dash' },
        });
        if (blockOrder && (blockOrder['1'] || blockOrder['2'])) {
            const midB1 = (trials.find(t => t.block === 1)?.trial ?? 1);
            const lastB1 = [...trials].reverse().find(t => t.block === 1)?.trial ?? xBoundary;
            const lastB2 = [...trials].reverse().find(t => t.block === 2)?.trial ?? block2First.trial;
            annotations.push({
                x: (midB1 + lastB1) / 2, y: 1, yref: 'paper',
                text: `Block 1 · ${blockOrder['1'] || '?'}`,
                showarrow: false, font: { size: 10, color: 'rgba(255,255,255,0.75)' },
                yanchor: 'bottom',
            });
            annotations.push({
                x: (block2First.trial + lastB2) / 2, y: 1, yref: 'paper',
                text: `Block 2 · ${blockOrder['2'] || '?'}`,
                showarrow: false, font: { size: 10, color: 'rgba(255,255,255,0.75)' },
                yanchor: 'bottom',
            });
        }
    }

    const layout = {
        ...plotlyLayout,
        xaxis: { ...plotlyLayout.xaxis, title: { text: 'Trial number', font: { size: 10 } } },
        yaxis: { ...plotlyLayout.yaxis, title: { text: yLabel, font: { size: 10 } } },
        shapes,
        annotations,
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
        // Only show demographic columns if at least one subject has a match
        const anyDemo = subjects.some(s => s.summary.demographics && s.summary.demographics.matched);
        const demoCols = anyDemo
            ? '<th>Age</th><th>Sex</th><th>Hand</th><th>Block order</th>'
            : '';
        thead.innerHTML = `<tr>
            <th></th><th>Subject</th><th>Date</th>
            ${demoCols}
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
            let demoTds = '';
            if (anyDemo) {
                const d = s.demographics || {};
                const findField = (key) => (d.fields || []).find(f => f.key === key)?.value || '';
                const bo = d.block_order || {};
                const blockOrder = (bo['1'] || bo['2'])
                    ? `${bo['1'] || '?'} / ${bo['2'] || '?'}`
                    : (d.aborted ? 'aborted' : '—');
                demoTds = `
                    <td>${escapeHtml(findField('age')) || '—'}</td>
                    <td>${escapeHtml(findField('sex')) || '—'}</td>
                    <td>${escapeHtml(findField('handedness')) || '—'}</td>
                    <td>${escapeHtml(blockOrder)}</td>
                `;
            }
            return `<tr>
                <td><span class="chip-dot" style="background:${SUBJECT_COLORS[i % SUBJECT_COLORS.length]};display:inline-block;width:8px;height:8px;border-radius:50%"></span></td>
                <td>${escapeHtml(s.filename)}</td>
                <td>${escapeHtml(s.recording_date)}</td>
                ${demoTds}
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
// Set DEBUG_EXPAND=true to trace the expand/collapse path in the console.
const DEBUG_EXPAND = false;
function logExpand(...args) {
    if (DEBUG_EXPAND) console.log('[expand]', ...args);
}
function snapshotPanel(panel, label) {
    if (!DEBUG_EXPAND) return;
    const container = panel.querySelector('.chart-container');
    const pRect = panel.getBoundingClientRect();
    const cRect = container ? container.getBoundingClientRect() : null;
    const cs = getComputedStyle(panel);
    const csC = container ? getComputedStyle(container) : null;
    const ancestors = [];
    let node = panel.parentElement;
    while (node && node !== document.body) {
        const a = getComputedStyle(node);
        if (a.transform !== 'none' || a.filter !== 'none' || a.perspective !== 'none' ||
            a.willChange !== 'auto' || a.contain !== 'none' || a.overflow !== 'visible') {
            ancestors.push({
                tag: node.tagName.toLowerCase() + (node.id ? '#' + node.id : '') +
                     (node.className ? '.' + String(node.className).split(' ').join('.') : ''),
                transform: a.transform, filter: a.filter, perspective: a.perspective,
                willChange: a.willChange, contain: a.contain, overflow: a.overflow,
            });
        }
        node = node.parentElement;
    }
    const tag = `[expand:${label}] ${panel.dataset.chartId || '?'}`;
    console.log(`${tag}  class="${panel.className}"`);
    console.log(`${tag}  panelRect  x=${pRect.x.toFixed(0)} y=${pRect.y.toFixed(0)} w=${pRect.width.toFixed(0)} h=${pRect.height.toFixed(0)}`);
    if (cRect)
        console.log(`${tag}  contRect   x=${cRect.x.toFixed(0)} y=${cRect.y.toFixed(0)} w=${cRect.width.toFixed(0)} h=${cRect.height.toFixed(0)}`);
    console.log(`${tag}  panel css: position=${cs.position} z=${cs.zIndex} top=${cs.top} left=${cs.left} w=${cs.width} h=${cs.height} display=${cs.display}`);
    if (csC)
        console.log(`${tag}  cont  css: position=${csC.position} display=${csC.display} w=${csC.width} h=${csC.height} flex=${csC.flex}`);
    console.log(`${tag}  viewport ${window.innerWidth}x${window.innerHeight}  scrollY=${window.scrollY}  body.chart-expanded-open=${document.body.classList.contains('chart-expanded-open')}  backdrop=${!!document.querySelector('.chart-backdrop')}  #expanded=${document.querySelectorAll('.chart-panel.expanded').length}`);
    if (ancestors.length) {
        console.warn(`${tag}  positioned/clipping ancestors (may trap position:fixed): ${ancestors.length}`);
        ancestors.forEach((a, i) => {
            console.warn(`${tag}    [${i}] ${a.tag}  transform=${a.transform}  filter=${a.filter}  perspective=${a.perspective}  willChange=${a.willChange}  contain=${a.contain}  overflow=${a.overflow}`);
        });
    }
}

function stopPanelClick(e) { e.stopPropagation(); }

function initChartExpand() {
    // Inject an "Expand" button into every chart panel header, and wire it up.
    // Works for both individual and compare screens because we run this once at
    // startup and again after each render pass (see enhanceChartPanels()).
    enhanceChartPanels();

    // Escape closes any expanded panel
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            logExpand('keydown Escape → collapseAllCharts()');
            collapseAllCharts();
        }
    });
    logExpand('initChartExpand() done. panels enhanced:',
        document.querySelectorAll('.chart-panel').length);
}

function enhanceChartPanels() {
    document.querySelectorAll('.chart-panel').forEach(panel => {
        const h3 = panel.querySelector('h3');
        if (!h3) return;

        // Give each panel a stable id for logging
        if (!panel.dataset.chartId) {
            const c = panel.querySelector('.chart-container');
            panel.dataset.chartId = (c && c.id) || h3.textContent.trim().slice(0, 40);
        }

        // Ensure the header has a single action wrapper on the right
        let actions = h3.querySelector('.chart-header-actions');
        if (!actions) {
            actions = document.createElement('span');
            actions.className = 'chart-header-actions';
            h3.appendChild(actions);
        }

        // Avg / All trials toggle — only for the spectrum panels
        const container = panel.querySelector('.chart-container');
        const isSpectrum = container && (container.id === 'chart-spec-theta' || container.id === 'chart-spec-beta');
        if (isSpectrum && !actions.querySelector('.chart-mode-btn')) {
            panel.dataset.mode = panel.dataset.mode || 'avg';
            const toggle = document.createElement('button');
            toggle.className = 'chart-mode-btn';
            toggle.type = 'button';
            const paintToggle = () => {
                const isAll = panel.dataset.mode === 'all';
                toggle.textContent = isAll ? 'View: All trials' : 'View: Avg';
                toggle.classList.toggle('active', isAll);
            };
            paintToggle();
            toggle.title = 'Switch between averaged and per-trial view';
            toggle.addEventListener('click', (e) => {
                e.stopPropagation();
                panel.dataset.mode = panel.dataset.mode === 'all' ? 'avg' : 'all';
                paintToggle();
                drawSpectrumChart(container.id, panel.dataset.mode);
            });
            actions.appendChild(toggle);
        }

        // Expand button on every panel
        if (!actions.querySelector('.chart-expand-btn')) {
            const btn = document.createElement('button');
            btn.className = 'chart-expand-btn';
            btn.type = 'button';
            btn.textContent = '⤢ Expand';
            btn.title = 'Expand chart (Esc to collapse)';
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                logExpand('button clicked on', panel.dataset.chartId, 'currently expanded?', panel.classList.contains('expanded'));
                toggleChartExpand(panel, btn);
            });
            actions.appendChild(btn);
        }
    });
}

function toggleChartExpand(panel, btn) {
    const id = panel.dataset.chartId || '?';
    logExpand('toggleChartExpand →', id, '(before)');
    snapshotPanel(panel, 'before toggle');

    const willExpand = !panel.classList.contains('expanded');

    // ── Portal-in / portal-out ──
    // Ancestors with `animation: fadeUp` keep a `transform` after the animation
    // finishes (fill-mode both), which makes them the containing block for any
    // `position: fixed` descendant. The panel then anchors to that ancestor
    // instead of the viewport and can render off-screen if the page is scrolled.
    // Fix: when expanding, move the panel out to <body>; when collapsing,
    // put it back where it was.
    if (willExpand) {
        if (!panel.dataset.portalHome) {
            const parent = panel.parentElement;
            const next = panel.nextElementSibling;
            // Insert a placeholder so we can restore the exact original spot
            const placeholder = document.createElement('div');
            placeholder.className = 'chart-panel-placeholder';
            placeholder.style.display = 'none';
            placeholder.dataset.portalFor = id;
            parent.insertBefore(placeholder, panel);
            panel.dataset.portalHome = '1';
            panel._portalPlaceholder = placeholder;
            document.body.appendChild(panel);
            logExpand('portaled panel to <body>. placeholder inserted in', parent.tagName.toLowerCase() + (parent.id ? '#' + parent.id : ''));
        }
    }

    const expanded = panel.classList.toggle('expanded');
    btn.textContent = expanded ? '× Collapse' : '⤢ Expand';
    logExpand('classList.toggle("expanded") →', expanded, 'on', id);

    // Backdrop + body-scroll lock
    let backdrop = document.querySelector('.chart-backdrop');
    if (expanded) {
        // Collapse any other expanded panel first
        document.querySelectorAll('.chart-panel.expanded').forEach(p => {
            if (p !== panel) {
                logExpand('collapsing sibling', p.dataset.chartId);
                p.classList.remove('expanded');
                const b = p.querySelector('.chart-expand-btn');
                if (b) b.textContent = '⤢ Expand';
                restorePanelFromPortal(p);
            }
        });
        if (!backdrop) {
            logExpand('creating backdrop');
            backdrop = document.createElement('div');
            backdrop.className = 'chart-backdrop';
            backdrop.addEventListener('click', () => {
                logExpand('backdrop clicked → collapseAllCharts()');
                collapseAllCharts();
            });
            document.body.appendChild(backdrop);
        }
        document.body.classList.add('chart-expanded-open');
        panel.addEventListener('click', stopPanelClick);
    } else {
        if (backdrop) {
            logExpand('removing backdrop');
            backdrop.remove();
        }
        document.body.classList.remove('chart-expanded-open');
        panel.removeEventListener('click', stopPanelClick);
        restorePanelFromPortal(panel);
    }

    // Snapshot right after class toggle (before rAF resize)
    snapshotPanel(panel, 'after class toggle');

    // Ask Plotly to resize after the CSS transition has settled. Two rAFs is
    // the reliable way to wait for the browser to compute the new box.
    const container = panel.querySelector('.chart-container');
    if (container && window.Plotly) {
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                try {
                    logExpand('Plotly.Plots.resize(', container.id || '(no id)', ')');
                    Plotly.Plots.resize(container);
                    snapshotPanel(panel, 'after Plotly.resize');
                } catch (err) {
                    console.error('[expand] Plotly.resize error', err);
                }
            });
        });
    } else {
        logExpand('no Plotly or no container — skipping resize');
    }
}

function restorePanelFromPortal(panel) {
    if (!panel.dataset.portalHome) return;
    const placeholder = panel._portalPlaceholder;
    if (placeholder && placeholder.parentNode) {
        placeholder.parentNode.insertBefore(panel, placeholder);
        placeholder.remove();
        logExpand('restored panel', panel.dataset.chartId, 'to original DOM position');
    } else {
        console.warn('[expand] cannot restore panel — placeholder missing', panel.dataset.chartId);
    }
    delete panel.dataset.portalHome;
    delete panel._portalPlaceholder;
}

function collapseAllCharts() {
    const expanded = document.querySelectorAll('.chart-panel.expanded');
    logExpand('collapseAllCharts() → count =', expanded.length);
    expanded.forEach(panel => {
        panel.classList.remove('expanded');
        const btn = panel.querySelector('.chart-expand-btn');
        if (btn) btn.textContent = '⤢ Expand';
        restorePanelFromPortal(panel);
        const container = panel.querySelector('.chart-container');
        if (container && window.Plotly) {
            requestAnimationFrame(() => {
                requestAnimationFrame(() => {
                    try { Plotly.Plots.resize(container); } catch (_) {}
                });
            });
        }
    });
    const backdrop = document.querySelector('.chart-backdrop');
    if (backdrop) backdrop.remove();
    document.body.classList.remove('chart-expanded-open');
}

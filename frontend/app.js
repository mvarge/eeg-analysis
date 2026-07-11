/* ============================================
   EEG Flanker Analysis — App Logic
   Wavelet-based pipeline with per-channel exclusion
   ============================================ */

const API = window.location.origin;
let currentResultId = null;
let currentData = null;              // last upload's full response
let uploadedSubjects = [];           // [{ result_id, filename }]
let stagedFiles = [];                // File[] waiting to be analysed
// Per-block card label mode: 'refresh' → "60 Hz block" (default when
// demographics are known), 'block' → "Block 1/2" for validity checking.
let powerBlockLabelMode = 'refresh';

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
        if (files.length > 0) stageFiles(files);
    });
    fileInput.addEventListener('change', () => {
        const files = Array.from(fileInput.files).filter(f => f.name.endsWith('.txt'));
        if (files.length > 0) stageFiles(files);
        fileInput.value = '';   // allow re-selecting the same file later
    });

    // Staging controls
    document.getElementById('staging-add').addEventListener('click', () => fileInput.click());
    document.getElementById('staging-clear').addEventListener('click', () => {
        stagedFiles = [];
        renderStaging();
    });
    document.getElementById('staging-analyze').addEventListener('click', () => {
        if (stagedFiles.length > 0) uploadFiles(stagedFiles.slice());
    });

    // Demographics CSV
    initDemographicsUpload();
    initBehaviouralUpload();
    refreshDemographicsStatus();

    // ── Stage files for review before analysis ──
    // Files chosen/dropped are added to a reviewable list (deduped by
    // name+size) rather than uploaded immediately, so the analyst can drop a
    // stray non-recording export before it breaks a subject.
    function stageFiles(files) {
        document.getElementById('upload-summary').hidden = true;
        errorEl.hidden = true;
        for (const f of files) {
            const dup = stagedFiles.some(s => s.name === f.name && s.size === f.size);
            if (!dup) stagedFiles.push(f);
        }
        renderStaging();
    }

    // Group the staged files by canonical subject ID for display and upload.
    function groupStaged() {
        const groups = new Map();  // subject_id -> File[]
        for (const f of stagedFiles) {
            const sid = subjectIdFromFilename(f.name);
            if (!groups.has(sid)) groups.set(sid, []);
            groups.get(sid).push(f);
        }
        return groups;
    }

    function renderStaging() {
        const staging = document.getElementById('staging');
        const groupsEl = document.getElementById('staging-groups');
        const countEl = document.getElementById('staging-count');
        if (!stagedFiles.length) {
            staging.hidden = true;
            groupsEl.innerHTML = '';
            countEl.textContent = '';
            return;
        }
        staging.hidden = false;
        const groups = groupStaged();
        countEl.textContent = `(${stagedFiles.length} file${stagedFiles.length > 1 ? 's' : ''}, ${groups.size} subject${groups.size > 1 ? 's' : ''})`;

        groupsEl.innerHTML = Array.from(groups.entries()).map(([sid, group]) => {
            const rows = group.map(f => `
                <div class="staging-file">
                    <span class="staging-file-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
                    <span class="staging-file-size">${formatBytes(f.size)}</span>
                    <button type="button" class="staging-file-remove" data-name="${escapeHtml(f.name)}" data-size="${f.size}" title="Remove file">×</button>
                </div>`).join('');
            return `
            <div class="staging-group">
                <div class="staging-group-head">
                    <span class="staging-group-id">${escapeHtml(sid)}</span>
                    <span class="staging-group-meta">${group.length} file${group.length > 1 ? 's' : ''}</span>
                </div>
                ${rows}
            </div>`;
        }).join('');

        groupsEl.querySelectorAll('.staging-file-remove').forEach(btn => {
            btn.addEventListener('click', () => {
                const name = btn.dataset.name;
                const size = Number(btn.dataset.size);
                stagedFiles = stagedFiles.filter(f => !(f.name === name && f.size === size));
                renderStaging();
            });
        });
    }
    // Expose for the post-run summary retry handler.
    initUpload._renderStaging = renderStaging;
    initUpload._groupStaged = groupStaged;

    async function uploadFiles(files) {
        errorEl.hidden = true;
        document.getElementById('upload-summary').hidden = true;
        document.getElementById('staging').hidden = true;
        progress.hidden = false;
        const fill = progress.querySelector('.progress-fill');
        const progressText = progress.querySelector('.progress-text');
        fill.classList.remove('indeterminate');
        fill.style.width = '0%';
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
        const succeeded = [];   // subject IDs that processed OK this run
        const failed = [];      // { sid, files: File[], message }
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
                    const err = await resp.json().catch(() => ({}));
                    throw new Error(err.detail || `HTTP ${resp.status}`);
                }
                const data = await resp.json();
                lastData = data;
                count++;
                succeeded.push(sid);

                if (!uploadedSubjects.find(s => s.result_id === data.result_id)) {
                    uploadedSubjects.push({
                        result_id: data.result_id,
                        filename: data.summary.filename,
                    });
                }
            } catch (err) {
                // Don't abort the whole run — record the failure, keep going,
                // so subjects that DO parse are still available afterwards.
                failed.push({ sid, files: group, message: err.message });
            }
        }

        fill.classList.remove('indeterminate');
        fill.style.width = '100%';
        progressText.textContent = `Done — ${count} succeeded, ${failed.length} failed.`;

        // Keep only the failed files staged so they can be fixed and retried.
        stagedFiles = failed.flatMap(f => f.files);

        // If nothing at all succeeded and nothing had been uploaded before,
        // just show the error(s) inline (nowhere useful to continue to).
        const totalAvailable = uploadedSubjects.length;

        if (failed.length === 0) {
            // Clean run — go straight to results as before.
            setTimeout(() => {
                progress.hidden = true;
                if (totalAvailable === 1) {
                    showResults(lastData);
                } else {
                    document.getElementById('upload-section').hidden = true;
                    showComparison();
                }
            }, 500);
            return;
        }

        // Mixed / failed run — show a summary with an explicit way forward so
        // the analyst is never stranded on the upload screen.
        setTimeout(() => {
            progress.hidden = true;
            showUploadSummary(succeeded, failed, totalAvailable, lastData);
        }, 400);
    }

    function showUploadSummary(succeeded, failed, totalAvailable, lastData) {
        const box = document.getElementById('upload-summary');
        const body = document.getElementById('upload-summary-body');
        const continueBtn = document.getElementById('upload-summary-continue');
        const retryBtn = document.getElementById('upload-summary-retry');

        const lines = [];
        if (succeeded.length) {
            lines.push(`<div class="upload-summary-line ok">✓ ${succeeded.length} subject${succeeded.length > 1 ? 's' : ''} processed: <span class="us-detail">${escapeHtml(succeeded.join(', '))}</span></div>`);
        }
        for (const f of failed) {
            lines.push(`<div class="upload-summary-line fail">✕ ${escapeHtml(f.sid)} failed <span class="us-detail">${escapeHtml(f.message)}</span></div>`);
        }
        if (totalAvailable > 0) {
            lines.push(`<div class="upload-summary-line"><span class="us-detail">${totalAvailable} subject${totalAvailable > 1 ? 's' : ''} available to view. Remove or fix the failed file(s) below and analyse again, or continue.</span></div>`);
        } else {
            lines.push(`<div class="upload-summary-line"><span class="us-detail">No subjects available yet. Remove the offending file(s) below and analyse again.</span></div>`);
        }
        body.innerHTML = lines.join('');

        // "Continue" only makes sense if there's something to view.
        continueBtn.hidden = totalAvailable === 0;
        continueBtn.onclick = () => {
            box.hidden = true;
            if (totalAvailable === 1) {
                if (lastData) showResults(lastData);
                else loadSubject(uploadedSubjects[0].result_id);
            } else {
                document.getElementById('upload-section').hidden = true;
                showComparison();
            }
        };

        retryBtn.hidden = stagedFiles.length === 0;
        retryBtn.onclick = () => {
            box.hidden = true;
            if (initUpload._renderStaging) initUpload._renderStaging();
        };

        box.hidden = false;
        // Re-show the staging list (now holding only the failed files).
        if (initUpload._renderStaging) initUpload._renderStaging();
    }
}

// Human-readable file size.
function formatBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
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
                    ? ` · excluded θ <b>${acc.eeg_error_trials_dropped.theta}</b> / β <b>${acc.eeg_error_trials_dropped.beta}</b> (incorrect response)`
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

// Behavioural accuracy box (Doc 6). Renders per-block + overall accuracy and
// how many EEG trials were excluded for an incorrect response. Hidden when no
// behavioural data is aligned.
function renderAccuracyPanel(accuracy) {
    const panel = document.getElementById('accuracy-panel');
    if (!panel) return;
    if (!accuracy || !accuracy.length) {
        panel.hidden = true;
        return;
    }
    panel.hidden = false;

    const totBeh = accuracy.reduce((s, a) => s + (a.n_beh_trials || 0), 0);
    const totErr = accuracy.reduce((s, a) => s + (a.n_errors || 0), 0);
    const overall = totBeh ? (totBeh - totErr) / totBeh : NaN;
    const totDropTheta = accuracy.reduce((s, a) => s + (a.eeg_error_trials_dropped?.theta || 0), 0);
    const totDropBeta = accuracy.reduce((s, a) => s + (a.eeg_error_trials_dropped?.beta || 0), 0);

    const summary = document.getElementById('accuracy-summary');
    if (summary) {
        summary.textContent = `overall ${fmtPct(overall)} · ${totErr} incorrect trial${totErr === 1 ? '' : 's'} excluded`;
    }

    const body = document.getElementById('accuracy-body');
    const cards = accuracy.map(a => {
        const dropped = (a.eeg_error_trials_dropped?.theta || 0);
        const errBadge = a.n_errors
            ? `<span class="acc-err">${a.n_errors} incorrect</span>`
            : `<span class="acc-ok">no errors</span>`;
        return `<div class="acc-card">
            <div class="acc-card-head">Block ${a.block} ${errBadge}</div>
            <div class="acc-figure">${fmtPct(a.accuracy)}</div>
            <div class="acc-detail">
                ${a.n_beh_trials} trials · ${a.n_beh_trials - a.n_errors} correct
                ${a.n_errors ? `<br>excluded ${dropped} EEG epoch${dropped === 1 ? '' : 's'} (incorrect response)` : ''}
                ${(a.matched_error_con || a.matched_error_inc)
                    ? `<br><span class="acc-cond">errors: ${a.matched_error_con} congruent · ${a.matched_error_inc} incongruent</span>`
                    : ''}
            </div>
        </div>`;
    }).join('');
    const overallCard = `<div class="acc-card acc-card-overall">
        <div class="acc-card-head">Overall</div>
        <div class="acc-figure">${fmtPct(overall)}</div>
        <div class="acc-detail">${totBeh} trials · ${totErr} incorrect<br>
            excluded θ ${totDropTheta} / β ${totDropBeta} epoch${(totDropTheta === 1 && totDropBeta === 1) ? '' : 's'}</div>
    </div>`;
    body.innerHTML = overallCard + cards;
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

// Manual review annotations for validity checks. Clicking a check cycles its
// state (default → ok → flag → default). Persisted in localStorage keyed by
// subject + check code so it survives reloads and subject switches.
const CHECKS_REVIEW_KEY = 'eeg.checksReview.v1';
function loadChecksReview() {
    try { return JSON.parse(localStorage.getItem(CHECKS_REVIEW_KEY)) || {}; }
    catch (_) { return {}; }
}
function saveChecksReview(state) {
    try { localStorage.setItem(CHECKS_REVIEW_KEY, JSON.stringify(state)); }
    catch (_) { /* storage full / disabled — degrade to session-only */ }
}
let checksReview = loadChecksReview();

function reviewKey(code) {
    return `${currentResultId || '_'}::${code}`;
}
function cycleReviewState(current) {
    // default (undefined) → ok → flag → default
    if (!current) return 'ok';
    if (current === 'ok') return 'flag';
    return null;
}

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
        const review = checksReview[reviewKey(c.code)];
        const reviewCls = review ? ` check-review-${review}` : '';
        const mark = review === 'ok' ? '✓' : review === 'flag' ? '✕' : '';
        return `<div class="check-row check-${cls}${reviewCls}" data-code="${escapeHtml(c.code)}"
                     role="button" tabindex="0"
                     title="Click to mark reviewed (✓ ok → ✕ flagged → clear)">
            <span class="pill ${cls}">${c.level}</span>
            <span class="check-code">${escapeHtml(c.code)}</span>
            <span class="check-message">${escapeHtml(c.message)}</span>
            <span class="check-review-mark">${mark}</span>
        </div>`;
    }).join('');

    // Wire click / keyboard cycling of the manual review state.
    list.querySelectorAll('.check-row').forEach(row => {
        const code = row.dataset.code;
        const cycle = () => {
            const key = reviewKey(code);
            const next = cycleReviewState(checksReview[key]);
            if (next) checksReview[key] = next; else delete checksReview[key];
            saveChecksReview(checksReview);
            renderChecksPanel(checks);
        };
        row.addEventListener('click', cycle);
        row.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); cycle(); }
        });
    });
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
        renderAccuracyPanel(d.accuracy);
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
                // Surface task-numbering warnings (aborted/restarted flanker
                // runs: block != 80 rows, out-of-range live_row) so misnumbering
                // is never silent.
                const numWarn = (data.warnings || []).filter(w =>
                    /expected \d+|live_row|Task #|block label/i.test(w));
                if (numWarn.length) {
                    setBehaviouralStatus(`⚠ ${sid}: ${numWarn.join(' · ')}`, false);
                }
                // If this behavioural session is for the currently-displayed
                // subject, refresh the alignment panel immediately.
                if (currentResultId && data.subject_id === currentResultId) {
                    renderAlignmentPanel(data.alignment, data.accuracy);
                    renderAccuracyPanel(data.accuracy);
                    renderChecksPanel(data.checks);
                    // Trial numbering now uses flanker task numbers and the
                    // missing-trials box may have populated — reload the full
                    // results so every table reflects the new alignment.
                    loadSubject(currentResultId);
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
    saveSession({ view: 'individual', subjectId: data.result_id });

    document.getElementById('upload-section').hidden = true;
    document.getElementById('results-section').hidden = false;
    document.getElementById('refresh-section').hidden = true;
    document.getElementById('compare-section').hidden = true;

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
    renderAccuracyPanel(data.accuracy);
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
    const blockPerTrial = data.trials.map(t => t.block);
    const specBlockOrder = (s.demographics && s.demographics.block_order) || {};

    renderSpectrumChart('chart-spec-theta', {
        freqs: data.spectra.freqs,
        conData: data.spectra.theta_congruent,
        incData: data.spectra.theta_incongruent,
        excData: data.spectra.theta_excluded,
        perTrial: data.spectra.theta_per_trial,
        exclusionFlags: fzExcludeArr,
        condPerTrial,
        blockPerTrial,
        blockOrder: specBlockOrder,
    }, 'Wavelet power (µV²)', s.config.theta_band);
    renderSpectrumChart('chart-spec-beta', {
        freqs: data.spectra.freqs,
        conData: data.spectra.beta_congruent,
        incData: data.spectra.beta_incongruent,
        excData: data.spectra.beta_excluded,
        perTrial: data.spectra.beta_per_trial,
        exclusionFlags: c3ExcludeArr,
        condPerTrial,
        blockPerTrial,
        blockOrder: specBlockOrder,
    }, 'Wavelet power (µV²)', s.config.beta_band);
    const blockOrder = (s.demographics && s.demographics.block_order) || {};
    renderPerTrialChart('chart-trials-theta', data.trials, 'theta_rel', 'fz_exclude', 'Trial θ relative power', blockOrder);
    renderPerTrialChart('chart-trials-beta',  data.trials, 'beta_rel',  'c3_exclude', 'Trial β relative power', blockOrder);
    renderExclusionChart('chart-exclusion', data.trials, s.theta.channel, s.beta.channel);

    // Included + excluded trials tables
    populateIncludedTable(data.trials, blockOrder);
    populateExcludedTable(data.trials);
    populateMissingTable(data.missing_trials);

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
    const hasRefresh = blockKeys.some(k => blockOrder[k]);
    // Label mode: 'refresh' shows "60 Hz block" (default when demographics known),
    // 'block' shows "Block 1/2" for validity checking. Toggle lives in the header.
    const mode = hasRefresh ? powerBlockLabelMode : 'block';
    const overallMax = Math.max(
        ...blockKeys.flatMap(k => [byBlock[k].rel_median_con, byBlock[k].rel_median_inc]),
        0.001
    );
    const toggleHtml = hasRefresh
        ? `<button type="button" class="block-label-toggle" data-prefix="${prefix}"
                   title="Switch between refresh-rate and block labels">${mode === 'refresh' ? 'View: block' : 'View: refresh rate'}</button>`
        : '';
    blocksEl.innerHTML = `
        <div class="card-blocks-header">Per-block breakdown ${toggleHtml}</div>
        ${blockKeys.map(k => {
            const b = byBlock[k];
            const hz = blockOrder[k];
            const label = (mode === 'refresh' && hz)
                ? `${escapeHtml(hz)} block`
                : (hz ? `Block ${k} · ${escapeHtml(hz)}` : `Block ${k}`);
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

    // Wire the refresh-rate / block label toggle (re-renders both cards so
    // theta and beta stay in sync).
    const toggleBtn = blocksEl.querySelector('.block-label-toggle');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', () => {
            powerBlockLabelMode = powerBlockLabelMode === 'refresh' ? 'block' : 'refresh';
            const s = currentData && currentData.summary;
            if (s) {
                setCard('theta', s.theta, s.demographics);
                setCard('beta', s.beta, s.demographics);
            }
        });
    }
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
    const currentGroup = (panel && panel.dataset.group) || 'condition';
    drawSpectrumChart(containerId, currentMode, currentGroup);
    updateSpectrumGroupToggle(containerId);
}

// Show/hide the Condition↔Refresh grouping toggle for a spectrum panel based
// on whether the loaded data actually has refresh-rate block info.
function updateSpectrumGroupToggle(containerId) {
    const el = document.getElementById(containerId);
    const panel = el && el.closest('.chart-panel');
    if (!panel) return;
    const gToggle = panel.querySelector('.chart-group-btn');
    if (!gToggle) return;
    const spec = el.__spectrum;
    const canRefresh = spec && spec.blockPerTrial && spec.blockPerTrial.length &&
        spec.blockOrder && Object.keys(spec.blockOrder).length > 0;
    gToggle.hidden = !canRefresh;
    if (!canRefresh && panel.dataset.group === 'refresh') {
        panel.dataset.group = 'condition';
    }
}

function drawSpectrumChart(containerId, mode, group) {
    const el = document.getElementById(containerId);
    const data = el.__spectrum;
    if (!data) return;
    const { freqs, conData, incData, excData,
            perTrial, exclusionFlags, condPerTrial,
            blockPerTrial, blockOrder,
            yLabel, bandRange } = data;

    // Resolve grouping: 'condition' (con/inc, default) or 'refresh' (per block,
    // labelled by refresh rate). Fall back to condition if we lack block data.
    const panel = el.closest('.chart-panel');
    const canRefresh = blockPerTrial && blockPerTrial.length &&
        blockOrder && Object.keys(blockOrder).length > 0;
    const grp = group || (panel && panel.dataset.group) || 'condition';
    const effGroup = (grp === 'refresh' && canRefresh) ? 'refresh' : 'condition';

    // Average the per-trial spectra rows for the trials matching `pred`.
    function avgRows(pred) {
        const rows = [];
        for (let i = 0; i < perTrial.length; i++) if (pred(i)) rows.push(perTrial[i]);
        if (!rows.length) return null;
        const n = rows[0].length;
        const out = new Array(n).fill(0);
        for (const r of rows) for (let f = 0; f < n; f++) out[f] += r[f];
        for (let f = 0; f < n; f++) out[f] /= rows.length;
        return out;
    }

    const traces = [];

    if (effGroup === 'refresh') {
        // One series per block, coloured, labelled by refresh rate.
        const blocks = [...new Set(blockPerTrial)].sort((a, b) => a - b);
        const BLOCK_COLORS = [CON_COLOR, INC_COLOR, '#818cf8', '#fbbf24'];
        const BLOCK_FILLS = [CON_COLOR_DIM, INC_COLOR_DIM,
                             'rgba(129,140,248,0.15)', 'rgba(251,191,36,0.15)'];
        blocks.forEach((blk, bi) => {
            const color = BLOCK_COLORS[bi % BLOCK_COLORS.length];
            const hz = blockOrder[String(blk)];
            const name = hz ? `${hz} block` : `Block ${blk}`;
            if (mode === 'all') {
                // Every surviving trial in this block, dimmed.
                let sawLegend = false;
                for (let i = 0; i < perTrial.length; i++) {
                    if (exclusionFlags[i] || blockPerTrial[i] !== blk) continue;
                    traces.push({
                        x: freqs, y: perTrial[i],
                        type: 'scatter', mode: 'lines',
                        line: { color, width: 1 }, opacity: 0.18,
                        name, legendgroup: `blk${blk}`, showlegend: !sawLegend,
                        hovertemplate: `<b>${name}</b> · trial ${i + 1}<br>%{x:.1f} Hz → %{y:.2f}<extra></extra>`,
                    });
                    sawLegend = true;
                }
                const avg = avgRows(i => !exclusionFlags[i] && blockPerTrial[i] === blk);
                if (avg) traces.push({
                    x: freqs, y: avg, name: `${name} · avg`, legendgroup: `blk${blk}-avg`,
                    type: 'scatter', mode: 'lines', line: { color, width: 2.2 },
                });
            } else {
                const avg = avgRows(i => !exclusionFlags[i] && blockPerTrial[i] === blk);
                if (avg) traces.push({
                    x: freqs, y: avg, name,
                    type: 'scatter', mode: 'lines',
                    line: { color, width: 1.6 },
                    fill: 'tozeroy', fillcolor: BLOCK_FILLS[bi % BLOCK_FILLS.length],
                });
            }
        });
    } else if (mode === 'all' && perTrial && perTrial.length) {
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

// ── Included trials table ──
// Mirror of the excluded table for trials that survived on BOTH channels.
// The block column shows the refresh-rate label (e.g. "60 Hz") when known.
//
// Trial numbering (§Doc 5): the primary "Task #" is the canonical flanker task
// number (1-160) obtained by matching each EEG epoch to its behavioural row via
// RT alignment. When no behavioural (flanker) file is loaded — or a given trial
// could not be matched — task_number is null and we fall back to the EEG
// recording number, flagged so the analyst knows it is NOT the flanker number.
// The EEG recording number is always shown in its own column so the analyst can
// still locate the epoch in the raw recording.
function trialNumCells(t) {
    const eeg = t.trial;
    if (t.task_number != null) {
        return `<td class="task-num">${t.task_number}</td><td class="eeg-num">${eeg}</td>`;
    }
    // No flanker match: show EEG number as a fallback in the Task # column,
    // marked so it is clearly not a flanker task number.
    return `<td class="task-num task-num-fallback" title="No flanker match — showing EEG recording number, not a flanker task number">${eeg}<span class="fallback-mark">*</span></td><td class="eeg-num">${eeg}</td>`;
}

function populateIncludedTable(trials, blockOrder = {}) {
    const included = trials.filter(t => !t.fz_exclude && !t.c3_exclude);
    const section = document.getElementById('included-section');
    const countEl = document.getElementById('included-count');
    const tbody = document.querySelector('#included-table tbody');
    if (!section || !tbody) return;

    if (included.length === 0) {
        section.hidden = true;
        return;
    }
    section.hidden = false;
    countEl.textContent = `(${included.length})`;

    tbody.innerHTML = included.map(t => {
        const mm = Math.floor(t.onset / 60);
        const ss = (t.onset - mm * 60).toFixed(2).padStart(5, '0');
        const onsetLabel = `${mm}:${ss}`;
        const onsetRaw = t.onset.toFixed(4);
        const hz = blockOrder[String(t.block)];
        const blockLabel = hz ? `${t.block} · ${escapeHtml(hz)}` : `${t.block}`;
        return `
        <tr class="epoch-row" data-trial="${t.trial}" role="button" tabindex="0" title="Click to inspect this epoch">
            ${trialNumCells(t)}
            <td>${blockLabel}</td>
            <td title="Raw seconds from LabChart file: ${onsetRaw}"><span class="onset-mm">${onsetLabel}</span><span class="onset-raw">${onsetRaw}s</span></td>
            <td>${t.condition}</td>
            <td>${t.rt_ms} ms</td>
            <td>${t.fz_ptp.toFixed(1)} µV</td>
            <td>${t.theta_rel.toFixed(3)}</td>
            <td>${t.beta_rel.toFixed(3)}</td>
            <td>${t.maxz.toFixed(2)}</td>
            <td>${t.impact.toFixed(1)}%</td>
            <td>${t.coinc.toFixed(2)}</td>
        </tr>`;
    }).join('');
    wireEpochRows(tbody);
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
        <tr class="epoch-row" data-trial="${t.trial}" role="button" tabindex="0" title="Click to inspect this epoch">
            ${trialNumCells(t)}
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
    wireEpochRows(tbody);
}

// ── Epoch viewer (Issues & Changes 1) ──
// Clicking any trial row (included or excluded) opens a per-epoch review modal.
// The pipeline discards epoch waveforms, so the backend reconstructs the trace
// on demand from the retained continuous signal (identical high-pass + epoching
// to the analysis). The reviewer's core job: catch contaminated trials that were
// KEPT — the modal shows the trace, the trial value vs its adaptive threshold,
// power values, and robust deviation from the accepted-trial median.
function wireEpochRows(tbody) {
    tbody.querySelectorAll('tr.epoch-row').forEach(row => {
        const trial = parseInt(row.dataset.trial, 10);
        if (!Number.isFinite(trial)) return;
        row.addEventListener('click', () => openEpochViewer(trial));
        row.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openEpochViewer(trial); }
        });
    });
}

let _epochScalogramLoaded = false;

async function openEpochViewer(trial, opts = {}) {
    if (!currentResultId) return;
    const modal = document.getElementById('epoch-modal');
    const body = document.getElementById('epoch-body');
    const title = document.getElementById('epoch-title');
    if (!modal || !body) return;

    const wantScal = !!opts.scalogram;
    modal.hidden = false;
    if (!opts.scalogram) {
        _epochScalogramLoaded = false;
        title.textContent = `Epoch ${trial}`;
        body.innerHTML = `<p class="epoch-loading">Reconstructing epoch ${trial}…</p>`;
    }

    let data;
    try {
        const url = `${API}/api/subjects/${encodeURIComponent(currentResultId)}/epoch/${trial}` +
            (wantScal ? '?scalogram=true' : '');
        const resp = await fetch(url);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        data = await resp.json();
    } catch (e) {
        body.innerHTML = `<p class="epoch-error">Could not load epoch ${trial}: ${escapeHtml(e.message)}</p>`;
        return;
    }
    _epochCurrent = { trial, data };
    renderEpochViewer(data, wantScal);
}

let _epochCurrent = null;

function renderEpochViewer(data, hasScalogram) {
    const t = data.trial;
    const ep = data.epoch;
    const dv = data.deviation;
    const th = data.thresholds;
    const title = document.getElementById('epoch-title');
    const body = document.getElementById('epoch-body');

    const taskLabel = t.task_number != null
        ? `Task #${t.task_number}` : `EEG #${t.trial} (no flanker match)`;
    title.textContent = `Epoch — ${taskLabel} · EEG #${t.trial}`;

    // Channel status chips.
    const fzChip = t.fz_exclude
        ? '<span class="ep-chip ep-excl">Fz–Pz excluded</span>'
        : '<span class="ep-chip ep-keep">Fz–Pz kept</span>';
    const c3Chip = t.c3_exclude
        ? '<span class="ep-chip ep-excl">C3–C4 excluded</span>'
        : '<span class="ep-chip ep-keep">C3–C4 kept</span>';
    const softChip = dv.soft_flag
        ? `<span class="ep-chip ep-soft" title="${escapeHtml(dv.soft_reasons.join('; '))}">⚠ borderline (kept)</span>`
        : '';

    // Identity block (item 1).
    const identity = `
        <div class="ep-identity">
            <div class="ep-id-row">
                <span class="ep-id-k">Task #</span><span class="ep-id-v">${t.task_number != null ? t.task_number : '—'}</span>
                <span class="ep-id-k">EEG #</span><span class="ep-id-v">${t.trial}</span>
                <span class="ep-id-k">Block</span><span class="ep-id-v">${t.block}</span>
                <span class="ep-id-k">Condition</span><span class="ep-id-v">${escapeHtml(t.condition)}</span>
                <span class="ep-id-k">Onset</span><span class="ep-id-v">${t.onset.toFixed(3)} s</span>
                <span class="ep-id-k">RT</span><span class="ep-id-v">${t.rt_ms} ms</span>
            </div>
            <div class="ep-chips">${fzChip}${c3Chip}${softChip}</div>
            ${t.reason ? `<div class="ep-reason">Exclusion reason: <strong>${escapeHtml(t.reason)}</strong></div>` : ''}
            ${dv.soft_flag ? `<div class="ep-soft-note">⚠ This trial was <strong>kept</strong> but sits far from the accepted-trial distribution: ${escapeHtml(dv.soft_reasons.join('; '))}. Worth a manual look.</div>` : ''}
        </div>`;

    // Deviation table (item 5) + power values (item 4) share the metric rows.
    const metricRows = Object.entries(dv.metrics).map(([key, m]) => {
        const rz = m.robust_z == null ? '—' : `${m.robust_z > 0 ? '+' : ''}${m.robust_z.toFixed(2)}`;
        const rzClass = (m.robust_z != null && Math.abs(m.robust_z) >= dv.soft_flag_z && m.channel_kept)
            ? 'ep-dev-hot' : '';
        const medTxt = m.median == null ? '—' : fmtNum(m.median, 3);
        return `<tr class="${rzClass}">
            <td>${escapeHtml(m.label)}</td>
            <td class="ep-num">${fmtNum(m.value, 3)}</td>
            <td class="ep-num">${medTxt}</td>
            <td class="ep-num">${rz}</td>
            <td>${m.channel_kept ? '<span class="ep-mini-keep">accepted</span>' : '<span class="ep-mini-excl">excluded</span>'}</td>
        </tr>`;
    }).join('');

    const thresholdsBlk = `
        <div class="ep-thresholds">
            <h4>Adaptive thresholds (this recording)</h4>
            <ul>
                <li>Blink (Fz slow-band p-t-p): <strong>${th.blink_uv} µV</strong>
                    — this trial: <strong>${t.fz_ptp.toFixed(1)} µV</strong> full-band p-t-p</li>
                <li>EMG (C3 beta power): <strong>${th.emg} µV²</strong></li>
                <li>Coincidence z (both channels): <strong>${th.coinc_z}</strong>
                    — this trial coinc: <strong>${t.coinc.toFixed(2)}</strong></li>
                <li>Burst z / impact: <strong>${th.burst_z}</strong> / <strong>${th.burst_impact}%</strong>
                    — this trial: maxz <strong>${t.maxz.toFixed(2)}</strong>, impact <strong>${t.impact.toFixed(1)}%</strong></li>
            </ul>
        </div>`;

    const devTable = `
        <div class="ep-panel">
            <h4>Power &amp; metrics vs accepted-trial distribution</h4>
            <p class="ep-note">Robust z = (value − median)/(1.4826·MAD) over trials whose
            channel was accepted. Denominator for relative power is 1–35 Hz. A channel
            marked "excluded" is not part of that channel's reference set.</p>
            <table class="ep-dev-table">
                <thead><tr><th>Measure</th><th>This trial</th><th>Accepted median</th><th>Robust z</th><th>Channel</th></tr></thead>
                <tbody>${metricRows}</tbody>
            </table>
        </div>`;

    const scalBtn = hasScalogram
        ? ''
        : `<button id="ep-scal-btn" class="btn-secondary ep-scal-btn">Show scalogram (time × frequency)</button>`;

    body.innerHTML = `
        ${identity}
        <div class="ep-panel">
            <h4>Signal trace — 0 to ${Math.round(ep.win_ms + ep.reach_ms)} ms (analysis + wavelet buffer)</h4>
            <p class="ep-note">The shaded buffer band (${ep.win_ms}–${Math.round(ep.win_ms + ep.reach_ms)} ms)
            feeds the wavelet edges only. Onset = 0 ms; keypress marked where available.
            The dashed slow-band (1–${th.blink_slow_hz} Hz) Fz trace is the blink detector's input.</p>
            <div id="ep-trace" class="ep-plot"></div>
        </div>
        ${thresholdsBlk}
        ${devTable}
        <div id="ep-scal-wrap" class="ep-panel" ${hasScalogram ? '' : 'hidden'}>
            <h4>Scalogram — |CWT|² (Fz–Pz theta cycles / C3–C4 beta cycles)</h4>
            <div id="ep-scal-fz" class="ep-plot"></div>
            <div id="ep-scal-c3" class="ep-plot"></div>
        </div>
        ${scalBtn}`;

    drawEpochTrace(data);
    if (hasScalogram && ep.scalogram) drawEpochScalogram(ep.scalogram, ep.times_ms);

    const sb = document.getElementById('ep-scal-btn');
    if (sb) sb.addEventListener('click', () => openEpochViewer(_epochCurrent.trial, { scalogram: true }));
}

function drawEpochTrace(data) {
    const ep = data.epoch;
    const t = data.trial;
    const winEnd = ep.win_ms + ep.reach_ms;
    // Only show 0..winEnd (+ a little pre-onset context) per the spec's "0–675 ms".
    const traces = [
        { x: ep.times_ms, y: ep.fz, name: 'Fz–Pz', type: 'scatter', mode: 'lines',
          line: { color: '#2563eb', width: 1.4 } },
        { x: ep.times_ms, y: ep.c3, name: 'C3–C4', type: 'scatter', mode: 'lines',
          line: { color: '#16a34a', width: 1.4 } },
        { x: ep.times_ms, y: ep.fz_slow, name: 'Fz slow (blink input)', type: 'scatter',
          mode: 'lines', line: { color: '#2563eb', width: 1, dash: 'dot' }, opacity: 0.6 },
    ];
    const shapes = [{
        type: 'rect', xref: 'x', yref: 'paper',
        x0: ep.win_ms, x1: winEnd, y0: 0, y1: 1,
        fillcolor: 'rgba(234,179,8,0.10)', line: { width: 0 }, layer: 'below',
    }];
    const annotations = [];
    // Onset marker at 0 ms.
    shapes.push({ type: 'line', xref: 'x', yref: 'paper', x0: 0, x1: 0, y0: 0, y1: 1,
        line: { color: '#111', width: 1.2, dash: 'solid' } });
    annotations.push({ x: 0, y: 1.02, xref: 'x', yref: 'paper', text: 'onset',
        showarrow: false, font: { size: 10, color: '#111' } });
    if (data.keypress_ms != null) {
        shapes.push({ type: 'line', xref: 'x', yref: 'paper', x0: data.keypress_ms,
            x1: data.keypress_ms, y0: 0, y1: 1, line: { color: '#dc2626', width: 1.2, dash: 'dash' } });
        annotations.push({ x: data.keypress_ms, y: 1.02, xref: 'x', yref: 'paper',
            text: 'keypress', showarrow: false, font: { size: 10, color: '#dc2626' } });
    }
    const layout = {
        margin: { l: 50, r: 12, t: 20, b: 40 }, height: 300,
        xaxis: { title: 'Time from onset (ms)', range: [-60, winEnd + 20], zeroline: false },
        yaxis: { title: 'µV' },
        shapes, annotations,
        legend: { orientation: 'h', y: -0.25 },
        paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    };
    Plotly.react('ep-trace', traces, layout, { displayModeBar: false, responsive: true });
}

function drawEpochScalogram(scal, timesMs) {
    const common = {
        margin: { l: 50, r: 12, t: 24, b: 36 }, height: 240,
        xaxis: { title: 'Time from onset (ms)' }, yaxis: { title: 'Hz' },
    };
    Plotly.react('ep-scal-fz',
        [{ z: scal.fz_power, x: timesMs, y: scal.freqs, type: 'heatmap', colorscale: 'Viridis',
           colorbar: { title: 'µV²', thickness: 10 } }],
        { ...common, title: { text: 'Fz–Pz', font: { size: 12 } } },
        { displayModeBar: false, responsive: true });
    Plotly.react('ep-scal-c3',
        [{ z: scal.c3_power, x: timesMs, y: scal.freqs, type: 'heatmap', colorscale: 'Viridis',
           colorbar: { title: 'µV²', thickness: 10 } }],
        { ...common, title: { text: 'C3–C4', font: { size: 12 } } },
        { displayModeBar: false, responsive: true });
}

function closeEpochViewer() {
    const modal = document.getElementById('epoch-modal');
    if (modal) modal.hidden = true;
    _epochCurrent = null;
}

// ── Raw EEG Data viewer (whole-recording overview) ──
// Shows the ENTIRE continuous recording (decimated) in one scrollable trace.
// The signal the pipeline actually analysed — the epoch window around every
// paired trial — is drawn in colour over a greyed-out background trace of
// everything it ignored. Block start/end spans, per-trial stimulus onset and
// keypress (by condition), and each epoch's accept/reject verdict are overlaid.
let _rawEegLoaded = null;   // result_id currently rendered

async function openRawEeg() {
    if (!currentResultId) return;
    const modal = document.getElementById('raweeg-modal');
    const body = document.getElementById('raweeg-body');
    const title = document.getElementById('raweeg-title');
    if (!modal || !body) return;

    modal.hidden = false;
    title.textContent = `Raw EEG Data — ${currentResultId}`;
    body.innerHTML = `<p class="epoch-loading">Reconstructing full recording…</p>`;

    let data;
    try {
        const url = `${API}/api/subjects/${encodeURIComponent(currentResultId)}/recording`;
        const resp = await fetch(url);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        data = await resp.json();
    } catch (e) {
        body.innerHTML = `<p class="epoch-error">Could not load recording: ${escapeHtml(e.message)}</p>`;
        return;
    }
    _rawEegLoaded = currentResultId;
    renderRawEeg(data);
}

function renderRawEeg(data) {
    const body = document.getElementById('raweeg-body');
    const ov = data.overview;
    const trials = data.trials || [];
    const blocks = data.blocks || [];
    const nIncl = trials.filter(t => t.included).length;
    const nExcl = trials.length - nIncl;

    body.innerHTML = `
        <div class="raweeg-legend">
            <span class="rw-key"><span class="rw-swatch rw-grey"></span> Not analysed (ignored by pipeline)</span>
            <span class="rw-key"><span class="rw-swatch rw-fz"></span> Fz–Pz (analysed)</span>
            <span class="rw-key"><span class="rw-swatch rw-c3"></span> C3–C4 (analysed)</span>
            <span class="rw-key"><span class="rw-swatch rw-incl"></span> Epoch window — accepted</span>
            <span class="rw-key"><span class="rw-swatch rw-excl"></span> Epoch window — rejected</span>
            <span class="rw-key"><span class="rw-line rw-con"></span> Congruent onset · keypress</span>
            <span class="rw-key"><span class="rw-line rw-inc"></span> Incongruent onset · keypress</span>
        </div>
        <p class="raweeg-meta">
            ${ov.n_samples.toLocaleString()} samples @ ${ov.fs} Hz
            (${(ov.duration_s / 60).toFixed(1)} min) · shown decimated ×${ov.decimation} ·
            ${trials.length} trials (${nIncl} accepted, ${nExcl} rejected) ·
            epoch window ${data.window.win_s * 1000} ms + ${data.window.pad_s * 1000} ms buffer
        </p>
        <div id="raweeg-plot" class="raweeg-plot"></div>
        <p class="raweeg-hint">Drag to zoom · use the range slider below to scroll the whole recording · double-click to reset.</p>`;

    drawRawEeg(data);
}

function drawRawEeg(data) {
    const ov = data.overview;
    const trials = data.trials || [];
    const blocks = data.blocks || [];
    const times = ov.times_s;
    const fz = ov.fz;
    const c3 = ov.c3;

    // Background: the whole recording greyed out (everything, analysed or not).
    const traces = [
        { x: times, y: fz, name: 'Fz–Pz (all)', type: 'scattergl', mode: 'lines',
          line: { color: 'rgba(148,163,184,0.55)', width: 0.7 }, hoverinfo: 'skip', showlegend: false },
        { x: times, y: c3, name: 'C3–C4 (all)', type: 'scattergl', mode: 'lines',
          line: { color: 'rgba(148,163,184,0.35)', width: 0.7 }, hoverinfo: 'skip', showlegend: false },
    ];

    // Foreground: coloured trace segments over each analysed epoch window. Using
    // NaN gaps to break the line between epochs keeps it a single cheap trace.
    const fzSeg = new Array(times.length).fill(NaN);
    const c3Seg = new Array(times.length).fill(NaN);
    // Precompute window bounds; walk the decimated axis once per trial region.
    for (const t of trials) {
        // find decimated indices within [win_start_s, win_end_s]
        let i = lowerBound(times, t.win_start_s);
        for (; i < times.length && times[i] <= t.win_end_s; i++) {
            fzSeg[i] = fz[i];
            c3Seg[i] = c3[i];
        }
    }
    traces.push(
        { x: times, y: fzSeg, name: 'Fz–Pz (analysed)', type: 'scattergl', mode: 'lines',
          line: { color: '#2563eb', width: 1.1 }, connectgaps: false,
          hovertemplate: '%{x:.2f}s<br>%{y:.1f} µV<extra>Fz–Pz</extra>' },
        { x: times, y: c3Seg, name: 'C3–C4 (analysed)', type: 'scattergl', mode: 'lines',
          line: { color: '#16a34a', width: 1.1 }, connectgaps: false,
          hovertemplate: '%{x:.2f}s<br>%{y:.1f} µV<extra>C3–C4</extra>' },
    );

    // Shapes: block spans, buffer + window rects (accept/reject coloured),
    // onset & keypress lines (coloured by condition).
    const shapes = [];
    const annotations = [];

    for (const b of blocks) {
        shapes.push({ type: 'rect', xref: 'x', yref: 'paper',
            x0: b.start_s, x1: b.end_s, y0: 0, y1: 1,
            fillcolor: 'rgba(37,99,235,0.04)', line: { width: 0 }, layer: 'below' });
        // start & end markers
        shapes.push({ type: 'line', xref: 'x', yref: 'paper', x0: b.start_s, x1: b.start_s,
            y0: 0, y1: 1, line: { color: '#1e3a8a', width: 1.4 } });
        shapes.push({ type: 'line', xref: 'x', yref: 'paper', x0: b.end_s, x1: b.end_s,
            y0: 0, y1: 1, line: { color: '#1e3a8a', width: 1.4, dash: 'dot' } });
        annotations.push({ x: b.start_s, y: 1.06, xref: 'x', yref: 'paper',
            text: `Block ${b.block} start`, showarrow: false, font: { size: 10, color: '#1e3a8a' } });
        annotations.push({ x: b.end_s, y: 1.06, xref: 'x', yref: 'paper',
            text: `Block ${b.block} end`, showarrow: false, font: { size: 10, color: '#1e3a8a' } });
    }

    for (const t of trials) {
        // buffer band (light) then window band (coloured by verdict)
        shapes.push({ type: 'rect', xref: 'x', yref: 'paper',
            x0: t.buf_start_s, x1: t.buf_end_s, y0: 0.04, y1: 0.96,
            fillcolor: 'rgba(148,163,184,0.10)', line: { width: 0 }, layer: 'below' });
        shapes.push({ type: 'rect', xref: 'x', yref: 'paper',
            x0: t.win_start_s, x1: t.win_end_s, y0: 0.04, y1: 0.96,
            fillcolor: t.included ? 'rgba(22,163,74,0.16)' : 'rgba(220,38,38,0.16)',
            line: { color: t.included ? 'rgba(22,163,74,0.5)' : 'rgba(220,38,38,0.5)', width: 0.6 },
            layer: 'below' });
        // onset (solid) + keypress (dashed), coloured by condition
        const col = t.cond === 'con' ? '#059669' : '#dc2626';
        shapes.push({ type: 'line', xref: 'x', yref: 'paper', x0: t.onset_s, x1: t.onset_s,
            y0: 0.04, y1: 0.96, line: { color: col, width: 0.8 } });
        if (t.keypress_s != null) {
            shapes.push({ type: 'line', xref: 'x', yref: 'paper', x0: t.keypress_s, x1: t.keypress_s,
                y0: 0.04, y1: 0.96, line: { color: col, width: 0.8, dash: 'dot' } });
        }
    }

    // Clickable trial markers (at onset, near top) so hovering/clicking a trial
    // opens its epoch viewer. One marker trace carries all trials.
    const mx = trials.map(t => t.onset_s);
    const my = trials.map(() => null);   // placed via yref paper below
    const mtext = trials.map(t => {
        const lbl = t.task_number != null ? `Task #${t.task_number}` : `EEG #${t.trial}`;
        return `${lbl} · ${t.condition} · block ${t.block}<br>${t.included ? 'accepted' : 'rejected: ' + (t.reason || '—')}`;
    });
    traces.push({
        x: mx, y: trials.map(() => 0),
        yaxis: 'y2',
        type: 'scattergl', mode: 'markers', name: 'Trials',
        marker: {
            size: 8,
            symbol: trials.map(t => t.included ? 'circle' : 'x'),
            color: trials.map(t => t.included ? '#16a34a' : '#dc2626'),
            line: { width: 0.5, color: '#fff' },
        },
        text: mtext, hovertemplate: '%{text}<extra></extra>',
        customdata: trials.map(t => t.trial),
        showlegend: false,
    });

    const layout = {
        margin: { l: 50, r: 12, t: 28, b: 60 }, height: 460,
        xaxis: { title: 'Time from recording start (s)', rangeslider: { visible: true }, zeroline: false },
        yaxis: { title: 'µV', domain: [0, 0.92] },
        yaxis2: { domain: [0.94, 1], range: [-1, 1], visible: false, fixedrange: true },
        shapes, annotations,
        legend: { orientation: 'h', y: -0.35 },
        hovermode: 'closest', dragmode: 'zoom',
        paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
    };
    Plotly.newPlot('raweeg-plot', traces, layout,
        { displayModeBar: true, responsive: true, displaylogo: false,
          modeBarButtonsToRemove: ['lasso2d', 'select2d'] });

    const plotEl = document.getElementById('raweeg-plot');
    if (plotEl) {
        plotEl.on('plotly_click', (ev) => {
            const p = ev.points && ev.points[0];
            if (!p || p.customdata == null) return;
            closeRawEeg();
            openEpochViewer(p.customdata);
        });
    }
}

// Smallest index i such that arr[i] >= target (arr ascending).
function lowerBound(arr, target) {
    let lo = 0, hi = arr.length;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (arr[mid] < target) lo = mid + 1; else hi = mid;
    }
    return lo;
}

function closeRawEeg() {
    const modal = document.getElementById('raweeg-modal');
    if (modal) modal.hidden = true;
}

// ── Missing trials table ──
// Flanker (behavioural) trials that have no matching EEG epoch. Requires an
// uploaded behavioural file; the box is hidden otherwise. Task numbers are the
// canonical flanker numbers (1-160); reason is always "eeg data not found".
function populateMissingTable(missing) {
    const section = document.getElementById('missing-section');
    const countEl = document.getElementById('missing-count');
    const tbody = document.querySelector('#missing-table tbody');
    if (!section || !tbody) return;

    if (!missing || missing.length === 0) {
        section.hidden = true;
        tbody.innerHTML = '';
        return;
    }
    section.hidden = false;
    countEl.textContent = `(${missing.length})`;

    tbody.innerHTML = missing.map(m => {
        const task = m.trial != null ? m.trial : '—';
        const blockNo = m.btrial != null ? m.btrial : '—';
        const rt = (m.rt_ms != null) ? `${m.rt_ms} ms` : '—';
        const correct = (m.correct === true) ? '✓' : (m.correct === false ? '✕' : '—');
        return `
        <tr>
            <td class="task-num">${task}</td>
            <td>${blockNo}</td>
            <td>${m.block}</td>
            <td>${escapeHtml(m.condition || '')}</td>
            <td>${rt}</td>
            <td class="${m.correct === false ? 'flag-yes' : 'flag-no'}">${correct}</td>
            <td>${missingReasonBadge(m)}</td>
        </tr>`;
    }).join('');
}

// The Missing box separates three non-equivalent causes (Doc 5 item 3):
//   dropped_epoch  — EEG existed but was dropped (NaN/artifact); belongs with Excluded.
//   alignment_miss — EEG present but RT alignment failed; recoverable upstream.
//   not_recorded   — genuine data loss.
const _MISSING_REASON_META = {
    dropped_epoch:  { cls: 'mr-dropped',  label: 'dropped epoch' },
    alignment_miss: { cls: 'mr-align',    label: 'alignment miss' },
    not_recorded:   { cls: 'mr-absent',   label: 'never recorded' },
};

function missingReasonBadge(m) {
    const meta = _MISSING_REASON_META[m.reason_code];
    const text = escapeHtml(m.reason || 'eeg data not found');
    if (!meta) return text;
    return `<span class="mr-badge ${meta.cls}" title="${text}">${meta.label}</span> `
        + `<span class="mr-detail">${text}</span>`;
}

// ── Subject list ──
function updateSubjectList() {
    const listEl = document.getElementById('subject-list');
    const itemsEl = document.getElementById('subject-items');
    if (uploadedSubjects.length > 1) {
        listEl.hidden = false;
        itemsEl.innerHTML = uploadedSubjects.map((s, i) => {
            const active = s.result_id === currentResultId ? ' active' : '';
            return `
            <span class="subject-chip${active}" data-id="${s.result_id}" role="button" tabindex="0"
                  title="View ${escapeHtml(s.filename)}">
                <span class="chip-dot" style="background:${SUBJECT_COLORS[i % SUBJECT_COLORS.length]}"></span>
                ${escapeHtml(s.filename)}
                <button class="chip-remove" data-id="${s.result_id}" title="Remove">×</button>
            </span>`;
        }).join('');

        // Click a chip → load that subject's individual results.
        itemsEl.querySelectorAll('.subject-chip').forEach(chip => {
            const load = () => {
                const id = chip.dataset.id;
                if (id && id !== currentResultId) loadSubject(id);
            };
            chip.addEventListener('click', (e) => {
                if (e.target.closest('.chip-remove')) return;
                load();
            });
            chip.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); load(); }
            });
        });

        itemsEl.querySelectorAll('.chip-remove').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const id = e.target.dataset.id;
                await fetch(`${API}/api/subjects/${id}`, { method: 'DELETE' });
                uploadedSubjects = uploadedSubjects.filter(s => s.result_id !== id);
                // If we removed the subject currently on screen, fall back to
                // the first remaining one (or the upload view if none left).
                if (id === currentResultId) {
                    if (uploadedSubjects.length) {
                        loadSubject(uploadedSubjects[0].result_id);
                    } else {
                        currentResultId = null;
                        currentData = null;
                        goToUpload();
                        return;
                    }
                }
                updateSubjectList();
                updateCompareButton();
            });
        });
    } else {
        listEl.hidden = true;
    }
}

// Fetch one already-uploaded subject's full payload and show it individually.
async function loadSubject(resultId) {
    try {
        const resp = await fetch(`${API}/api/subjects/${encodeURIComponent(resultId)}/results`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            alert(err.detail || 'Could not load subject.');
            return;
        }
        const data = await resp.json();
        document.getElementById('compare-section').hidden = true;
        document.getElementById('upload-section').hidden = true;
        showResults(data);
    } catch (err) {
        alert('Error loading subject: ' + err.message);
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
        document.getElementById('upload-section').hidden = true;
        document.getElementById('refresh-section').hidden = true;
        saveSession({ view: 'compare' });

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
            return `<tr class="compare-row" data-id="${escapeHtml(subj.result_id)}" role="button" tabindex="0" title="View ${escapeHtml(s.filename)}">
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

        // Click a table row → drill into that subject's individual results.
        tbody.querySelectorAll('.compare-row').forEach(row => {
            const load = () => { if (row.dataset.id) loadSubject(row.dataset.id); };
            row.addEventListener('click', load);
            row.addEventListener('keydown', (e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); load(); }
            });
        });

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
    document.getElementById('refresh-section').hidden = true;
    document.getElementById('upload-section').hidden = false;
    document.getElementById('upload-progress').hidden = true;
    document.getElementById('upload-summary').hidden = true;
    document.getElementById('file-input').value = '';
    // Reflect any files still staged (e.g. after a partial-failure run).
    if (initUpload._renderStaging) initUpload._renderStaging();
}

// ── Refresh-rate comparison (60 Hz vs 165 Hz) ──
// Holds the last-fetched payload so the provenance modal can look up the exact
// numbers behind a clicked chart.
let refreshData = null;
// Per-participant display aggregation for the refresh view: 'median' (primary),
// 'mean', or 'both' (side by side). Inspection only — it never changes the group
// headline (always mean Δ of per-participant medians) nor the CSV export (always
// carries both aggregations).
let refreshAgg = 'median';

async function showRefreshComparison() {
    let data;
    try {
        const resp = await fetch(`${API}/api/refresh-comparison`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            alert(err.detail || 'Refresh-rate comparison failed');
            return;
        }
        data = await resp.json();
    } catch (err) {
        alert('Error loading refresh-rate comparison: ' + err.message);
        return;
    }
    refreshData = data;

    document.getElementById('results-section').hidden = true;
    document.getElementById('compare-section').hidden = true;
    document.getElementById('upload-section').hidden = true;
    document.getElementById('refresh-section').hidden = false;
    saveSession({ view: 'refresh' });

    // List subjects excluded from the group stats, with WHY — distinguishing a
    // benign data gap (safely excluded) from a fixable upstream block/merge fault.
    renderRefreshExclusions(data);

    // Side-by-side sample-level averages for all three measures, up top.
    renderRefreshOverview(data.measures);

    renderRefreshMeasures(data.measures);
}

// Top-of-page overview: the average (mean Δ) difference across the sample for
// each measure, shown side by side so trends in spectral power and reaction
// time are visible at a glance. Uses the same group stat as each measure's
// headline (mean Δ of the per-participant median differences) — never affected
// by the inspection toggle.
function renderRefreshOverview(measures) {
    const el = document.getElementById('refresh-overview');
    if (!el) return;
    if (!measures || !measures.length) {
        el.hidden = true;
        el.innerHTML = '';
        return;
    }

    const cards = measures.map(m => {
        const g = m.group;
        const dp = m.decimals;
        const diffLabel = m.diff_label || '165 Hz − 60 Hz';
        if (!g || g.n === 0) {
            return `
            <div class="ro-card ro-empty">
                <div class="ro-measure">${escapeHtml(m.label)}</div>
                <div class="ro-nodata">No participant has both conditions yet.</div>
            </div>`;
        }
        const d = g.mean_diff;
        const dir = d > 0 ? 'ro-up' : (d < 0 ? 'ro-down' : 'ro-flat');
        const arrow = d > 0 ? '▲' : (d < 0 ? '▼' : '■');
        const ci = (g.ci95_lo == null)
            ? '—'
            : `${fmtNum(g.ci95_lo, dp)} … ${fmtNum(g.ci95_hi, dp)}`;
        return `
        <div class="ro-card">
            <div class="ro-measure">${escapeHtml(m.label)}</div>
            <div class="ro-sub">${escapeHtml(m.unit)}</div>
            <div class="ro-mean ${dir}"><span class="ro-arrow">${arrow}</span>${d > 0 ? '+' : ''}${fmtNum(d, dp)}</div>
            <div class="ro-mean-lab">mean Δ (${escapeHtml(diffLabel)})</div>
            <div class="ro-meta">
                <span title="Median of the per-participant differences">median Δ ${fmtNum(g.median_diff, dp)}</span>
                <span title="95% confidence interval of the mean difference">95% CI ${ci}</span>
                <span>N=${g.n}</span>
            </div>
        </div>`;
    }).join('');

    el.hidden = false;
    el.innerHTML = `
        <div class="ro-title">Sample averages · trend across all participants</div>
        <div class="ro-grid">${cards}</div>`;
}

// Collect the excluded subjects across measures and explain each. A subject can
// be excluded for different reasons per measure (e.g. beta channel excluded but
// theta fine), so we key by subject and list the measures affected.
function renderRefreshExclusions(data) {
    const warnEl = document.getElementById('refresh-warning');
    if (!warnEl) return;

    // subject -> { filename, reasons: Map<note, {kind, fixable, measures:[]}> }
    const bySubject = new Map();
    for (const m of data.measures) {
        for (const p of m.participants) {
            if (p.has_both) continue;
            if (!bySubject.has(p.result_id)) {
                bySubject.set(p.result_id, { filename: p.filename, items: new Map() });
            }
            const entry = bySubject.get(p.result_id);
            const note = p.note || 'excluded';
            if (!entry.items.has(note)) {
                entry.items.set(note, { kind: p.exclusion_kind, fixable: !!p.fixable, measures: [] });
            }
            entry.items.get(note).measures.push(m.label);
        }
    }

    if (bySubject.size === 0) {
        warnEl.hidden = true;
        warnEl.innerHTML = '';
        return;
    }

    const anyFixable = [...bySubject.values()].some(s =>
        [...s.items.values()].some(i => i.fixable));

    const cards = [...bySubject.values()].map(s => {
        const items = [...s.items.entries()].map(([note, info]) => {
            const badge = info.fixable
                ? '<span class="rx-badge rx-fixable">fixable upstream</span>'
                : '<span class="rx-badge rx-benign">safely excluded</span>';
            const meas = info.measures.length < 3
                ? ` <span class="rx-measures">(${info.measures.map(escapeHtml).join(', ')})</span>`
                : '';
            return `<li>${badge} ${escapeHtml(note)}${meas}</li>`;
        }).join('');
        return `<div class="rx-subject">
            <div class="rx-name">${escapeHtml(s.filename)}</div>
            <ul class="rx-reasons">${items}</ul>
        </div>`;
    }).join('');

    warnEl.hidden = false;
    warnEl.classList.toggle('refresh-warning-alert', anyFixable);
    warnEl.innerHTML = `
        <div class="rx-head">${bySubject.size} subject(s) excluded from the group comparison${anyFixable ? ' — one or more may be a fixable upstream problem' : ''}:</div>
        ${cards}`;
}

function renderRefreshMeasures(measures) {
    const wrap = document.getElementById('refresh-measures');
    // Column layout depends on the inspection toggle. 'both' shows median AND
    // mean side by side for each condition + both diffs; otherwise one value each.
    const agg = refreshAgg;
    const valCols = (agg === 'both')
        ? ['median', 'mean']
        : [agg];

    // Build the value cell(s) for one condition of one participant.
    function condCells(cond, dp) {
        if (!cond) return valCols.map(() => '<td>—</td>').join('');
        return valCols.map(a =>
            `<td>${fmtNum(cond[a], dp)} <span class="rr-n">n=${cond.n}</span></td>`).join('');
    }
    function condCellsIncomplete(cond, dp) {
        if (!cond) return valCols.map(() => '<td>—</td>').join('');
        return valCols.map(a => `<td>${fmtNum(cond[a], dp)}</td>`).join('');
    }
    // The Δ cell(s): in 'both' mode show median Δ and mean Δ; else the chosen one.
    function diffCells(p, dp) {
        const keys = (agg === 'both') ? ['diff_median', 'diff_mean'] : [agg === 'mean' ? 'diff_mean' : 'diff_median'];
        return keys.map(k => {
            const d = p[k];
            const cls = d > 0 ? 'rr-up' : (d < 0 ? 'rr-down' : '');
            return `<td class="${cls}">${d > 0 ? '+' : ''}${fmtNum(d, dp)}</td>`;
        }).join('');
    }

    wrap.innerHTML = measures.map((m, i) => {
        const g = m.group;
        const gm = m.group_from_means;
        const dp = m.decimals;
        const diffLabel = m.diff_label || '165 Hz − 60 Hz';

        // Headline group summary — ALWAYS mean Δ of per-participant medians,
        // regardless of the inspection toggle.
        const groupHtml = g.n === 0
            ? `<div class="refresh-group-empty">No participant has both refresh conditions yet.</div>`
            : `
            <div class="refresh-group">
                <div class="rg-stat"><span class="rg-num">${fmtNum(g.mean_diff, dp)}</span><span class="rg-lab">mean Δ</span></div>
                <div class="rg-stat"><span class="rg-num">${fmtNum(g.median_diff, dp)}</span><span class="rg-lab">median Δ</span></div>
                <div class="rg-stat"><span class="rg-num">${g.sd_diff == null ? '—' : fmtNum(g.sd_diff, dp)}</span><span class="rg-lab">SD</span></div>
                <div class="rg-stat"><span class="rg-num">${g.ci95_lo == null ? '—' : `${fmtNum(g.ci95_lo, dp)}…${fmtNum(g.ci95_hi, dp)}`}</span><span class="rg-lab">95% CI</span></div>
                <div class="rg-stat"><span class="rg-num">${g.n}</span><span class="rg-lab">participants</span></div>
            </div>
            <div class="refresh-group-robust">Robustness (mean-aggregated): mean Δ ${fmtNum(gm.mean_diff, dp)} · median Δ ${fmtNum(gm.median_diff, dp)} · SD ${gm.sd_diff == null ? '—' : fmtNum(gm.sd_diff, dp)}</div>`;

        // Per-participant table.
        const rows = m.participants.map(p => {
            const lo = p.conditions[m.rate_low];
            const hi = p.conditions[m.rate_high];
            const span = valCols.length * 2 + (agg === 'both' ? 2 : 1) + 1;
            if (!p.has_both) {
                const note = p.note || 'only one refresh condition present';
                const badge = p.fixable
                    ? '<span class="rx-badge rx-fixable">fixable</span> '
                    : (p.exclusion_kind ? '<span class="rx-badge rx-benign">excluded</span> ' : '');
                return `<tr class="rr-incomplete">
                    <td>${escapeHtml(p.filename)}</td>
                    ${condCellsIncomplete(lo, dp)}
                    ${condCellsIncomplete(hi, dp)}
                    ${(agg === 'both' ? '<td>—</td><td>—</td>' : '<td>—</td>')}
                    <td class="rr-note">${badge}${escapeHtml(note)}</td>
                </tr>`;
            }
            return `<tr>
                <td>${escapeHtml(p.filename)}</td>
                ${condCells(lo, dp)}
                ${condCells(hi, dp)}
                ${diffCells(p, dp)}
                <td></td>
            </tr>`;
        }).join('');

        // Header cells depend on the toggle.
        const aggTag = (a) => `<span class="th-agg">${a}</span>`;
        const condHead = (label) => (agg === 'both')
            ? `<th>${escapeHtml(label)} ${aggTag('median')}</th><th>${escapeHtml(label)} ${aggTag('mean')}</th>`
            : `<th>${escapeHtml(label)} ${aggTag(agg)}</th>`;
        const diffHead = (agg === 'both')
            ? `<th>Δ ${aggTag('median')}</th><th>Δ ${aggTag('mean')}</th>`
            : `<th>Δ (${escapeHtml(diffLabel)}) ${aggTag(agg)}</th>`;

        return `
        <div class="refresh-measure" data-measure="${m.key}">
            <div class="refresh-measure-head">
                <div>
                    <h3>${escapeHtml(m.label)}</h3>
                    <span class="refresh-measure-sub">${escapeHtml(m.channel)}${m.band ? ' · ' + escapeHtml(m.band) : ''} · unit: ${escapeHtml(m.unit)}</span>
                </div>
                <button class="btn-ghost btn-sm refresh-prov-btn" data-measure="${m.key}">How was this computed?</button>
            </div>
            ${groupHtml}
            <div class="refresh-chart" id="refresh-chart-${m.key}"
                 role="button" tabindex="0" title="Click to see the computation trace"></div>
            <div class="compare-table-wrap">
                <table class="compare-table refresh-table">
                    <thead><tr>
                        <th>Participant</th>
                        ${condHead(m.rate_low || 'lower')}
                        ${condHead(m.rate_high || 'higher')}
                        ${diffHead}
                        <th></th>
                    </tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </div>`;
    }).join('');

    // Draw charts + wire click-to-provenance.
    measures.forEach(m => {
        drawRefreshChart(`refresh-chart-${m.key}`, m);
        const chartEl = document.getElementById(`refresh-chart-${m.key}`);
        const open = () => openProvenance(m.key);
        chartEl.addEventListener('click', open);
        chartEl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });
    });
    wrap.querySelectorAll('.refresh-prov-btn').forEach(btn => {
        btn.addEventListener('click', () => openProvenance(btn.dataset.measure));
    });
}

// Grouped bar: per participant, the two refresh-condition values side by side.
// Uses the selected inspection aggregation (median primary / mean); in 'both'
// mode it plots the median (the primary) to keep the chart readable.
function drawRefreshChart(containerId, m) {
    const complete = m.participants.filter(p => p.has_both);
    if (!complete.length) {
        document.getElementById(containerId).innerHTML =
            '<div class="refresh-chart-empty">Need at least one participant with both 60 Hz and 165 Hz conditions to plot.</div>';
        return;
    }
    const a = (refreshAgg === 'mean') ? 'mean' : 'median';
    const names = complete.map(p => p.filename);
    const lo = complete.map(p => p.conditions[m.rate_low][a]);
    const hi = complete.map(p => p.conditions[m.rate_high][a]);

    const traces = [
        {
            type: 'bar', name: m.rate_low, x: names, y: lo,
            marker: { color: CON_COLOR },
            hovertemplate: `%{x}<br>${m.rate_low} (${a}): %{y}<extra></extra>`,
        },
        {
            type: 'bar', name: m.rate_high, x: names, y: hi,
            marker: { color: INC_COLOR },
            hovertemplate: `%{x}<br>${m.rate_high} (${a}): %{y}<extra></extra>`,
        },
    ];
    const layout = {
        ...plotlyLayout,
        barmode: 'group',
        height: 260,
        margin: { t: 10, r: 15, b: 55, l: 55 },
        xaxis: { ...plotlyLayout.xaxis, tickfont: { size: 9 } },
        yaxis: { ...plotlyLayout.yaxis, title: { text: m.unit, font: { size: 10 } } },
    };
    Plotly.newPlot(containerId, traces, layout, plotlyConfig);
}

// ── Provenance modal ──
function openProvenance(measureKey) {
    if (!refreshData) return;
    const m = refreshData.measures.find(x => x.key === measureKey);
    if (!m) return;
    const dp = m.decimals;

    document.getElementById('prov-title').textContent = `${m.label} — computation trace`;

    const stagesHtml = (m.provenance.stages || []).map(s => {
        const vals = Object.entries(s.values || {});
        const valHtml = vals.length
            ? `<div class="prov-values">${vals.map(([k, v]) =>
                `<span class="prov-kv"><span class="pk">${escapeHtml(k)}</span> = <span class="pv">${escapeHtml(fmtProvVal(v))}</span></span>`).join('')}</div>`
            : '';
        const badge = s.retained
            ? '<span class="prov-badge kept">value retained</span>'
            : '<span class="prov-badge discarded">intermediate not retained</span>';
        return `<li class="prov-stage">
            <div class="prov-stage-head"><span class="prov-stage-name">${escapeHtml(s.stage)}</span>${badge}</div>
            <div class="prov-stage-detail">${escapeHtml(s.detail)}</div>
            ${valHtml}
        </li>`;
    }).join('');

    // Per-participant worked example: the actual numbers behind the plot.
    const complete = m.participants.filter(p => p.has_both);
    let exampleHtml = '';
    if (complete.length) {
        const rows = complete.map(p => {
            const lo = p.conditions[m.rate_low];
            const hi = p.conditions[m.rate_high];
            return `<tr>
                <td>${escapeHtml(p.filename)}</td>
                <td>${fmtNum(lo.median, dp)} <span class="rr-n">(median of ${lo.n}; mean ${fmtNum(lo.mean, dp)})</span></td>
                <td>${fmtNum(hi.median, dp)} <span class="rr-n">(median of ${hi.n}; mean ${fmtNum(hi.mean, dp)})</span></td>
                <td>${p.diff_median > 0 ? '+' : ''}${fmtNum(p.diff_median, dp)} <span class="rr-n">(mean-agg ${p.diff_mean > 0 ? '+' : ''}${fmtNum(p.diff_mean, dp)})</span></td>
            </tr>`;
        }).join('');
        const g = m.group;
        const gm = m.group_from_means;
        exampleHtml = `
        <h4 class="prov-h4">Worked values for this measure</h4>
        <p class="prov-note">Per-participant values are medians (primary); the mean
        is shown in parentheses for comparison. The signed Δ direction
        (${escapeHtml(m.diff_label || '165 − 60')}) is identical here, on screen and in the CSV.</p>
        <div class="compare-table-wrap">
            <table class="compare-table refresh-table">
                <thead><tr>
                    <th>Participant</th><th>${escapeHtml(m.rate_low)} median</th>
                    <th>${escapeHtml(m.rate_high)} median</th><th>Δ (median-agg)</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
        <div class="prov-headline">
            Group (primary, median-aggregated): mean Δ = <strong>${fmtNum(g.mean_diff, dp)}</strong>,
            median Δ = <strong>${fmtNum(g.median_diff, dp)}</strong>,
            SD = ${g.sd_diff == null ? '—' : fmtNum(g.sd_diff, dp)},
            95% CI = ${g.ci95_lo == null ? '—' : `[${fmtNum(g.ci95_lo, dp)}, ${fmtNum(g.ci95_hi, dp)}]`},
            N = ${g.n}.<br>
            Robustness (mean-aggregated): mean Δ = <strong>${fmtNum(gm.mean_diff, dp)}</strong>,
            median Δ = ${fmtNum(gm.median_diff, dp)}, SD = ${gm.sd_diff == null ? '—' : fmtNum(gm.sd_diff, dp)}.
        </div>`;
    }

    document.getElementById('prov-body').innerHTML = `
        <p class="prov-lead">The ordered path from raw signal to the plotted
        ${escapeHtml(m.diff_label || '165 Hz − 60 Hz')} value. Stages marked
        "value retained" expose the actual numbers used; those marked
        "intermediate not retained" were computed in the pipeline but not stored.</p>
        <ol class="prov-stages">${stagesHtml}</ol>
        ${exampleHtml}`;

    document.getElementById('provenance-modal').hidden = false;
}

function closeProvenance() {
    document.getElementById('provenance-modal').hidden = true;
}

function fmtProvVal(v) {
    if (Array.isArray(v)) return `[${v.join(', ')}]`;
    return String(v);
}

function fmtNum(v, dp) {
    if (v == null || Number.isNaN(v)) return '—';
    return Number(v).toFixed(dp);
}


// ── Session persistence ──
// The backend keeps parsed results in memory (until it restarts), so on a page
// refresh we can re-fetch the still-available subjects and restore the view the
// analyst was last on — no re-upload needed. We only persist lightweight view
// state (which subjects, which one was open); the heavy data comes from the API.
const SESSION_KEY = 'eeg.session.v1';
function saveSession(patch) {
    let cur = {};
    try { cur = JSON.parse(localStorage.getItem(SESSION_KEY)) || {}; } catch (_) {}
    const next = { ...cur, ...patch };
    try { localStorage.setItem(SESSION_KEY, JSON.stringify(next)); } catch (_) {}
}
function clearSession() {
    try { localStorage.removeItem(SESSION_KEY); } catch (_) {}
}
function readSession() {
    try { return JSON.parse(localStorage.getItem(SESSION_KEY)) || {}; }
    catch (_) { return {}; }
}

async function restoreSession() {
    let subjects;
    try {
        const resp = await fetch(`${API}/api/subjects`);
        if (!resp.ok) return;
        const data = await resp.json();
        subjects = data.subjects || [];
    } catch (_) {
        return;  // backend unreachable — stay on the upload screen
    }
    if (!subjects.length) {
        clearSession();
        return;
    }

    // Rebuild the in-memory subject list from what the backend still holds.
    uploadedSubjects = subjects.map(s => ({
        result_id: s.result_id,
        filename: s.filename,
    }));

    const sess = readSession();
    const stillHere = id => uploadedSubjects.some(s => s.result_id === id);

    if (sess.view === 'compare' && uploadedSubjects.length >= 2) {
        updateCompareButton();
        showComparison();
        return;
    }
    if (sess.view === 'refresh') {
        updateCompareButton();
        showRefreshComparison();
        return;
    }
    // Individual view: prefer the last-open subject, else the first available.
    const target = (sess.subjectId && stillHere(sess.subjectId))
        ? sess.subjectId
        : uploadedSubjects[0].result_id;
    await loadSubject(target);
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
    document.getElementById('btn-refresh-view').addEventListener('click', () => showRefreshComparison());
    document.getElementById('btn-refresh-from-compare').addEventListener('click', () => showRefreshComparison());

    document.getElementById('btn-new').addEventListener('click', async () => {
        for (const s of uploadedSubjects) {
            await fetch(`${API}/api/subjects/${s.result_id}`, { method: 'DELETE' });
        }
        uploadedSubjects = [];
        currentResultId = null;
        currentData = null;
        stagedFiles = [];
        clearSession();
        goToUpload();
    });

    document.getElementById('btn-back-individual').addEventListener('click', () => {
        document.getElementById('compare-section').hidden = true;
        document.getElementById('results-section').hidden = false;
    });

    // Refresh-rate view navigation
    document.getElementById('btn-refresh-back').addEventListener('click', () => {
        document.getElementById('refresh-section').hidden = true;
        if (uploadedSubjects.length >= 2) {
            showComparison();
        } else {
            document.getElementById('results-section').hidden = false;
        }
    });
    document.getElementById('btn-refresh-add-more').addEventListener('click', () => {
        document.getElementById('refresh-section').hidden = true;
        goToUpload();
    });
    document.getElementById('btn-refresh-download').addEventListener('click', () => {
        window.location.href = `${API}/api/download-csv-refresh`;
    });

    // Per-participant aggregation toggle (median / mean / side by side).
    // Inspection only: re-renders the tables and charts from the already-loaded
    // data; never re-fetches, never touches the group headline or the export.
    const aggToggle = document.getElementById('refresh-agg-toggle');
    if (aggToggle) {
        aggToggle.querySelectorAll('.seg-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                refreshAgg = btn.dataset.agg;
                aggToggle.querySelectorAll('.seg-btn').forEach(b =>
                    b.classList.toggle('active', b === btn));
                if (refreshData) renderRefreshMeasures(refreshData.measures);
            });
        });
    }

    // Provenance modal close (button, overlay click, Escape)
    document.getElementById('prov-close').addEventListener('click', closeProvenance);
    document.getElementById('provenance-modal').addEventListener('click', (e) => {
        if (e.target.id === 'provenance-modal') closeProvenance();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeProvenance();
    });

    // Epoch viewer modal close (button, overlay click, Escape)
    document.getElementById('epoch-close').addEventListener('click', closeEpochViewer);
    document.getElementById('epoch-modal').addEventListener('click', (e) => {
        if (e.target.id === 'epoch-modal') closeEpochViewer();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeEpochViewer();
    });

    // Raw EEG Data viewer (button + modal close)
    const rawBtn = document.getElementById('btn-raw-eeg');
    if (rawBtn) rawBtn.addEventListener('click', openRawEeg);
    const rawClose = document.getElementById('raweeg-close');
    if (rawClose) rawClose.addEventListener('click', closeRawEeg);
    const rawModal = document.getElementById('raweeg-modal');
    if (rawModal) rawModal.addEventListener('click', (e) => {
        if (e.target.id === 'raweeg-modal') closeRawEeg();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeRawEeg();
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
        document.getElementById('refresh-section').hidden = true;
        goToUpload();
    });
}

document.addEventListener('DOMContentLoaded', () => {
    initBgWave();
    initUpload();
    initActions();
    initChartExpand();
    // Restore a previous session from backend memory (survives page refresh
    // as long as the server hasn't restarted).
    restoreSession();
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

        // Grouping toggle (Condition ↔ Refresh rate) — only for spectrum panels.
        // Created unconditionally (like the mode button); its visibility is
        // managed by updateSpectrumGroupToggles() once data is loaded.
        if (isSpectrum && !actions.querySelector('.chart-group-btn')) {
            panel.dataset.group = panel.dataset.group || 'condition';
            const gToggle = document.createElement('button');
            gToggle.className = 'chart-group-btn';
            gToggle.type = 'button';
            gToggle.hidden = true;  // shown when refresh-rate data is available
            const paintGroup = () => {
                const isRefresh = panel.dataset.group === 'refresh';
                gToggle.textContent = isRefresh ? 'Group: refresh rate' : 'Group: condition';
                gToggle.classList.toggle('active', isRefresh);
            };
            paintGroup();
            gToggle.title = 'Group spectra by congruency or by refresh-rate block';
            gToggle.addEventListener('click', (e) => {
                e.stopPropagation();
                panel.dataset.group = panel.dataset.group === 'refresh' ? 'condition' : 'refresh';
                paintGroup();
                drawSpectrumChart(container.id, panel.dataset.mode, panel.dataset.group);
            });
            actions.appendChild(gToggle);
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

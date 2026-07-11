"""
EEG processing pipeline - stages 3-10 from the reference:
  3. 1 Hz high-pass on continuous signal (MNE)
  4. Epoch 0-500 ms + ±300 ms padding for wavelet edges
  5. Per-trial metrics: fz_ptp, beta (FFT), maxz, impact, coinc, theta (FFT)
  6. Per-channel exclusion (Fz-Pz vs C3-C4 get their own trial sets)
  8-9. Complex Morlet CWT (pywt) → absolute + relative theta/beta power
  10. Summary stats + balance check

Configuration constants match pipeline_stages_1to6.py and _8to10_wavelet.py
from the reference zip - do NOT tune per-participant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import numpy as np
import pandas as pd
import mne
import pywt

from parser import ParsedEEG, Trial
from logging_setup import get_logger

logger = get_logger(__name__)

mne.set_log_level("ERROR")


# ============================================================
#  Frozen configuration - identical for every recording
# ============================================================
HP_HZ         = 1.0             # high-pass cutoff (Hz)
WIN_S         = 0.500           # analysis window (s)
PAD_S         = 0.300           # epoch padding each side (>= wavelet reach)
REACH_S       = 0.179           # wavelet reach (1.5 * 3 cycles / (2π * 4 Hz))

# Exclusion thresholds (from S1P002 validation)
BLINK_UV      = 80              # Fz peak-to-peak (µV) — legacy full-band cutoff,
                               # retained only for reporting/back-compat. The
                               # blink DECISION now uses the adaptive rule below.
# Adaptive blink detection (work-order Task 4). Blinks are detected on a
# 1-7 Hz low-passed Fz signal (isolates the slow blink shape from higher-freq
# noise) and thresholded at median + BLINK_K · MAD of THIS recording's own
# slow-band peak-to-peak deflections, computed over the 675 ms check window.
# The reference *distribution* is per-recording; BLINK_K is a single frozen
# constant applied identically to every file (hard rule 1). BLINK_K = 10.0 is
# frozen to reproduce S1P002's reference blink set (43-44 flags, θ surv 116);
# because each file uses its own median/MAD, the same K catches the smaller
# 30-50 µV blinks on other recordings.
BLINK_SLOW_HZ = 7.0            # low-pass (Hz) isolating the slow blink shape
BLINK_K       = 10.0           # frozen MAD multiplier (see note above)
EMG_BETA      = 150_000         # C3 beta power (µV²) — legacy fixed cutoff,
                               # retained for reporting only. The EMG DECISION
                               # now uses the adaptive rule below.
# Adaptive EMG rejection (work-order Task 5). The old fixed 150 000 µV² cutoff
# was tuned to S1P002 and sliced through the middle of a normal beta
# distribution on higher-amplitude recordings (S2P003: 45 trials dropped).
# Replace with median + EMG_K · (1.4826·MAD) of THIS recording's own C3 beta
# power distribution — a robust (normalised-MAD) z-threshold on the recording's
# own scale. Computed condition-blind: congruent and incongruent trials are
# pooled when estimating the threshold, so rejection cannot bias the
# 60-vs-165 contrast (hard rule 5). EMG_K is a single frozen constant applied
# identically to every file (hard rule 1). EMG_K = 4.5 reproduces S1P002's
# reference EMG set exactly (trials 81/82/83/128; ~4 rejections) and sits at
# the sensitive edge of the 4.5-4.9 range that does so.
EMG_K         = 4.5            # frozen normalised-MAD multiplier (see note)
BURST_Z       = 3.6            # C3 max |z| in window .... AND ....
BURST_IMPACT  = 15              # ... % change in beta when spike removed
COINC_Z       = 3.0             # simultaneous z on BOTH channels

# Wavelet (Morlet, complex)
THETA_BAND    = (4.0, 8.0)      # Hz @ Fz-Pz
BETA_BAND     = (13.0, 30.0)    # Hz @ C3-C4
THETA_CYC     = 3.0             # Morlet cycles for theta
BETA_CYC      = 7.0             # Morlet cycles for beta
TOTAL_BAND    = (1.0, 35.0)     # denominator for relative power
# NB: capped at 35 Hz (work-order Task 6). The 50 Hz acquisition low-pass
# (confirmed from victoria_EEG_settings.adiset) rolls off before 50 Hz and
# already attenuates 35-40 Hz, which would otherwise depress this denominator
# and uniformly inflate every relative-power value. Capping at 35 keeps the
# denominator entirely inside the flat passband. The theta (4-8 Hz) and beta
# (13-30 Hz) numerators are unaffected; both remain fully inside the band.
FREQ_STEP     = 0.5             # Hz


# ============================================================
#  Output dataclasses
# ============================================================
@dataclass
class TrialResult:
    """Everything computed for one trial."""
    trial: int
    btrial: int
    block: int
    cond: str                 # 'con' or 'first'
    onset: float
    key: float
    rt_ms: int

    # Stage 5 metrics
    fz_ptp: float             # peak-to-peak Fz over window+reach (µV)
    theta_fft: float          # crude theta band FFT power (used for cross-checks)
    beta_fft: float           # crude beta band FFT power (µV²)
    maxz: float               # max |z-score| on C3 over window
    impact: float             # % change in beta_fft when peak spike removed
    coinc: float              # min z across channels at same time (max)

    # Stage 6 exclusion
    blink: bool
    fz_exclude: bool
    c3_exclude: bool
    reason: str

    # Stage 8-9 wavelet power
    theta_abs: float          # absolute theta (µV²)
    theta_rel: float          # theta / total 1-40 Hz
    beta_abs: float
    beta_rel: float

    # Behavioural cross-reference (Doc 6). True when this EEG trial was aligned
    # to a flanker (OpenSesame) row that the participant answered INCORRECTLY.
    # Such trials are excluded from ALL analyses (theta, beta, RT) — an
    # incorrect response means the cognitive process of interest did not run to
    # completion, so the epoch is not comparable. Set post-alignment by
    # apply_response_errors(); False until behavioural data is aligned.
    response_error: bool = False

    @property
    def theta_excluded(self) -> bool:
        """Effective Fz-Pz/theta exclusion = EEG artifact OR incorrect response."""
        return self.fz_exclude or self.response_error

    @property
    def beta_excluded(self) -> bool:
        """Effective C3-C4/beta exclusion = EEG artifact OR incorrect response."""
        return self.c3_exclude or self.response_error


@dataclass
class ChannelSummary:
    """Aggregate summary for one analysis channel."""
    channel: str              # 'Fz-Pz' or 'C3-C4'
    band: str                 # 'theta' or 'beta'
    surviving: int            # trials passing exclusion
    excluded: int
    surviving_by_condition: dict     # {block: {'con': n, 'first': n}}
    excluded_by_condition: dict
    exclusion_pct_con: float
    exclusion_pct_inc: float
    balance_flag: bool        # True if |con% - inc%| > 15
    # medians on surviving trials
    abs_median_con: float
    abs_median_inc: float
    rel_median_con: float
    rel_median_inc: float
    # per-block breakdown: {block_number: {'n_total_con', 'n_total_inc',
    #     'n_surv_con', 'n_surv_inc', 'n_exc_con', 'n_exc_inc',
    #     'exclusion_pct_con', 'exclusion_pct_inc',
    #     'abs_median_con', 'abs_median_inc',
    #     'rel_median_con', 'rel_median_inc'}}
    by_block: dict
    # Channel-scoped exclusion (work-order Task 7). When a contamination
    # finding (e.g. S005 on C3-C4 beta) invalidates ONE derivation/band, that
    # channel is marked excluded here while the other channel and behavioural
    # data are retained. When True, power values above are not to be reported
    # (the payload nulls them) and `exclusion_code`/`exclusion_reason` say why.
    channel_excluded: bool = False
    exclusion_code: str = ""
    exclusion_reason: str = ""


@dataclass
class PipelineResult:
    """Complete pipeline output for one recording."""
    filename: str
    recording_date: str
    sampling_rate: float
    channel_names: List[str]
    n_trials: int
    n_blocks: int
    trials: List[TrialResult]
    theta_summary: ChannelSummary
    beta_summary: ChannelSummary

    # Averaged wavelet spectra for plotting (surviving trials only)
    spectrum_freqs: np.ndarray             # Hz
    theta_spectrum_con: np.ndarray         # mean |CWT|² across surviving con trials, Fz
    theta_spectrum_inc: np.ndarray
    beta_spectrum_con: np.ndarray          # mean |CWT|² across surviving con trials, C3
    beta_spectrum_inc: np.ndarray
    # Averaged spectra across *excluded* trials (both conditions pooled)
    theta_spectrum_excluded: np.ndarray
    beta_spectrum_excluded: np.ndarray
    # Per-trial spectra (n_trials × n_freqs) for the "all trials" toggle view
    theta_spec_all: np.ndarray
    beta_spec_all: np.ndarray

    # C3-C4 spectral-shape metrics over analysed epochs (Task 8 / S005 inputs).
    c3_beta_share: float = 0.0        # 13-30 Hz ÷ total
    c3_high_share: float = 0.0        # 30-TOTAL_BAND[1] Hz ÷ total
    # Median Fz-Pz peak-to-peak (µV) over analysed epochs — the amplitude-scale
    # descriptor the cohort-level C006 outlier check compares across recordings.
    fz_ptp_median: float = 0.0
    # Adaptive-threshold values this recording used (for QC display).
    blink_threshold_uv: float = 0.0
    emg_threshold: float = 0.0


# ============================================================
#  Helpers
# ============================================================
def _bandpow(x: np.ndarray, lo: float, hi: float, fs: float) -> float:
    """Hann-windowed FFT band power (µV²), matching the reference."""
    x = x - x.mean()
    X = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    f = np.fft.rfftfreq(len(x), 1.0 / fs)
    return float(X[(f >= lo) & (f < hi)].sum())


def _wavelet_name(cycles: float) -> str:
    """Complex Morlet 'cmorB-C'; B (bandwidth) encodes the cycle count."""
    return f"cmor{cycles / (2 * np.pi):.4f}-1.0"


def _cwt_power(seg: np.ndarray, fs: float, cycles: float, freqs: np.ndarray) -> np.ndarray:
    """Return |CWT|² at each frequency, shape (n_freqs, n_samples)."""
    wav = _wavelet_name(cycles)
    scales = pywt.central_frequency(wav) * fs / freqs
    coef, _ = pywt.cwt(seg, scales, wav, sampling_period=1.0 / fs)
    return np.abs(coef) ** 2


# ============================================================
#  Main pipeline
# ============================================================
def run_pipeline(parsed: ParsedEEG) -> PipelineResult:
    """Run stages 3-10 on parsed EEG data and return a PipelineResult."""
    fs = parsed.sampling_rate
    trials_meta: List[Trial] = parsed.trials

    # ---- STAGE 3: high-pass ------------------------------------------------
    # MNE expects (n_channels, n_times) in Volts
    raw_data = np.stack([parsed.fz, parsed.c3]) * 1e-6

    # NaN-safe filtering (work-order Task 1). The parser stores np.nan for
    # samples LabChart wrote as "NaN" (amplifier saturation) and for the
    # inter-segment gaps. If those NaN reach MNE's FIR high-pass they would
    # smear across the whole recording via the filter kernel and silently
    # poison every downstream epoch. So we:
    #   1. remember exactly which samples were NaN in the raw arrays, and
    #   2. zero them before filtering (a local, bounded edit),
    # then later DROP any trial whose analysed window overlaps an
    # originally-NaN sample. Zeroing is safe only because those trials never
    # contribute to any metric — the drop happens before Stage 5.
    nan_mask = ~np.isfinite(raw_data)          # (2, n_times) True where NaN/Inf
    n_nan_samples = int(nan_mask.any(axis=0).sum())
    if n_nan_samples:
        logger.info(
            "%s: %d sample(s) NaN/Inf in raw signal; zeroed for filtering, "
            "trials overlapping them will be dropped (S002)",
            parsed.filename, n_nan_samples,
        )
        raw_data = np.where(nan_mask, 0.0, raw_data)
    bad_sample = nan_mask.any(axis=0)          # (n_times,) True if either channel NaN here

    info = mne.create_info(["Fz-Pz", "C3-C4"], fs, "eeg")
    raw = mne.io.RawArray(raw_data, info, verbose=False)
    raw_hp = raw.copy().filter(l_freq=HP_HZ, h_freq=None, verbose=False)

    FZ = raw_hp.get_data(picks="Fz-Pz")[0] * 1e6   # back to µV
    C3 = raw_hp.get_data(picks="C3-C4")[0] * 1e6
    T_axis = raw_hp.times

    # ---- STAGE 3b: drop trials whose analysed window overlaps NaN (S002) --
    # Check window is onset .. onset + WIN_S + REACH_S (the 675 ms window the
    # wavelet actually reaches into), scoped per trial — never the whole file
    # (hard rule 3). A trial touching an originally-NaN sample is dropped here
    # so NaN never enters any FFT/wavelet. Reasons are recorded on the parsed
    # object for the S002 check to surface.
    check_win_samples = int(round((WIN_S + REACH_S) * fs))
    dropped_nan: List[dict] = []
    kept_trials: List[Trial] = []
    n_total = len(raw_data[0])
    for t in trials_meta:
        lo = t.onset_sample_concat
        hi = min(lo + check_win_samples, n_total)
        if lo < 0 or lo >= n_total or bad_sample[lo:hi].any():
            dropped_nan.append({
                "trial": t.trial, "block": t.block, "cond": t.cond,
                "onset_sample": lo,
            })
            continue
        kept_trials.append(t)
    if dropped_nan:
        logger.warning(
            "%s: dropped %d trial(s) with NaN in analysed window (S002): %s",
            parsed.filename, len(dropped_nan),
            ", ".join(str(d["trial"]) for d in dropped_nan),
        )
    # Expose for the S002 validity check (does not mutate parsed.trials).
    parsed.nan_dropped_trials = dropped_nan
    trials_meta = kept_trials
    if not trials_meta:
        logger.error("%s: all trials dropped for NaN; no analysable data", parsed.filename)

    # ---- STAGE 4: build events + Epochs (window + padding) ----------------
    # NB: use the concatenated sample index (segment-aware), not `t.onset * fs`.
    # For single-segment files the two are identical; for multi-segment files
    # (recording restart), only the concatenated index maps correctly into the
    # stacked fz/c3 arrays. See parser.Trial.onset_sample_concat.
    events = np.column_stack([
        np.array([t.onset_sample_concat for t in trials_meta], dtype=int),
        np.zeros(len(trials_meta), int),
        np.array([1 if t.cond == "con" else 2 for t in trials_meta]),
    ])
    metadata = pd.DataFrame([{
        "trial": t.trial, "btrial": t.btrial, "block": t.block,
        "cond": t.cond, "onset": t.onset, "key": t.key, "rt_ms": t.rt_ms,
    } for t in trials_meta])

    epochs = mne.Epochs(
        raw_hp, events, {"con": 1, "first": 2},
        tmin=-PAD_S, tmax=WIN_S + PAD_S,
        baseline=None, metadata=metadata, preload=True, verbose=False,
    )

    # ---- STAGE 5: per-trial metrics (on the epochs, in µV) ----------------
    d = epochs.get_data(copy=True) * 1e6
    T = epochs.times
    win_mask = (T >= 0) & (T <= WIN_S)

    n_trials = d.shape[0]
    metrics = []
    for i in range(n_trials):
        fzw = d[i, 0][win_mask]
        c3w = d[i, 1][win_mask]

        theta_fft = _bandpow(fzw, THETA_BAND[0], THETA_BAND[1], fs)
        beta_fft  = _bandpow(c3w, BETA_BAND[0],  BETA_BAND[1],  fs)

        c3_z = np.abs((c3w - c3w.mean()) / (c3w.std() + 1e-12))
        maxz = float(c3_z.max())

        # Beta-impact: does removing the most extreme sample change beta?
        pk = int(np.argmax(c3_z))
        m = slice(max(0, pk - 2), min(len(c3w), pk + 3))
        idx = np.arange(len(c3w))
        good = np.ones(len(c3w), bool); good[m] = False
        c3_cleaned = c3w.copy()
        c3_cleaned[m] = np.interp(idx[m], idx[good], c3w[good])
        b_clean = _bandpow(c3_cleaned, BETA_BAND[0], BETA_BAND[1], fs)
        impact = float(abs(beta_fft - b_clean) / beta_fft * 100) if beta_fft > 0 else 0.0

        fz_z = np.abs((fzw - fzw.mean()) / (fzw.std() + 1e-12))
        coinc = float(np.max(np.minimum(fz_z, c3_z)))

        metrics.append(dict(
            theta_fft=theta_fft, beta_fft=beta_fft,
            maxz=maxz, impact=impact, coinc=coinc,
        ))

    # ---- STAGE 6: blink detection + exclusion flags -----------------------
    # Adaptive blink detection (work-order Task 4). Detect on a 1-7 Hz
    # low-passed Fz signal so the slow blink deflection is isolated from
    # higher-frequency noise, then threshold at median + BLINK_K · MAD of THIS
    # recording's own slow-band peak-to-peak values over the 675 ms check
    # window (WIN_S + REACH_S, hard rule 6). The distribution is per-recording;
    # BLINK_K is frozen (hard rule 1). We index the continuous slow-band signal
    # by each trial's concatenated onset sample — the same windowing used for
    # the NaN check — rather than re-epoching.
    raw_slow = raw_hp.copy().filter(l_freq=None, h_freq=BLINK_SLOW_HZ, verbose=False)
    fz_slow = raw_slow.get_data(picks="Fz-Pz")[0] * 1e6   # µV, continuous
    n_cont = len(fz_slow)
    check_win = int(round((WIN_S + REACH_S) * fs))
    slow_ptp = np.empty(n_trials, dtype=float)
    for i, t in enumerate(trials_meta):
        lo = t.onset_sample_concat
        hi = min(lo + check_win, n_cont)
        seg = fz_slow[lo:hi]
        slow_ptp[i] = float(seg.max() - seg.min()) if seg.size else 0.0

    # median + K·MAD threshold, this recording's own slow-band distribution
    slow_median = float(np.median(slow_ptp)) if n_trials else 0.0
    slow_mad = float(np.median(np.abs(slow_ptp - slow_median))) if n_trials else 0.0
    blink_thresh = slow_median + BLINK_K * slow_mad
    blink_flags = [bool(v > blink_thresh) for v in slow_ptp]
    logger.info(
        "%s blink (adaptive slow-band): median=%.1f MAD=%.1f K=%.1f thr=%.1fµV -> %d flagged",
        parsed.filename, slow_median, slow_mad, BLINK_K, blink_thresh, sum(blink_flags),
    )
    # Expose slow-band inputs for the S007 check (missed small blinks).
    parsed.blink_slow_ptp = slow_ptp.tolist()
    parsed.blink_threshold_uv = blink_thresh

    # Also read Fz peak-to-peak (µV) over window+reach for reporting (legacy
    # full-band metric; no longer drives the decision).
    span = epochs.copy().crop(0, WIN_S + REACH_S)
    span_data = span.get_data(copy=True) * 1e6
    fz_ptp = span_data[:, 0, :].max(axis=1) - span_data[:, 0, :].min(axis=1)

    # ---- STAGE 8-9: wavelet power on the padded epoch --------------------
    FREQS = np.arange(TOTAL_BAND[0], TOTAL_BAND[1] + 1e-6, FREQ_STEP)
    TH_MASK = (FREQS >= THETA_BAND[0]) & (FREQS <= THETA_BAND[1])
    BE_MASK = (FREQS >= BETA_BAND[0])  & (FREQS <= BETA_BAND[1])

    # Analysis-window slice within the padded epoch
    a = int(PAD_S * fs)
    b = a + int(WIN_S * fs)

    theta_spec_cache = []   # one array per trial: mean over window, per freq (Fz)
    beta_spec_cache  = []   # same, for C3

    for i in range(n_trials):
        # Use the padded epoch from mne
        fz_seg = d[i, 0]
        c3_seg = d[i, 1]
        pz = _cwt_power(fz_seg, fs, THETA_CYC, FREQS)[:, a:b].mean(axis=1)   # per-freq
        pc = _cwt_power(c3_seg, fs, BETA_CYC,  FREQS)[:, a:b].mean(axis=1)
        theta_spec_cache.append(pz)
        beta_spec_cache.append(pc)

    theta_spec = np.array(theta_spec_cache)   # (n_trials, n_freqs)
    beta_spec  = np.array(beta_spec_cache)

    theta_abs = theta_spec[:, TH_MASK].mean(axis=1)
    theta_tot = theta_spec.sum(axis=1)
    theta_rel = np.where(theta_tot > 0, theta_spec[:, TH_MASK].sum(axis=1) / theta_tot, 0.0)

    beta_abs = beta_spec[:, BE_MASK].mean(axis=1)
    beta_tot = beta_spec.sum(axis=1)
    beta_rel = np.where(beta_tot > 0, beta_spec[:, BE_MASK].sum(axis=1) / beta_tot, 0.0)

    # ---- C3 contamination metrics (work-order Task 8 / S005 inputs) -------
    # Spectral-shape descriptors of the C3-C4 derivation, averaged over the
    # analysed epochs (hard rule 3 — never the whole file). Broadband EMG piles
    # power into and above the beta band, so beta share and 30-Hz-and-up share
    # rise together. Computed from THIS recording's own mean beta wavelet
    # spectrum. NB: TOTAL_BAND caps at 35 Hz (Task 6), so the "high band" here
    # is 30-35 Hz, not the doc's 30-40 Hz; the S005 threshold is calibrated to
    # this basis when the real fixtures arrive. Stored, not yet thresholded.
    HI_MASK = (FREQS >= 30.0) & (FREQS <= TOTAL_BAND[1])
    mean_beta_spec = beta_spec.mean(axis=0) if len(beta_spec) else np.zeros_like(FREQS)
    c3_total_power = float(mean_beta_spec.sum())
    if c3_total_power > 0:
        c3_beta_share = float(mean_beta_spec[BE_MASK].sum() / c3_total_power)
        c3_high_share = float(mean_beta_spec[HI_MASK].sum() / c3_total_power)
    else:
        c3_beta_share = c3_high_share = 0.0
    logger.info(
        "%s C3 spectral shape: beta(13-30)=%.1f%% high(30-%d)=%.1f%%",
        parsed.filename, c3_beta_share * 100, int(TOTAL_BAND[1]), c3_high_share * 100,
    )

    # ---- Adaptive EMG threshold (work-order Task 5) -----------------------
    # median + EMG_K · (1.4826·MAD) of THIS recording's own C3 beta-power
    # distribution, pooled across conditions (condition-blind, hard rule 5).
    # The scale is per-recording; EMG_K is frozen (hard rule 1).
    beta_fft_all = np.array([m["beta_fft"] for m in metrics], dtype=float)
    if beta_fft_all.size:
        emg_median = float(np.median(beta_fft_all))
        emg_mad = float(np.median(np.abs(beta_fft_all - emg_median)))
        emg_thresh = emg_median + EMG_K * 1.4826 * emg_mad
    else:
        emg_median = emg_mad = 0.0
        emg_thresh = EMG_BETA
    logger.info(
        "%s EMG (adaptive C3 beta): median=%.0f MAD=%.0f K=%.1f thr=%.0fµV² -> %d over threshold",
        parsed.filename, emg_median, emg_mad, EMG_K, emg_thresh,
        int((beta_fft_all > emg_thresh).sum()),
    )
    parsed.emg_threshold = emg_thresh

    # ---- Assemble per-trial results + exclusion decisions -----------------
    trial_results: List[TrialResult] = []
    for i, tm in enumerate(trials_meta):
        m = metrics[i]
        blink = bool(blink_flags[i])
        coinc = m["coinc"]
        beta_val = m["beta_fft"]
        maxz = m["maxz"]
        impact = m["impact"]

        fz_exclude = blink or (coinc > COINC_Z)
        c3_exclude = (beta_val > emg_thresh) or (maxz > BURST_Z and impact > BURST_IMPACT) or (coinc > COINC_Z)

        reasons = []
        if blink:
            reasons.append(f"blink (Fz slow-band ptp>{blink_thresh:.0f}µV)")
        if coinc > COINC_Z:
            reasons.append(f"coincident transient (z={coinc:.1f})")
        if beta_val > emg_thresh:
            reasons.append(f"gross EMG (beta={beta_val:,.0f}>{emg_thresh:,.0f})")
        if maxz > BURST_Z and impact > BURST_IMPACT:
            reasons.append(f"C3 burst (z={maxz:.1f}, beta impact {impact:.0f}%)")
        reason = "; ".join(reasons)

        trial_results.append(TrialResult(
            trial=tm.trial, btrial=tm.btrial, block=tm.block, cond=tm.cond,
            onset=tm.onset, key=tm.key, rt_ms=tm.rt_ms,
            fz_ptp=float(fz_ptp[i]),
            theta_fft=m["theta_fft"], beta_fft=beta_val,
            maxz=maxz, impact=impact, coinc=coinc,
            blink=blink, fz_exclude=fz_exclude, c3_exclude=c3_exclude, reason=reason,
            theta_abs=float(theta_abs[i]), theta_rel=float(theta_rel[i]),
            beta_abs=float(beta_abs[i]), beta_rel=float(beta_rel[i]),
        ))

    # ---- Summaries ---------------------------------------------------------
    # Use the EFFECTIVE per-band exclusion (EEG artifact OR incorrect response,
    # Doc 6). At pipeline time no behavioural data is aligned yet, so
    # response_error is False and this equals the pure-EEG decision; after
    # alignment, recompute_after_response_errors() rebuilds these.
    theta_summary = _channel_summary(trial_results, "Fz-Pz", "theta", "theta_excluded")
    beta_summary  = _channel_summary(trial_results, "C3-C4", "beta",  "beta_excluded")

    # ---- Averaged wavelet spectra (surviving trials only, for plotting) ---
    def _mean_spec(spec: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if not mask.any():
            return np.zeros_like(FREQS)
        return spec[mask].mean(axis=0)

    keep_theta = np.array([not t.theta_excluded for t in trial_results])
    keep_beta  = np.array([not t.beta_excluded for t in trial_results])
    con_mask   = np.array([t.cond == "con"   for t in trial_results])
    inc_mask   = np.array([t.cond == "first" for t in trial_results])

    return PipelineResult(
        filename=parsed.filename,
        recording_date=parsed.recording_date,
        sampling_rate=fs,
        channel_names=parsed.channel_names,
        n_trials=n_trials,
        n_blocks=parsed.n_blocks,
        trials=trial_results,
        theta_summary=theta_summary,
        beta_summary=beta_summary,
        spectrum_freqs=FREQS,
        theta_spectrum_con=_mean_spec(theta_spec, keep_theta & con_mask),
        theta_spectrum_inc=_mean_spec(theta_spec, keep_theta & inc_mask),
        beta_spectrum_con=_mean_spec(beta_spec,  keep_beta  & con_mask),
        beta_spectrum_inc=_mean_spec(beta_spec,  keep_beta  & inc_mask),
        theta_spectrum_excluded=_mean_spec(theta_spec, (~keep_theta)),
        beta_spectrum_excluded=_mean_spec(beta_spec,  (~keep_beta)),
        theta_spec_all=theta_spec,
        beta_spec_all=beta_spec,
        c3_beta_share=c3_beta_share,
        c3_high_share=c3_high_share,
        fz_ptp_median=float(np.median(fz_ptp)) if len(fz_ptp) else 0.0,
        blink_threshold_uv=blink_thresh,
        emg_threshold=emg_thresh,
    )


def apply_channel_exclusion(result: PipelineResult, channel: str, code: str, reason: str) -> None:
    """Mark one derivation/band as scope-excluded (work-order Task 7).

    A contamination finding (e.g. S005 on C3-C4 beta) invalidates that channel
    while the other channel and behavioural data are retained. This flags the
    matching ChannelSummary; the payload then nulls its power values so they
    cannot be mistaken for valid, and records the triggering code + measured
    value. Idempotent; safe to call for either 'Fz-Pz' or 'C3-C4'.
    """
    for summary in (result.theta_summary, result.beta_summary):
        if summary.channel == channel:
            summary.channel_excluded = True
            summary.exclusion_code = code
            summary.exclusion_reason = reason
            logger.warning(
                "%s: channel-scoped exclusion — %s %s (%s)",
                result.filename, channel, summary.band, reason,
            )
            return
    logger.error("apply_channel_exclusion: unknown channel %r", channel)


def recompute_after_response_errors(result: PipelineResult) -> None:
    """Rebuild summaries + averaged spectra after ``response_error`` flags change.

    Doc 6: incorrectly-responded trials are excluded from all analyses. The
    ``response_error`` flags are set post-alignment (behavioural upload), which
    happens after ``run_pipeline`` has already built the summaries/spectra from
    the pure-EEG decision. Call this whenever the flags are (re)assigned to fold
    the behavioural exclusion into the theta/beta channel summaries and the
    averaged wavelet spectra. Uses the effective ``theta_excluded`` /
    ``beta_excluded`` properties (EEG artifact OR incorrect response).

    Idempotent: it recomputes from ``theta_spec_all`` / ``beta_spec_all`` (the
    retained per-trial spectra, unaffected by exclusion) and the current flags,
    so repeated behavioural uploads converge to the same state. Preserves any
    channel-scoped exclusion metadata already on the summaries.
    """
    trials = result.trials

    # Preserve channel-scoped exclusion metadata across the rebuild.
    prev = {
        "Fz-Pz": (result.theta_summary.channel_excluded,
                  result.theta_summary.exclusion_code,
                  result.theta_summary.exclusion_reason),
        "C3-C4": (result.beta_summary.channel_excluded,
                  result.beta_summary.exclusion_code,
                  result.beta_summary.exclusion_reason),
    }

    result.theta_summary = _channel_summary(trials, "Fz-Pz", "theta", "theta_excluded")
    result.beta_summary  = _channel_summary(trials, "C3-C4", "beta",  "beta_excluded")
    for summary, chan in ((result.theta_summary, "Fz-Pz"), (result.beta_summary, "C3-C4")):
        exc, code, reason = prev[chan]
        summary.channel_excluded = exc
        summary.exclusion_code = code
        summary.exclusion_reason = reason

    FREQS = np.arange(TOTAL_BAND[0], TOTAL_BAND[1] + 1e-6, FREQ_STEP)
    theta_spec = result.theta_spec_all
    beta_spec = result.beta_spec_all

    def _mean_spec(spec: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if spec is None or len(spec) == 0 or not mask.any():
            return np.zeros_like(FREQS)
        return spec[mask].mean(axis=0)

    keep_theta = np.array([not t.theta_excluded for t in trials])
    keep_beta  = np.array([not t.beta_excluded for t in trials])
    con_mask   = np.array([t.cond == "con"   for t in trials])
    inc_mask   = np.array([t.cond == "first" for t in trials])

    result.theta_spectrum_con = _mean_spec(theta_spec, keep_theta & con_mask)
    result.theta_spectrum_inc = _mean_spec(theta_spec, keep_theta & inc_mask)
    result.beta_spectrum_con  = _mean_spec(beta_spec,  keep_beta  & con_mask)
    result.beta_spectrum_inc  = _mean_spec(beta_spec,  keep_beta  & inc_mask)
    result.theta_spectrum_excluded = _mean_spec(theta_spec, ~keep_theta)
    result.beta_spectrum_excluded  = _mean_spec(beta_spec,  ~keep_beta)


def _channel_summary(trials: List[TrialResult], channel: str, band: str, exclude_attr: str) -> ChannelSummary:
    """Build the aggregate ChannelSummary for one analysis channel."""
    total = len(trials)
    excluded = [t for t in trials if getattr(t, exclude_attr)]
    surviving = [t for t in trials if not getattr(t, exclude_attr)]

    surviving_by_cond: dict = {}
    excluded_by_cond: dict = {}
    for blk in sorted({t.block for t in trials}):
        surviving_by_cond[blk] = {}
        excluded_by_cond[blk] = {}
        for cond in ("con", "first"):
            surviving_by_cond[blk][cond] = sum(1 for t in surviving if t.block == blk and t.cond == cond)
            excluded_by_cond[blk][cond]  = sum(1 for t in excluded  if t.block == blk and t.cond == cond)

    n_con = sum(1 for t in trials if t.cond == "con")
    n_inc = sum(1 for t in trials if t.cond == "first")
    n_ex_con = sum(1 for t in excluded if t.cond == "con")
    n_ex_inc = sum(1 for t in excluded if t.cond == "first")
    pct_con = 100.0 * n_ex_con / n_con if n_con else 0.0
    pct_inc = 100.0 * n_ex_inc / n_inc if n_inc else 0.0
    balance_flag = abs(pct_con - pct_inc) > 15.0

    abs_attr = f"{band}_abs"
    rel_attr = f"{band}_rel"

    def _median(cond: str, attr: str) -> float:
        vals = [getattr(t, attr) for t in surviving if t.cond == cond]
        return float(np.median(vals)) if vals else 0.0

    # Per-block breakdown
    by_block: dict = {}
    for blk in sorted({t.block for t in trials}):
        trials_blk = [t for t in trials if t.block == blk]
        surv_blk = [t for t in surviving if t.block == blk]
        n_tot_con = sum(1 for t in trials_blk if t.cond == "con")
        n_tot_inc = sum(1 for t in trials_blk if t.cond == "first")
        n_surv_con = sum(1 for t in surv_blk if t.cond == "con")
        n_surv_inc = sum(1 for t in surv_blk if t.cond == "first")
        n_exc_con = n_tot_con - n_surv_con
        n_exc_inc = n_tot_inc - n_surv_inc

        def _median_blk(cond: str, attr: str) -> float:
            vals = [getattr(t, attr) for t in surv_blk if t.cond == cond]
            return float(np.median(vals)) if vals else 0.0

        by_block[blk] = {
            "n_total_con": n_tot_con,
            "n_total_inc": n_tot_inc,
            "n_surv_con": n_surv_con,
            "n_surv_inc": n_surv_inc,
            "n_exc_con": n_exc_con,
            "n_exc_inc": n_exc_inc,
            "exclusion_pct_con": (100.0 * n_exc_con / n_tot_con) if n_tot_con else 0.0,
            "exclusion_pct_inc": (100.0 * n_exc_inc / n_tot_inc) if n_tot_inc else 0.0,
            "abs_median_con": _median_blk("con", abs_attr),
            "abs_median_inc": _median_blk("first", abs_attr),
            "rel_median_con": _median_blk("con", rel_attr),
            "rel_median_inc": _median_blk("first", rel_attr),
        }

    return ChannelSummary(
        channel=channel, band=band,
        surviving=len(surviving), excluded=len(excluded),
        surviving_by_condition=surviving_by_cond,
        excluded_by_condition=excluded_by_cond,
        exclusion_pct_con=pct_con, exclusion_pct_inc=pct_inc,
        balance_flag=balance_flag,
        abs_median_con=_median("con",   abs_attr),
        abs_median_inc=_median("first", abs_attr),
        rel_median_con=_median("con",   rel_attr),
        rel_median_inc=_median("first", rel_attr),
        by_block=by_block,
    )


# ============================================================
#  Epoch reconstruction (for the per-epoch review viewer)
# ============================================================
# The pipeline discards per-trial time-series after computing metrics. For the
# epoch viewer we reconstruct a single trial's waveform on demand from the
# retained continuous signal (server keeps the ParsedEEG in memory), using the
# EXACT same high-pass + epoching MNE performs in run_pipeline, so the displayed
# trace is the signal that was actually analysed (verified: fz_ptp reproduces to
# 0.0 µV difference). Nothing is precomputed or stored.

def _highpassed_continuous(parsed: ParsedEEG):
    """Return (raw_hp, fs): the 1 Hz high-passed continuous MNE Raw, matching
    Stage 3 of run_pipeline (NaN-zeroed before filtering)."""
    fs = parsed.sampling_rate
    raw_data = np.stack([parsed.fz, parsed.c3]) * 1e-6           # V
    raw_data = np.where(~np.isfinite(raw_data), 0.0, raw_data)   # NaN-safe (S002)
    info = mne.create_info(["Fz-Pz", "C3-C4"], fs, "eeg")
    raw = mne.io.RawArray(raw_data, info, verbose=False)
    raw_hp = raw.copy().filter(l_freq=HP_HZ, h_freq=None, verbose=False)
    return raw_hp, fs


def reconstruct_epoch(
    parsed: ParsedEEG,
    onset_sample_concat: int,
    cond: str,
    want_scalogram: bool = False,
) -> dict:
    """Reconstruct one trial's epoch for display.

    Returns a dict with:
      * ``times_ms``     — epoch time axis in ms from stimulus onset (incl. the
                           ±PAD_S padding, so −300 .. +800 ms).
      * ``fz`` / ``c3``  — high-passed epoch traces (µV), same length as times_ms.
      * ``fs``           — sampling rate (Hz).
      * ``win_ms`` / ``reach_ms`` / ``pad_ms`` — window/buffer/pad boundaries (ms)
                           so the frontend can shade the analysis vs buffer bands.
      * ``fz_slow``      — the 1–7 Hz slow-band Fz trace over the epoch (the exact
                           signal the adaptive blink detector thresholds), so the
                           viewer can point at the feature that triggered a blink
                           exclusion.
      * ``scalogram``    — optional {freqs, fz_power, c3_power} time×frequency
                           |CWT|² over the epoch when ``want_scalogram``.
    """
    raw_hp, fs = _highpassed_continuous(parsed)
    code = 1 if cond == "con" else 2
    label = "con" if cond == "con" else "first"
    events = np.array([[int(onset_sample_concat), 0, code]], dtype=int)
    epochs = mne.Epochs(
        raw_hp, events, {label: code},
        tmin=-PAD_S, tmax=WIN_S + PAD_S,
        baseline=None, preload=True, verbose=False,
    )
    if len(epochs) == 0:
        raise ValueError("epoch could not be extracted at that onset sample")
    d = epochs.get_data(copy=True)[0] * 1e6   # (2, n_samp) µV
    fz = d[0]
    c3 = d[1]
    times_ms = (epochs.times * 1000.0)

    # Slow-band (1–7 Hz) Fz over the same epoch — the blink-detector's input.
    raw_slow = raw_hp.copy().filter(l_freq=None, h_freq=BLINK_SLOW_HZ, verbose=False)
    slow_ep = mne.Epochs(
        raw_slow, events, {label: code},
        tmin=-PAD_S, tmax=WIN_S + PAD_S,
        baseline=None, preload=True, verbose=False,
    )
    fz_slow = slow_ep.get_data(copy=True)[0][0] * 1e6

    out = {
        "times_ms": times_ms.tolist(),
        "fz": fz.tolist(),
        "c3": c3.tolist(),
        "fz_slow": fz_slow.tolist(),
        "fs": float(fs),
        "win_ms": WIN_S * 1000.0,
        "reach_ms": REACH_S * 1000.0,
        "pad_ms": PAD_S * 1000.0,
    }

    if want_scalogram:
        FREQS = np.arange(TOTAL_BAND[0], TOTAL_BAND[1] + 1e-6, FREQ_STEP)
        # theta-cycle CWT for Fz, beta-cycle CWT for C3 (matches the pipeline's
        # per-channel wavelet choice).
        fz_pow = _cwt_power(fz, fs, THETA_CYC, FREQS)
        c3_pow = _cwt_power(c3, fs, BETA_CYC, FREQS)
        out["scalogram"] = {
            "freqs": FREQS.tolist(),
            "fz_power": [row.tolist() for row in fz_pow],
            "c3_power": [row.tolist() for row in c3_pow],
            "theta_band": list(THETA_BAND),
            "beta_band": list(BETA_BAND),
        }
    return out


# ============================================================
#  Whole-recording overview (for the "Raw EEG Data" viewer)
# ============================================================
# A downsampled view of the ENTIRE continuous recording so an analyst can see,
# in one scrollable trace, which parts of the signal the pipeline actually used
# (the epoch windows around each paired trial) versus everything it ignored, and
# the accept/reject verdict of each analysed window. Like the epoch viewer this
# is reconstructed on demand from the retained continuous signal; nothing is
# precomputed or stored. The same 1 Hz high-pass as the analysis is applied so
# the trace shows the signal that was analysed (and so slow DC drift doesn't
# swamp a whole-recording y-axis). The signal is decimated to a target point
# budget because Plotly/scattergl chokes on hundreds of thousands of points.

def recording_overview(parsed: ParsedEEG, max_points: int = 12000) -> dict:
    """Return a decimated whole-recording trace + sample-rate/time metadata.

    Only the continuous signal + timeline is produced here; per-trial markers
    and epoch-window spans are assembled by the server (which owns the analysed
    TrialResult verdicts and the concat onset samples). Returns a dict with:
      * ``fs``          — original sampling rate (Hz).
      * ``decimation``  — stride used (1 = none).
      * ``n_samples``   — original continuous length.
      * ``duration_s``  — recording length in seconds.
      * ``times_s``     — decimated time axis (s from recording start).
      * ``fz`` / ``c3`` — decimated high-passed traces (µV).
    """
    raw_hp, fs = _highpassed_continuous(parsed)
    data = raw_hp.get_data() * 1e6            # (2, n_samples) µV
    fz_full, c3_full = data[0], data[1]
    n_total = fz_full.shape[0]

    decimation = max(1, int(np.ceil(n_total / max(1, max_points))))
    idx = np.arange(0, n_total, decimation)
    times_s = idx / fs

    return {
        "fs": float(fs),
        "decimation": int(decimation),
        "n_samples": int(n_total),
        "duration_s": float(n_total / fs) if fs else 0.0,
        "times_s": [round(float(t), 3) for t in times_s],
        "fz": [round(float(v), 2) for v in fz_full[idx]],
        "c3": [round(float(v), 2) for v in c3_full[idx]],
    }

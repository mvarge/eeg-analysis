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

mne.set_log_level("ERROR")


# ============================================================
#  Frozen configuration - identical for every recording
# ============================================================
HP_HZ         = 1.0             # high-pass cutoff (Hz)
WIN_S         = 0.500           # analysis window (s)
PAD_S         = 0.300           # epoch padding each side (>= wavelet reach)
REACH_S       = 0.179           # wavelet reach (1.5 * 3 cycles / (2π * 4 Hz))

# Exclusion thresholds (from S1P002 validation)
BLINK_UV      = 80              # Fz peak-to-peak (µV) over window+reach
EMG_BETA      = 150_000         # C3 beta power (µV²) in window
BURST_Z       = 3.6             # C3 max |z| in window .... AND ....
BURST_IMPACT  = 15              # ... % change in beta when spike removed
COINC_Z       = 3.0             # simultaneous z on BOTH channels

# Wavelet (Morlet, complex)
THETA_BAND    = (4.0, 8.0)      # Hz @ Fz-Pz
BETA_BAND     = (13.0, 30.0)    # Hz @ C3-C4
THETA_CYC     = 3.0             # Morlet cycles for theta
BETA_CYC      = 7.0             # Morlet cycles for beta
TOTAL_BAND    = (1.0, 40.0)     # denominator for relative power
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
    info = mne.create_info(["Fz-Pz", "C3-C4"], fs, "eeg")
    raw = mne.io.RawArray(raw_data, info, verbose=False)
    raw_hp = raw.copy().filter(l_freq=HP_HZ, h_freq=None, verbose=False)

    FZ = raw_hp.get_data(picks="Fz-Pz")[0] * 1e6   # back to µV
    C3 = raw_hp.get_data(picks="C3-C4")[0] * 1e6
    T_axis = raw_hp.times

    # ---- STAGE 4: build events + Epochs (window + padding) ----------------
    events = np.column_stack([
        np.round(np.array([t.onset for t in trials_meta]) * fs).astype(int),
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
    # Blink = Fz peak-to-peak > BLINK_UV over window+reach (post-key blinks bleed back)
    span = epochs.copy().crop(0, WIN_S + REACH_S)
    fz_only = span.copy().pick(["Fz-Pz"])
    fz_only.drop_bad(reject=dict(eeg=BLINK_UV * 1e-6), verbose=False)
    blink_flags = [len(dl) > 0 for dl in fz_only.drop_log]

    # Also read Fz peak-to-peak (µV) over window+reach for reporting
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
        c3_exclude = (beta_val > EMG_BETA) or (maxz > BURST_Z and impact > BURST_IMPACT) or (coinc > COINC_Z)

        reasons = []
        if blink:
            reasons.append(f"blink (Fz ptp>{BLINK_UV}µV)")
        if coinc > COINC_Z:
            reasons.append(f"coincident transient (z={coinc:.1f})")
        if beta_val > EMG_BETA:
            reasons.append(f"gross EMG (beta={beta_val:,.0f})")
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
    theta_summary = _channel_summary(trial_results, "Fz-Pz", "theta", "fz_exclude")
    beta_summary  = _channel_summary(trial_results, "C3-C4", "beta",  "c3_exclude")

    # ---- Averaged wavelet spectra (surviving trials only, for plotting) ---
    def _mean_spec(spec: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if not mask.any():
            return np.zeros_like(FREQS)
        return spec[mask].mean(axis=0)

    keep_theta = np.array([not t.fz_exclude for t in trial_results])
    keep_beta  = np.array([not t.c3_exclude for t in trial_results])
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
    )


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
    )

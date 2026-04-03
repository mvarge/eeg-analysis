"""
EEG processing pipeline.

Takes parsed EEG data and performs:
1. Bandpass filtering (1-40 Hz) using MNE
2. Epoch extraction (-200ms to +1000ms) using MNE
3. Baseline correction (-200ms to 0ms)
4. FFT power analysis using SciPy
5. Band power extraction (theta 4-8 Hz, beta 13-30 Hz)
"""

import numpy as np
import mne
from scipy.fft import rfft, rfftfreq
from dataclasses import dataclass
from parser import ParsedEEG, Marker


@dataclass
class EpochData:
    """A single epoch of EEG data."""
    trial_index: int
    condition: str  # 'congruent' or 'incongruent'
    block: int      # 1 or 2
    ch1_data: np.ndarray  # filtered, baseline-corrected
    ch2_data: np.ndarray
    times: np.ndarray  # time axis in seconds relative to stimulus


@dataclass
class PowerResult:
    """Power spectrum result for one epoch."""
    trial_index: int
    condition: str
    block: int      # 1 or 2
    ch1_theta_power: float  # mean power 4-8 Hz
    ch2_beta_power: float   # mean power 13-30 Hz
    ch1_freqs: np.ndarray
    ch1_power_spectrum: np.ndarray
    ch2_freqs: np.ndarray
    ch2_power_spectrum: np.ndarray


@dataclass
class PipelineResult:
    """Complete pipeline output."""
    filename: str
    recording_date: str
    sampling_rate: float
    channel_names: list
    epochs: list           # List[EpochData]
    power_results: list    # List[PowerResult]
    # Summary values
    theta_power_congruent: float
    theta_power_incongruent: float
    beta_power_congruent: float
    beta_power_incongruent: float
    n_epochs_congruent: int
    n_epochs_incongruent: int
    # For visualisation: averaged waveforms
    avg_ch1_congruent: np.ndarray
    avg_ch1_incongruent: np.ndarray
    avg_ch2_congruent: np.ndarray
    avg_ch2_incongruent: np.ndarray
    epoch_times: np.ndarray
    # Averaged power spectra
    avg_spectrum_ch1_con: np.ndarray
    avg_spectrum_ch1_inc: np.ndarray
    avg_spectrum_ch2_con: np.ndarray
    avg_spectrum_ch2_inc: np.ndarray
    spectrum_freqs: np.ndarray


def bandpass_filter(data: np.ndarray, sfreq: float, l_freq: float = 1.0, h_freq: float = 40.0) -> np.ndarray:
    """Apply bandpass filter using MNE."""
    # MNE filter expects (n_channels, n_times) shape
    data_2d = data.reshape(1, -1)
    filtered = mne.filter.filter_data(data_2d, sfreq, l_freq, h_freq, verbose=False)
    return filtered.flatten()


def extract_epochs(parsed: ParsedEEG, pre_ms: float = 200.0, post_ms: float = 1000.0) -> list:
    """
    Extract and filter epochs from parsed EEG data.

    Args:
        parsed: ParsedEEG from the parser
        pre_ms: Pre-stimulus window in ms (for baseline)
        post_ms: Post-stimulus window in ms

    Returns:
        List of EpochData
    """
    sfreq = parsed.sampling_rate
    pre_samples = int(pre_ms / 1000.0 * sfreq)
    post_samples = int(post_ms / 1000.0 * sfreq)
    total_samples = pre_samples + post_samples

    # Filter entire channels first (more efficient than per-epoch)
    ch1_filtered = bandpass_filter(parsed.channel1, sfreq)
    ch2_filtered = bandpass_filter(parsed.channel2, sfreq)

    # Time axis for epochs (in seconds, relative to stimulus)
    times = np.arange(-pre_samples, post_samples) / sfreq

    epochs = []
    for i, marker in enumerate(parsed.trial_markers):
        onset = marker.sample_index
        start = onset - pre_samples
        end = onset + post_samples

        # Bounds check
        if start < 0 or end > len(ch1_filtered):
            continue

        ch1_epoch = ch1_filtered[start:end].copy()
        ch2_epoch = ch2_filtered[start:end].copy()

        # Baseline correction: subtract mean of pre-stimulus period
        ch1_baseline = np.mean(ch1_epoch[:pre_samples])
        ch2_baseline = np.mean(ch2_epoch[:pre_samples])
        ch1_epoch -= ch1_baseline
        ch2_epoch -= ch2_baseline

        epochs.append(EpochData(
            trial_index=i,
            condition=marker.condition,
            block=marker.block,
            ch1_data=ch1_epoch,
            ch2_data=ch2_epoch,
            times=times,
        ))

    return epochs


def compute_power(epoch: EpochData, sfreq: float) -> PowerResult:
    """
    Compute FFT power spectrum for an epoch.

    Returns PowerResult with theta (4-8 Hz) and beta (13-30 Hz) band powers.
    """
    n = len(epoch.ch1_data)
    freqs = rfftfreq(n, d=1.0 / sfreq)

    # FFT for both channels
    ch1_fft = rfft(epoch.ch1_data)
    ch2_fft = rfft(epoch.ch2_data)

    # Power = squared magnitude, normalised
    ch1_power = (2.0 / n) * np.abs(ch1_fft) ** 2
    ch2_power = (2.0 / n) * np.abs(ch2_fft) ** 2

    # Extract band powers
    theta_mask = (freqs >= 4) & (freqs <= 8)
    beta_mask = (freqs >= 13) & (freqs <= 30)

    ch1_theta = np.mean(ch1_power[theta_mask]) if np.any(theta_mask) else 0.0
    ch2_beta = np.mean(ch2_power[beta_mask]) if np.any(beta_mask) else 0.0

    return PowerResult(
        trial_index=epoch.trial_index,
        condition=epoch.condition,
        block=epoch.block,
        ch1_theta_power=ch1_theta,
        ch2_beta_power=ch2_beta,
        ch1_freqs=freqs,
        ch1_power_spectrum=ch1_power,
        ch2_freqs=freqs,
        ch2_power_spectrum=ch2_power,
    )


def run_pipeline(parsed: ParsedEEG) -> PipelineResult:
    """
    Run the full EEG analysis pipeline.

    Args:
        parsed: ParsedEEG from the parser

    Returns:
        PipelineResult with all analysis outputs
    """
    sfreq = parsed.sampling_rate

    # Step 1: Extract epochs (includes filtering and baseline correction)
    epochs = extract_epochs(parsed)

    # Step 2: Compute power for each epoch
    power_results = [compute_power(ep, sfreq) for ep in epochs]

    # Step 3: Separate by condition
    con_epochs = [e for e in epochs if e.condition == "congruent"]
    inc_epochs = [e for e in epochs if e.condition == "incongruent"]
    con_powers = [p for p in power_results if p.condition == "congruent"]
    inc_powers = [p for p in power_results if p.condition == "incongruent"]

    # Step 4: Compute summary statistics
    theta_con = np.mean([p.ch1_theta_power for p in con_powers]) if con_powers else 0.0
    theta_inc = np.mean([p.ch1_theta_power for p in inc_powers]) if inc_powers else 0.0
    beta_con = np.mean([p.ch2_beta_power for p in con_powers]) if con_powers else 0.0
    beta_inc = np.mean([p.ch2_beta_power for p in inc_powers]) if inc_powers else 0.0

    # Step 5: Compute averaged waveforms for visualisation
    epoch_times = epochs[0].times if epochs else np.array([])

    avg_ch1_con = np.mean([e.ch1_data for e in con_epochs], axis=0) if con_epochs else np.array([])
    avg_ch1_inc = np.mean([e.ch1_data for e in inc_epochs], axis=0) if inc_epochs else np.array([])
    avg_ch2_con = np.mean([e.ch2_data for e in con_epochs], axis=0) if con_epochs else np.array([])
    avg_ch2_inc = np.mean([e.ch2_data for e in inc_epochs], axis=0) if inc_epochs else np.array([])

    # Step 6: Compute averaged power spectra
    spectrum_freqs = power_results[0].ch1_freqs if power_results else np.array([])
    avg_spec_ch1_con = np.mean([p.ch1_power_spectrum for p in con_powers], axis=0) if con_powers else np.array([])
    avg_spec_ch1_inc = np.mean([p.ch1_power_spectrum for p in inc_powers], axis=0) if inc_powers else np.array([])
    avg_spec_ch2_con = np.mean([p.ch2_power_spectrum for p in con_powers], axis=0) if con_powers else np.array([])
    avg_spec_ch2_inc = np.mean([p.ch2_power_spectrum for p in inc_powers], axis=0) if inc_powers else np.array([])

    return PipelineResult(
        filename=parsed.filename,
        recording_date=parsed.recording_date,
        sampling_rate=sfreq,
        channel_names=parsed.channel_names,
        epochs=epochs,
        power_results=power_results,
        theta_power_congruent=float(theta_con),
        theta_power_incongruent=float(theta_inc),
        beta_power_congruent=float(beta_con),
        beta_power_incongruent=float(beta_inc),
        n_epochs_congruent=len(con_epochs),
        n_epochs_incongruent=len(inc_epochs),
        avg_ch1_congruent=avg_ch1_con,
        avg_ch1_incongruent=avg_ch1_inc,
        avg_ch2_congruent=avg_ch2_con,
        avg_ch2_incongruent=avg_ch2_inc,
        epoch_times=epoch_times,
        avg_spectrum_ch1_con=avg_spec_ch1_con,
        avg_spectrum_ch1_inc=avg_spec_ch1_inc,
        avg_spectrum_ch2_con=avg_spec_ch2_con,
        avg_spectrum_ch2_inc=avg_spec_ch2_inc,
        spectrum_freqs=spectrum_freqs,
    )

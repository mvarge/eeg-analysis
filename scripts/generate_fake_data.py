"""
Generate fake LabChart EEG data files for testing.

Creates files that mimic the S1P002.txt format with:
- Realistic EEG-like signals (alpha/theta/beta oscillations + noise)
- Proper header (Interval, ExcelDateTime, ChannelTitle, Range)
- 160 trials (80 congruent + 80 incongruent) in 2 blocks
- Markers in the format: #1 con/first/key/second #2 con/first/key/second
- Slightly different power profiles per subject
"""

import numpy as np
import os
import sys


def generate_eeg_signal(n_samples, fs, theta_power=1.0, beta_power=0.5, alpha_power=0.8):
    """Generate a realistic-ish EEG signal with controlled band powers."""
    t = np.arange(n_samples) / fs
    signal = np.zeros(n_samples)
    
    # Theta (4-8 Hz)
    for freq in np.linspace(4, 8, 3):
        phase = np.random.uniform(0, 2 * np.pi)
        signal += theta_power * np.random.uniform(0.5, 1.5) * np.sin(2 * np.pi * freq * t + phase)
    
    # Alpha (8-13 Hz)
    for freq in np.linspace(8, 13, 3):
        phase = np.random.uniform(0, 2 * np.pi)
        signal += alpha_power * np.random.uniform(0.3, 1.0) * np.sin(2 * np.pi * freq * t + phase)
    
    # Beta (13-30 Hz)
    for freq in np.linspace(13, 30, 5):
        phase = np.random.uniform(0, 2 * np.pi)
        signal += beta_power * np.random.uniform(0.2, 0.8) * np.sin(2 * np.pi * freq * t + phase)
    
    # Pink noise (1/f)
    white = np.random.randn(n_samples)
    fft = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n_samples, 1/fs)
    freqs[0] = 1  # avoid division by zero
    fft *= 1.0 / np.sqrt(freqs)
    pink = np.fft.irfft(fft, n_samples) * 3
    signal += pink
    
    # Scale to realistic µV range
    signal *= 8
    
    return signal


def generate_subject_file(filepath, subject_id, seed=None):
    """Generate a complete fake LabChart export file."""
    if seed is not None:
        np.random.seed(seed)
    
    fs = 400.0  # sampling rate
    interval = 1.0 / fs
    
    # Subject-specific power profiles (slight variation)
    ch1_theta = np.random.uniform(0.8, 1.5)
    ch1_beta = np.random.uniform(0.3, 0.7)
    ch2_theta = np.random.uniform(0.4, 0.8)
    ch2_beta = np.random.uniform(0.6, 1.2)
    
    # Incongruent has slightly higher theta (the expected Flanker effect)
    theta_inc_boost = np.random.uniform(1.05, 1.25)
    beta_inc_boost = np.random.uniform(0.90, 1.10)
    
    # Recording parameters
    n_trials_per_block = 80
    n_blocks = 2
    total_trials = n_trials_per_block * n_blocks
    
    # Pre-stimulus baseline: ~600s of resting before first marker
    pre_stim_samples = int(600 * fs)
    
    # Each trial: stimulus → ~0.4-0.7s → key press → ~0.3-0.5s gap → next trial
    trial_duration_range = (0.7, 1.0)  # total time per trial in seconds
    response_time_range = (0.35, 0.65)
    
    # Generate condition order (randomized per block)
    conditions_block1 = ['con'] * 40 + ['first'] * 40
    np.random.shuffle(conditions_block1)
    conditions_block2 = ['con'] * 40 + ['first'] * 40
    np.random.shuffle(conditions_block2)
    
    # Build the timeline: collect (sample_index, marker_text) pairs
    markers = []
    current_time = 626.0  # Start time similar to real data
    current_sample = int(current_time * fs)
    
    # Block 1 start marker
    markers.append((current_sample, current_time, 'first'))  # block start
    current_time += np.random.uniform(0.5, 0.8)
    current_sample = int(current_time * fs)
    
    # Block 1 trials
    for cond in conditions_block1:
        markers.append((current_sample, current_time, cond))
        rt = np.random.uniform(*response_time_range)
        current_time += rt
        current_sample = int(current_time * fs)
        markers.append((current_sample, current_time, 'key'))
        gap = np.random.uniform(0.5, 0.8)
        current_time += gap
        current_sample = int(current_time * fs)
    
    # Block 1→2 boundary: 'first' (block 2 start) then 'second'
    markers.append((current_sample, current_time, 'first'))  # block 2 start
    current_time += 0.3
    current_sample = int(current_time * fs)
    markers.append((current_sample, current_time, 'second'))  # block boundary
    current_time += np.random.uniform(0.5, 0.8)
    current_sample = int(current_time * fs)
    
    # Block 2 trials
    for cond in conditions_block2:
        markers.append((current_sample, current_time, cond))
        rt = np.random.uniform(*response_time_range)
        current_time += rt
        current_sample = int(current_time * fs)
        markers.append((current_sample, current_time, 'key'))
        gap = np.random.uniform(0.5, 0.8)
        current_time += gap
        current_sample = int(current_time * fs)
    
    # End marker
    markers.append((current_sample, current_time, 'second'))  # end
    current_time += 2.0
    
    # Total recording length
    total_samples = int(current_time * fs) + int(5 * fs)  # 5s padding at end
    
    # Generate continuous EEG for both channels
    ch1 = generate_eeg_signal(total_samples, fs, theta_power=ch1_theta, beta_power=ch1_beta)
    ch2 = generate_eeg_signal(total_samples, fs, theta_power=ch2_theta, beta_power=ch2_beta)
    
    # Add condition-specific power modulation around trial markers
    # Incongruent trials get a theta boost in the post-stimulus window
    for sample_idx, t, cond in markers:
        if cond in ('con', 'first'):
            # Post-stimulus window: 200-600ms
            start = sample_idx + int(0.2 * fs)
            end = min(sample_idx + int(0.6 * fs), total_samples)
            n = end - start
            if n > 0 and cond == 'first':
                # Boost theta for incongruent
                boost_signal = np.zeros(n)
                for freq in np.linspace(4, 8, 3):
                    phase = np.random.uniform(0, 2 * np.pi)
                    tt = np.arange(n) / fs
                    boost_signal += 2.0 * theta_inc_boost * np.sin(2 * np.pi * freq * tt + phase)
                # Apply with a Hanning window for smooth onset
                window = np.hanning(n)
                ch1[start:end] += boost_signal * window
    
    # Build marker lookup by sample index
    marker_dict = {}
    for sample_idx, t, cond in markers:
        marker_dict[sample_idx] = cond
    
    # Write file
    excel_dt = 46113.0 + np.random.uniform(0, 10)  # Fake Excel date
    date_str = f"0{np.random.randint(1,4)}/04/2026 {np.random.randint(10,23)}:{np.random.randint(10,59)}:{np.random.randint(10,59)}.{np.random.randint(10000,99999)}"
    
    with open(filepath, 'w', encoding='latin-1') as f:
        f.write(f"Interval=\t{interval} s\n")
        f.write(f"ExcelDateTime=\t{excel_dt:.16e}\t{date_str}\n")
        f.write("TimeFormat=\tStartOfBlock\n")
        f.write("DateFormat=\t\n")
        f.write("ChannelTitle=\tEEG Fz-Pz \tEEG C3-C4\n")
        f.write("Range=\t200.0 µV\t200.0 µV\n")
        
        for i in range(total_samples):
            t = i * interval
            line = f"{t:.4f}\t{ch1[i]:.5f}\t{ch2[i]:.5f}"
            if i in marker_dict:
                m = marker_dict[i]
                line += f"\t#1 {m} #2 {m}"
            f.write(line + "\n")
    
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    print(f"  Created {filepath} ({size_mb:.1f} MB, {total_samples} samples)")


def main():
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)
    
    # Generate 3 fake subjects
    subjects = [
        ("S1P003.txt", 42),
        ("S1P004.txt", 123),
        ("S1P005.txt", 789),
    ]
    
    print("Generating fake EEG data files...")
    for filename, seed in subjects:
        filepath = os.path.join(data_dir, filename)
        generate_subject_file(filepath, filename, seed=seed)
    
    print(f"\nDone! Generated {len(subjects)} fake subject files in {data_dir}")


if __name__ == "__main__":
    main()

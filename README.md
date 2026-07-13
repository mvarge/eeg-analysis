# EEG Flanker Analysis Tool

A web-based tool that processes EEG recordings from an **Eriksen Flanker Task** experiment. Upload your LabChart `.txt` export files and the tool automatically parses the data, computes **theta** and **beta** spectral power for congruent vs incongruent trials (with a strict artifact-rejection pipeline), displays interactive charts, and exports SPSS-ready CSV files.

Everything runs locally on your computer — your data never leaves your machine.

![Made with love](https://img.shields.io/badge/made%20with-💛-yellow)
![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## What the Tool Does

This tool implements a 10-stage pipeline built around **complex Morlet wavelet** spectral power in the reaction-time window of each trial:

1. **Parse LabChart 8 text exports** — reads Latin-1 encoded, tab-delimited files at 400 Hz with two channels: **Fz-Pz** and **C3-C4**. Extracts all embedded comment markers.
2. **Pair markers into trials** — walks through `con` (congruent) and `first` (incongruent) markers, pairs each with the next `key` (button press). Detects the >30 s inter-block pause and assigns block 1 vs block 2 automatically.
3. **1 Hz high-pass filter** — removes slow drift via MNE-Python.
4. **Epoch each trial** — 0 to +500 ms post-stimulus window with a ±300 ms pad, giving a 1.1 s segment for wavelet analysis without edge artifacts.
5. **Per-trial quality metrics** on each epoch:
   - Peak-to-peak amplitude (blink probe on Fz-Pz)
   - Theta-band FFT power (Fz-Pz)
   - Beta-band FFT power (C3-C4) — gross EMG probe
   - Max |z-score| across the epoch
   - Sample-level burst impact score
   - Between-channel coincidence z-score
6. **Automatic artifact rejection** per channel (independent trial sets for Fz-Pz and C3-C4):
   - Blink (`fz_ptp` > 80 µV)
   - Gross EMG (`beta_fft` > 150 000)
   - Amplitude burst (`maxz` ≥ 3.6 AND `impact` ≥ 15)
   - Between-channel coincidence (`coinc` ≥ 3.0)
7. **Complex Morlet wavelet spectral power** — 1–40 Hz in 0.5 Hz steps. Theta uses 3-cycle wavelets (4–8 Hz), beta uses 7-cycle wavelets (13–30 Hz). Reports both **absolute** power and **relative** power (band ÷ 1–40 Hz total, expressed as a fraction).
8. **Per-condition medians** on the surviving trials — separately for congruent and incongruent.
9. **Balance check** — flags a channel if per-condition exclusion rates differ by more than 10 %-points.
10. **Export SPSS-ready CSVs** with per-trial rows, per-participant summary rows, and combined multi-participant files.

The primary metric is **relative power** (fraction of total 1–40 Hz power falling in each band), which is more robust to individual differences in overall amplitude than absolute power.

---

## The Three Screens

### Screen 1 — Upload

The landing page. A drop zone accepts one or more LabChart `.txt` files (or click to browse). Each file is processed as it lands.

- **One file** → jumps to the individual analysis screen
- **Multiple files** → jumps to the group comparison screen
- You can also add participants one at a time

### Screen 2 — Individual Analysis

Shows the full analysis for a single participant.

**Info bar:** filename, recording date, sampling rate, block count, trial count broken down by condition.

**Two power cards** — one for theta (Fz-Pz), one for beta (C3-C4):

- **Relative power** (primary metric): median for con vs inc, as a fraction of total 1–40 Hz power
- **Absolute power** (secondary): median for con vs inc, in µV²
- **Surviving trials** after artifact rejection, broken down by condition
- **Balance flag** — warns if the exclusion rate between conditions differs by more than 10 %

**Charts:**

- **Wavelet Spectrum — Fz-Pz** — mean 1–40 Hz power spectrum (log scale) for surviving trials, con vs inc lines, theta band shaded
- **Wavelet Spectrum — C3-C4** — same, with beta band shaded
- **Per-Trial Scatter** — every trial's relative theta and relative beta power, colored by condition, so you can see the spread behind the medians

**Exclusion breakdown** — a table showing how many trials each rejection rule removed per channel and condition, and a list of every excluded trial with the specific rules that fired.

**Action buttons:**

- **Trial CSV** — one row per trial with all 6 quality metrics, exclusion flags, and absolute/relative power
- **Exclusions CSV** — one row per excluded trial with the reason
- **Summary CSV** — one row for this participant with all medians and survival counts
- **Add Another Participant** / **Compare Participants** / **Start Over**

### Screen 3 — Group Comparison

Shows all uploaded participants side by side.

**Summary table** — one row per participant: recording date, theta/beta relative medians (con and inc), survival counts, balance flags.

**Comparison charts:**

- **Theta Relative Power by Participant** — grouped bars, con vs inc per participant
- **Beta Relative Power by Participant** — same for beta
- **Exclusion Rate by Participant** — theta and beta exclusion percentages, so you can spot noisy recordings
- **Congruency Effect by Participant** — bar chart of (inc − con) theta relative power, the Flanker effect

**Action buttons:**

- **Group Summary CSV** — one row per participant
- **Group Trial CSV** — every trial from every participant in one flat file (ideal for SPSS repeated-measures)
- **Add More Participants** / **← Back to Individual**

### Navigation

Click the header/logo to return to the upload screen.

---

## How to Install & Run

### What You Need First

- **Python 3.10 or newer**
  - **Mac / Linux:** open Terminal and type `python3 --version`
  - **Windows:** open Command Prompt (or PowerShell) and type `python --version`
  - If missing: install the latest from [python.org/downloads](https://www.python.org/downloads/). **On Windows, tick "Add Python to PATH" during installation.**
- **A web browser** (any modern one).

### Step-by-Step

**1. Download**

Green **Code** button on GitHub → **Download ZIP** → unzip somewhere convenient.

Or via terminal:
```bash
git clone https://github.com/mvarge/eeg-analysis.git
cd eeg-analysis
```

**2. Run**

#### macOS / Linux

```bash
cd ~/Desktop/eeg-analysis    # or wherever you unzipped it
chmod +x run.sh               # first time only
./run.sh
```

#### Windows

Double-click `run.bat` in File Explorer.

Or from Command Prompt / PowerShell:
```cmd
cd %USERPROFILE%\Desktop\eeg-analysis
run.bat
```

PowerShell alternative:
```powershell
.\run.ps1
```
(If PowerShell blocks the script, run once: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`.)

The first run creates a Python virtual environment and installs dependencies (MNE, SciPy, PyWavelets, FastAPI…) — about 30 seconds.

**3. Open**

[http://localhost:8000](http://localhost:8000)

**4. Upload**

Drag `.txt` files onto the drop zone. One → individual view; several → group view.

**5. Download**

Every screen exposes CSV export buttons.

**6. Stop**

Ctrl + C in the terminal window where the server is running.

---

## Your Data Files

The tool accepts **LabChart 8 text exports** (`.txt`). Export from LabChart via **File → Export → Text**, keeping the **Comments** checkbox ticked so trial markers are included.

Each file should contain:

- A header with `Interval=`, `ExcelDateTime=`, `ChannelTitle=`, `Range=`
- Two data columns: **Fz-Pz** and **C3-C4** at 400 Hz
- Comment markers (`#1 con`, `#1 first`, `#1 key`, occasional `#1 second` for block boundaries)

Blocks are detected automatically from the >30 s pause between them.

**Your data stays private** — everything runs on your computer.

### Test Data

Generate fake participant files without touching real data:

**macOS / Linux:**
```bash
.venv/bin/python scripts/generate_fake_data.py
```

**Windows:**
```cmd
.venv\Scripts\python scripts\generate_fake_data.py
```

This drops three files (`S1P003.txt`–`S1P005.txt`) into `data/`.

---

## What's in the Output CSVs

### Trial-level CSV (one row per trial)

| Column | Meaning |
|--------|---------|
| `recording` | Participant identifier |
| `trial` | 1-based trial number |
| `block`, `btrial` | Block number and within-block trial number |
| `cond` | `con` or `first` (incongruent) |
| `onset`, `key`, `rt_ms` | Stimulus onset time, key-press time, reaction time |
| `fz_ptp` | Fz-Pz peak-to-peak (µV) |
| `theta_fft` | Fz-Pz theta FFT power (band-limited) |
| `beta_fft` | C3-C4 beta FFT power (gross EMG probe) |
| `maxz` | Max absolute z-score across the epoch |
| `impact` | Burst impact score |
| `coinc` | Between-channel coincidence z-score |
| `blink`, `fz_exclude`, `c3_exclude` | Boolean rejection flags |
| `reason` | Human-readable exclusion reasons |
| `theta_abs`, `theta_rel` | Absolute and relative theta wavelet power (Fz-Pz) |
| `beta_abs`, `beta_rel` | Absolute and relative beta wavelet power (C3-C4) |

### Summary CSV (one row per participant)

| Column | Meaning |
|--------|---------|
| `recording`, `recording_date` | Participant and timestamp |
| `theta_surviving`, `theta_excluded` | Trial counts on Fz-Pz |
| `theta_rel_median_con`, `theta_rel_median_inc` | Relative theta medians |
| `theta_abs_median_con`, `theta_abs_median_inc` | Absolute theta medians |
| `beta_surviving`, `beta_excluded` | Trial counts on C3-C4 |
| `beta_rel_median_con`, `beta_rel_median_inc` | Relative beta medians |
| `beta_abs_median_con`, `beta_abs_median_inc` | Absolute beta medians |
| `theta_exclusion_pct_con`, `theta_exclusion_pct_inc` | % excluded per condition (Fz-Pz) |
| `beta_exclusion_pct_con`, `beta_exclusion_pct_inc` | % excluded per condition (C3-C4) |
| `theta_balance_flag`, `beta_balance_flag` | `True` if con-vs-inc exclusion rates differ by more than 10 %-pts |

### Exclusions CSV (one row per rejected trial)

| Column | Meaning |
|--------|---------|
| `recording`, `trial`, `block`, `cond` | Trial identity |
| `channel` | `Fz-Pz` or `C3-C4` |
| `reason` | Which rejection rules fired (`blink`, `gross EMG`, `burst`, `coincidence`) |

> The trial-level CSV is best for SPSS — a single flat table you can sort/filter by participant, block, and condition. The exclusion flags let you replicate or override the automatic rejection.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `python3: command not found` (Mac/Linux) | Install Python from [python.org/downloads](https://www.python.org/downloads/) |
| `'python' is not recognized...` (Windows) | Reinstall Python and tick **Add Python to PATH**, or install from the Microsoft Store |
| `permission denied: ./run.sh` (Mac/Linux) | Run `chmod +x run.sh` first |
| `run.ps1 cannot be loaded because running scripts is disabled` (Windows) | Run once: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` — or just use `run.bat` |
| `no matching distribution for scipy` | Delete the `.venv` folder and run the script again |
| Page won't load | Make sure the terminal window is still running |
| Upload error | Confirm the file is a LabChart `.txt` export with the Comments box ticked |
| All my trials get excluded | Check the exclusion breakdown — a very noisy recording, incorrect gain, or wrong channel montage can trip the automatic rules |

---

## Technical Details

- **Parser**: LabChart 8 text (Latin-1, tab-delimited), 400 Hz, channels Fz-Pz and C3-C4. Block boundaries detected from >30 s marker gap.
- **Trial pairing**: each `con`/`first` marker paired with the next `key` marker.
- **Filtering**: 1 Hz high-pass via MNE-Python (IIR).
- **Epoching**: 0 to +500 ms post-stimulus with ±300 ms padding for wavelet edge control.
- **Quality metrics** (per epoch): peak-to-peak, band-limited FFT (theta on Fz-Pz, beta on C3-C4), max |z|, burst impact, between-channel coincidence.
- **Rejection thresholds** (frozen from the reference implementation):
  - Blink: `fz_ptp` > 80 µV
  - Gross EMG: `beta_fft` > 150 000
  - Burst: `maxz` ≥ 3.6 **and** `impact` ≥ 15
  - Coincidence: `coinc` ≥ 3.0
- **Spectral power**: complex Morlet wavelets via PyWavelets, 1–40 Hz in 0.5 Hz steps, 3 cycles for theta, 7 cycles for beta. Averaged over surviving trials per condition.
- **Balance flag**: `|excl_pct_con − excl_pct_inc| > 10`.
- **Frontend**: Plotly.js, vanilla HTML/CSS/JS.
- **Backend**: Python FastAPI, in-memory results (no database).
- **Privacy**: everything local.

---

*Made by M with 💛*

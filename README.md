# 🧠 EEG Flanker Analysis Tool

A web-based tool that processes EEG recordings from an **Eriksen Flanker Task** experiment. Upload your LabChart `.txt` export files and the tool automatically parses the data, extracts **theta** and **beta** brain wave power for congruent vs incongruent trials across both experimental blocks, displays interactive charts, and exports SPSS-ready CSV files.

Everything runs locally on your computer — your data never leaves your machine.

![Made with love](https://img.shields.io/badge/made%20with-💛-yellow)
![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## 📖 What the Tool Does

This tool implements the full EEG analysis pipeline described in the developer brief:

1. **Reads LabChart 8 text exports** — parses the file header (sampling rate, channel names, recording date) and the two channels of EEG data, plus all embedded comment markers
2. **Identifies trial events** — finds `con` (congruent) and `inc`/`first` (incongruent) markers, determines which block (1 or 2) each trial belongs to, and handles the known marker mislabelling where `first` actually means incongruent
3. **Filters the signal** — applies a 1–40 Hz bandpass filter to both channels using MNE-Python
4. **Extracts epochs** — cuts the continuous EEG into 1.2-second windows around each stimulus onset (-200ms to +1000ms), one epoch per trial (160 trials total: 80 per block)
5. **Applies baseline correction** — subtracts the mean signal from the 200ms before each stimulus to remove drift
6. **Computes frequency power** — runs FFT on each epoch and extracts:
   - **Theta power (4–8 Hz)** from Channel 1 (Fz–Pz)
   - **Beta power (13–30 Hz)** from Channel 2 (Cz–P4)
7. **Averages by condition** — computes mean power for congruent vs incongruent trials
8. **Exports CSV files** ready for direct import into SPSS

---

## 🖥 The Three Screens

### Screen 1 — Upload

The landing page. You see a drop zone where you can drag and drop one or more LabChart `.txt` export files (or click to browse). A progress indicator shows while each file is being processed.

- **Drop one file** → goes to the individual analysis screen
- **Drop multiple files at once** → goes straight to the group comparison screen
- You can also upload one file at a time and build up a group incrementally

### Screen 2 — Individual Analysis

Shows the full analysis results for a single participant. This screen contains:

**Info bar** at the top showing:
- Filename (subject ID)
- Recording date (extracted from the LabChart file)
- Sampling rate
- Total epoch count (e.g. "160 — 80 con / 80 inc")

**Two power summary cards:**
- **θ Theta Power** — shows mean theta power (4–8 Hz) from Channel 1 (Fz–Pz) for congruent vs incongruent trials, with a visual comparison bar
- **β Beta Power** — shows mean beta power (13–30 Hz) from Channel 2 (Cz–P4) for congruent vs incongruent trials, with a visual comparison bar

**Five interactive charts** (hover for values, zoom, pan):
- **Averaged ERP — Ch1** — the averaged event-related potential waveform for Channel 1, with separate lines for congruent (teal) and incongruent (pink) conditions. Time axis is -200ms to +1000ms relative to stimulus onset
- **Averaged ERP — Ch2** — same for Channel 2
- **Power Spectrum — Ch1** — frequency power distribution for Channel 1 with the theta band (4–8 Hz) highlighted as a shaded region
- **Power Spectrum — Ch2** — frequency power distribution for Channel 2 with the beta band (13–30 Hz) highlighted
- **Single-Trial Power Distribution** — violin plots showing the spread of theta and beta power values across all individual trials. This helps you see whether the mean values are representative or if there are outliers skewing the average

**Action buttons:**
- **Summary CSV** — downloads a one-row summary with averaged power values for this subject
- **Trial-Level CSV** — downloads a detailed CSV with one row per trial (160 rows), including block number, condition, and individual theta/beta power values
- **Add Another Subject** — returns to the upload screen to add more files
- **Compare Subjects** — once 2+ subjects are uploaded, switches to the group comparison view
- **Start Over** — clears all uploaded data and returns to the upload screen

**Subject list** — when multiple subjects have been uploaded, a list appears showing all subjects with colour-coded chips. You can remove individual subjects by clicking the × on their chip.

### Screen 3 — Group Comparison

Shows all uploaded subjects side by side. This screen contains:

**Summary table** with one row per subject showing:
- Subject name, recording date
- Theta power (congruent and incongruent)
- Beta power (congruent and incongruent)
- Total epoch count

**Four comparison charts:**
- **θ Theta Power by Subject** — grouped bar chart comparing congruent vs incongruent theta power across all subjects
- **β Beta Power by Subject** — same for beta power
- **Overlaid ERPs — Ch1 (Congruent)** — all subjects' averaged congruent ERP waveforms overlaid on the same axes, each in a different colour
- **Overlaid ERPs — Ch1 (Incongruent)** — same for incongruent condition

**Action buttons:**
- **Group Summary CSV** — downloads a CSV with one row per subject (the format specified in the developer brief), with a `subject` column for easy SPSS sorting
- **Group Trial-Level CSV** — downloads a single CSV containing every trial from every subject (e.g. 5 subjects × 160 trials = 800 rows), with columns for subject, block, condition, and power values. This is the most useful format for SPSS repeated-measures analysis
- **Add More Subjects** — returns to upload to add more files to the group
- **← Back to Individual** — returns to the individual view for the last-viewed subject

### Navigation

Click the **header/logo** at any time to return to the upload screen.

---

## 🚀 How to Install & Run

### What You Need First

- **Python 3.10 or newer** — check by opening Terminal and typing `python3 --version`
  - If you don't have it: go to [python.org/downloads](https://www.python.org/downloads/) and install the latest version
- **A web browser** (Chrome, Firefox, Safari — anything works)

### Step-by-Step

**1. Download this project**

Click the green **Code** button on GitHub → **Download ZIP** → unzip it somewhere easy to find (like your Desktop).

Or if you're comfortable with Terminal:
```bash
git clone https://github.com/mvarge/eeg-analysis.git
cd eeg-analysis
```

**2. Run it**

Open **Terminal** (on Mac: press `Cmd + Space`, type "Terminal", hit Enter).

Then type these commands:
```bash
cd ~/Desktop/eeg-analysis    # or wherever you unzipped it
chmod +x run.sh               # make the run script executable (first time only)
./run.sh                       # start the app
```

The first time you run it, the script will automatically create a Python virtual environment and install all required packages (MNE, SciPy, FastAPI, etc.) — takes about 30 seconds. You'll see:
```
  ╔══════════════════════════════════════╗
  ║     EEG Flanker Analysis Tool        ║
  ╚══════════════════════════════════════╝

→ Installing dependencies (first run only)...
  ✓ Dependencies installed

→ Starting server...
  Open http://localhost:8000 in your browser
  Press Ctrl+C to stop
```

**3. Open in your browser**

Go to: **[http://localhost:8000](http://localhost:8000)**

**4. Upload your data**

- Drag and drop your `.txt` LabChart export file(s) onto the upload area
- **One file** → shows individual analysis with charts
- **Multiple files** → goes straight to group comparison view

**5. Download results**

Two CSV export options are available (both for individual and group views):
- **Summary CSV** — one row per subject with averaged power values
- **Trial-Level CSV** — one row per trial, including block number and condition (ideal for SPSS repeated-measures analysis)

**6. To stop the app**

Go back to Terminal and press **Ctrl + C**.

---

## 📁 Your Data Files

The tool accepts **LabChart 8 text exports** (`.txt` files). These should be exported from LabChart via File → Export → Text, with the Comments checkbox checked so that trial markers are included.

Each file should contain:
- A header with metadata (sampling rate, channel names, recording date)
- Two channels of EEG data (Channel 1: Fz–Pz, Channel 2: Cz–P4)
- Comment markers: `con` (congruent), `inc`/`first` (incongruent), `second` (block boundary)

**Your data stays private** — everything runs on your computer. Nothing is uploaded to the internet.

### Test Data

Want to try it without real data? Generate fake test files:
```bash
.venv/bin/python scripts/generate_fake_data.py
```
This creates fake subject files in `data/` that you can upload to test the tool.

---

## 📊 What's in the Output CSVs

### Summary CSV (one row per subject)

| Column | What it means |
|--------|--------------|
| `subject` | Subject identifier (from filename) |
| `recording_date` | When the EEG was recorded |
| `theta_power_congruent` | Average theta power (4–8 Hz) for congruent trials |
| `theta_power_incongruent` | Average theta power (4–8 Hz) for incongruent trials |
| `beta_power_congruent` | Average beta power (13–30 Hz) for congruent trials |
| `beta_power_incongruent` | Average beta power (13–30 Hz) for incongruent trials |
| `n_epochs_congruent` | Number of congruent trials used |
| `n_epochs_incongruent` | Number of incongruent trials used |

### Trial-Level CSV (one row per trial — best for SPSS)

| Column | What it means |
|--------|--------------|
| `subject` | Subject identifier (from filename) |
| `recording_date` | When the EEG was recorded |
| `trial` | Trial number (1–160) |
| `block` | Block number (1 or 2 — 80 trials each) |
| `condition` | `congruent` or `incongruent` |
| `theta_power` | Theta power (4–8 Hz) for this trial |
| `beta_power` | Beta power (13–30 Hz) for this trial |

> 💡 The trial-level CSV is ideal for SPSS — it's a single flat table you can sort/filter by subject, block, and condition. Use this for repeated-measures ANOVA or mixed-effects models.

---

## 🛠 Troubleshooting

| Problem | Solution |
|---------|----------|
| `python3: command not found` | Install Python from [python.org/downloads](https://www.python.org/downloads/) |
| `permission denied: ./run.sh` | Run `chmod +x run.sh` first |
| `no matching distribution for scipy` | Delete `.venv/` folder and run `./run.sh` again |
| Page won't load | Make sure Terminal is still running and shows the server message |
| Upload error | Make sure you're uploading a `.txt` file exported from LabChart |

---

## 📝 Technical Details

For the curious — the processing pipeline:

- **Parser**: Reads LabChart 8 text exports (Latin-1 encoding, tab-delimited)
- **Marker handling**: Corrects known mislabelling (`first` → incongruent), tracks block boundaries (block 1 vs block 2)
- **Filtering**: 1–40 Hz bandpass via MNE-Python
- **Epoching**: -200ms to +1000ms around stimulus onset (160 epochs per participant)
- **Baseline correction**: Subtracts mean of -200ms to 0ms window
- **Power analysis**: FFT via SciPy, extracts theta (4–8 Hz) from Fz-Pz and beta (13–30 Hz) from Cz-P4
- **Frontend**: Plotly.js interactive charts, vanilla HTML/CSS/JS
- **Backend**: Python FastAPI server (runs locally)
- **Privacy**: All processing happens on your machine — nothing is sent to the internet

---

*Made by M with 💛*

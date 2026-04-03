# 🧠 EEG Flanker Analysis Tool

A simple web app that analyses EEG data from an **Eriksen Flanker Task** experiment. Upload your LabChart recording files, and it automatically extracts **theta** and **beta** brain wave power for congruent vs incongruent trials — with pretty charts and a downloadable CSV ready for SPSS.

![Made with love](https://img.shields.io/badge/made%20with-💛-yellow)
![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## ✨ What It Does

1. **Upload** one or more LabChart `.txt` export files
2. **Automatically processes** the EEG data:
   - Parses the file and finds all trial markers
   - Filters the signal (1–40 Hz bandpass)
   - Cuts epochs around each stimulus (-200ms to +1000ms)
   - Computes power spectra via FFT
   - Extracts theta (4–8 Hz) and beta (13–30 Hz) power
3. **Shows interactive charts**: averaged ERPs, power spectra, and single-trial distributions
4. **Compares subjects**: upload multiple files to see group comparison charts
5. **Downloads CSVs** — both summary and trial-level exports with block tracking, ready for SPSS

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

Put your LabChart `.txt` export files in the `data/` folder (optional — you can also just drag-and-drop them in the browser).

**Your data stays private** — everything runs on your computer. Nothing is uploaded to the internet.

### Test Data

Want to try it without real data? Generate fake test files:
```bash
.venv/bin/python scripts/generate_fake_data.py
```
This creates 3 fake subject files in `data/` that you can upload to test the tool.

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
| Page won't load | Make sure Terminal is still running and shows the server message |
| Upload error | Make sure you're uploading a `.txt` file exported from LabChart |

---

## 📝 Technical Details

For the curious — the processing pipeline:

- **Parser**: Reads LabChart 8 text exports (Latin-1 encoding, tab-delimited)
- **Marker handling**: Corrects known mislabelling (`first` → incongruent), tracks block boundaries
- **Filtering**: 1–40 Hz bandpass via MNE-Python
- **Epoching**: -200ms to +1000ms around stimulus onset
- **Baseline correction**: Subtracts mean of -200ms to 0ms window
- **Power analysis**: FFT via SciPy, extracts theta (4–8 Hz) from Fz-Pz and beta (13–30 Hz) from C3-C4
- **Frontend**: Plotly.js charts, vanilla HTML/CSS/JS
- **Backend**: Python FastAPI server

---

*Made by M with 💛*

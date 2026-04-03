"""
FastAPI server for EEG analysis.

Serves the frontend and provides API endpoints for:
- File upload and processing
- Results retrieval
- CSV download
"""

import os
import tempfile
import io
import csv
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from parser import parse_labchart
from pipeline import run_pipeline

import math
import numpy as np


def sanitize_for_json(val):
    """Replace NaN/Inf with None for JSON serialization."""
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    return val

app = FastAPI(title="EEG Flanker Analysis")

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store results in memory (single-user local app)
_results = {}

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


def downsample_for_json(arr: np.ndarray, max_points: int = 500) -> list:
    """Downsample array for JSON transport if too large."""
    if len(arr) == 0:
        return []
    if len(arr) <= max_points:
        return arr.tolist()
    indices = np.linspace(0, len(arr) - 1, max_points, dtype=int)
    return arr[indices].tolist()


def downsample_pair(times: np.ndarray, values: np.ndarray, max_points: int = 500):
    """Downsample both time and value arrays consistently."""
    if len(times) == 0:
        return [], []
    if len(times) <= max_points:
        return times.tolist(), values.tolist()
    indices = np.linspace(0, len(times) - 1, max_points, dtype=int)
    return times[indices].tolist(), values[indices].tolist()


@app.post("/api/upload")
async def upload_eeg(file: UploadFile = File(...)):
    """Upload and process a LabChart EEG text export."""
    if not file.filename.endswith(".txt"):
        raise HTTPException(400, "Please upload a .txt LabChart export file")

    # Save to temp file
    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="wb") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Parse
        parsed = parse_labchart(tmp_path)
        # Override filename with the original upload name
        parsed.filename = file.filename

        # Run pipeline
        result = run_pipeline(parsed)

        # Store for later retrieval
        result_id = file.filename.replace(".txt", "")
        _results[result_id] = result

        # Build JSON response
        # Downsample waveforms for transport
        epoch_times_ds, avg_ch1_con_ds = downsample_pair(
            result.epoch_times * 1000,  # convert to ms
            result.avg_ch1_congruent
        )
        _, avg_ch1_inc_ds = downsample_pair(result.epoch_times * 1000, result.avg_ch1_incongruent)
        _, avg_ch2_con_ds = downsample_pair(result.epoch_times * 1000, result.avg_ch2_congruent)
        _, avg_ch2_inc_ds = downsample_pair(result.epoch_times * 1000, result.avg_ch2_incongruent)

        # Power spectra (limit to 0-50 Hz for display)
        freq_mask = result.spectrum_freqs <= 50
        spec_freqs = result.spectrum_freqs[freq_mask]
        spec_ch1_con = result.avg_spectrum_ch1_con[freq_mask] if len(result.avg_spectrum_ch1_con) > 0 else np.array([])
        spec_ch1_inc = result.avg_spectrum_ch1_inc[freq_mask] if len(result.avg_spectrum_ch1_inc) > 0 else np.array([])
        spec_ch2_con = result.avg_spectrum_ch2_con[freq_mask] if len(result.avg_spectrum_ch2_con) > 0 else np.array([])
        spec_ch2_inc = result.avg_spectrum_ch2_inc[freq_mask] if len(result.avg_spectrum_ch2_inc) > 0 else np.array([])

        # Per-epoch power values for scatter/distribution plots
        epoch_powers = []
        for p in result.power_results:
            epoch_powers.append({
                "trial": p.trial_index,
                "condition": p.condition,
                "theta_power": round(float(p.ch1_theta_power), 6),
                "beta_power": round(float(p.ch2_beta_power), 6),
            })

        return {
            "status": "success",
            "result_id": result_id,
            "summary": {
                "filename": result.filename,
                "recording_date": result.recording_date,
                "sampling_rate": result.sampling_rate,
                "channel_names": result.channel_names,
                "theta_power_congruent": sanitize_for_json(round(result.theta_power_congruent, 6)),
                "theta_power_incongruent": sanitize_for_json(round(result.theta_power_incongruent, 6)),
                "beta_power_congruent": sanitize_for_json(round(result.beta_power_congruent, 6)),
                "beta_power_incongruent": sanitize_for_json(round(result.beta_power_incongruent, 6)),
                "n_epochs_congruent": result.n_epochs_congruent,
                "n_epochs_incongruent": result.n_epochs_incongruent,
            },
            "waveforms": {
                "times_ms": epoch_times_ds,
                "ch1_congruent": avg_ch1_con_ds,
                "ch1_incongruent": avg_ch1_inc_ds,
                "ch2_congruent": avg_ch2_con_ds,
                "ch2_incongruent": avg_ch2_inc_ds,
            },
            "spectra": {
                "freqs": spec_freqs.tolist(),
                "ch1_congruent": spec_ch1_con.tolist(),
                "ch1_incongruent": spec_ch1_inc.tolist(),
                "ch2_congruent": spec_ch2_con.tolist(),
                "ch2_incongruent": spec_ch2_inc.tolist(),
            },
            "epoch_powers": epoch_powers,
        }

    except Exception as e:
        raise HTTPException(500, f"Processing error: {str(e)}")
    finally:
        os.unlink(tmp_path)


@app.get("/api/download-csv/{result_id}")
async def download_csv(result_id: str):
    """Download SPSS-ready summary CSV."""
    if result_id not in _results:
        raise HTTPException(404, "Result not found. Please upload a file first.")

    r = _results[result_id]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "filename", "recording_date",
        "theta_power_congruent", "theta_power_incongruent",
        "beta_power_congruent", "beta_power_incongruent",
        "n_epochs_congruent", "n_epochs_incongruent",
    ])
    writer.writerow([
        r.filename, r.recording_date,
        round(r.theta_power_congruent, 6),
        round(r.theta_power_incongruent, 6),
        round(r.beta_power_congruent, 6),
        round(r.beta_power_incongruent, 6),
        r.n_epochs_congruent, r.n_epochs_incongruent,
    ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={result_id}_summary.csv"},
    )


@app.get("/api/subjects")
async def list_subjects():
    """List all uploaded subjects."""
    subjects = []
    for rid, r in _results.items():
        subjects.append({
            "result_id": rid,
            "filename": r.filename,
            "recording_date": r.recording_date,
            "sampling_rate": r.sampling_rate,
            "n_epochs_congruent": r.n_epochs_congruent,
            "n_epochs_incongruent": r.n_epochs_incongruent,
        })
    return {"subjects": subjects}


@app.delete("/api/subjects/{result_id}")
async def remove_subject(result_id: str):
    """Remove a subject from memory."""
    if result_id in _results:
        del _results[result_id]
    return {"status": "ok"}


@app.get("/api/compare")
async def compare_subjects():
    """Compare all uploaded subjects — returns summary + power data for group charts."""
    if len(_results) < 2:
        raise HTTPException(400, "Need at least 2 subjects to compare. Upload more files.")

    subjects = []
    for rid, r in _results.items():
        # Waveforms downsampled
        epoch_times_ds, avg_ch1_con_ds = downsample_pair(
            r.epoch_times * 1000, r.avg_ch1_congruent
        )
        _, avg_ch1_inc_ds = downsample_pair(r.epoch_times * 1000, r.avg_ch1_incongruent)
        _, avg_ch2_con_ds = downsample_pair(r.epoch_times * 1000, r.avg_ch2_congruent)
        _, avg_ch2_inc_ds = downsample_pair(r.epoch_times * 1000, r.avg_ch2_incongruent)

        subjects.append({
            "result_id": rid,
            "filename": r.filename,
            "recording_date": r.recording_date,
            "channel_names": r.channel_names,
            "theta_power_congruent": round(r.theta_power_congruent, 6),
            "theta_power_incongruent": round(r.theta_power_incongruent, 6),
            "beta_power_congruent": round(r.beta_power_congruent, 6),
            "beta_power_incongruent": round(r.beta_power_incongruent, 6),
            "n_epochs_congruent": r.n_epochs_congruent,
            "n_epochs_incongruent": r.n_epochs_incongruent,
            "waveforms": {
                "times_ms": epoch_times_ds,
                "ch1_congruent": avg_ch1_con_ds,
                "ch1_incongruent": avg_ch1_inc_ds,
                "ch2_congruent": avg_ch2_con_ds,
                "ch2_incongruent": avg_ch2_inc_ds,
            },
        })

    return {"subjects": subjects}


@app.get("/api/download-csv-all")
async def download_csv_all():
    """Download combined SPSS-ready CSV for all subjects."""
    if not _results:
        raise HTTPException(404, "No results. Upload files first.")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "filename", "recording_date",
        "theta_power_congruent", "theta_power_incongruent",
        "beta_power_congruent", "beta_power_incongruent",
        "n_epochs_congruent", "n_epochs_incongruent",
    ])
    for r in _results.values():
        writer.writerow([
            r.filename, r.recording_date,
            round(r.theta_power_congruent, 6),
            round(r.theta_power_incongruent, 6),
            round(r.beta_power_congruent, 6),
            round(r.beta_power_incongruent, 6),
            r.n_epochs_congruent, r.n_epochs_incongruent,
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=eeg_group_summary.csv"},
    )


# Serve frontend
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)

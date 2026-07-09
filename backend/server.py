"""
FastAPI server for the EEG Flanker Analysis tool.

Wraps the parser + pipeline and exposes:
  POST /api/upload                         upload + analyse one LabChart file
  GET  /api/subjects                       list all in-memory results
  DELETE /api/subjects/{id}                drop one from memory
  GET  /api/compare                        aggregate data for group view
  GET  /api/download-csv-trials/{id}       per-trial CSV (results.csv format)
  GET  /api/download-csv-exclusions/{id}   excluded-trials CSV (exclusions.csv)
  GET  /api/download-csv/{id}              per-subject summary (medians)
  GET  /api/download-csv-trials-all        combined per-trial CSV
  GET  /api/download-csv-all               combined summary CSV
  GET  /api/download-csv-batch-summary     batch summary with balance flags
"""

from __future__ import annotations

import csv
import io
import math
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List

import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from parser import parse_labchart
from pipeline import (
    PipelineResult, TrialResult, ChannelSummary,
    BLINK_UV, EMG_BETA, BURST_Z, BURST_IMPACT, COINC_Z,
    HP_HZ, WIN_S, PAD_S, THETA_BAND, BETA_BAND, TOTAL_BAND,
    run_pipeline,
)
from demographics import (
    Demographic, DISPLAY_FIELDS,
    parse_demographics, match_demographics, parse_filename_ids,
)


app = FastAPI(title="EEG Flanker Analysis")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_results: Dict[str, PipelineResult] = {}
_demographics: List[Demographic] = []           # loaded from most recent CSV upload
_demographics_source: str | None = None         # filename of the CSV upload, for UI

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


# ============================================================
#  Helpers
# ============================================================
def _safe(x):
    """Replace NaN/Inf with None for JSON."""
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return x


def _condition_label(cond: str) -> str:
    """Convert internal marker label to display condition."""
    return "congruent" if cond == "con" else "incongruent"


def _demographic_payload(filename: str) -> dict:
    """
    Build the demographics payload for a given filename.

    Always returns a dict with `matched: bool`, `session/participant` parsed
    from the filename (or None), and `fields`, a list of {key, label, value}
    entries in display order. `block_order` maps 1/2 → e.g. '60 Hz'.
    """
    ids = parse_filename_ids(filename)
    payload = {
        "matched": False,
        "session": ids[0] if ids else None,
        "participant": ids[1] if ids else None,
        "fields": [],
        "block_order": {},
        "aborted": False,
        "csv_source": _demographics_source,
    }
    if not _demographics:
        return payload
    demo = match_demographics(filename, _demographics)
    if demo is None:
        return payload
    payload["matched"] = True
    payload["session"] = demo.session
    payload["participant"] = demo.participant
    payload["aborted"] = demo.aborted
    payload["block_order"] = {str(k): v for k, v in demo.block_order.items()}
    payload["fields"] = [
        {"key": key, "label": label, "value": demo.display.get(key, "")}
        for key, _col, label in DISPLAY_FIELDS
    ]
    return payload


def _summary_payload(r: PipelineResult) -> dict:
    """Structured summary for the frontend."""
    return {
        "filename": r.filename,
        "recording_date": r.recording_date,
        "sampling_rate": r.sampling_rate,
        "channel_names": r.channel_names,
        "n_trials": r.n_trials,
        "n_blocks": r.n_blocks,
        "n_congruent": sum(1 for t in r.trials if t.cond == "con"),
        "n_incongruent": sum(1 for t in r.trials if t.cond == "first"),
        "theta": _channel_payload(r.theta_summary),
        "beta":  _channel_payload(r.beta_summary),
        "demographics": _demographic_payload(r.filename),
        "config": {
            "hp_hz": HP_HZ,
            "window_s": WIN_S,
            "pad_s": PAD_S,
            "theta_band": list(THETA_BAND),
            "beta_band": list(BETA_BAND),
            "total_band": list(TOTAL_BAND),
            "blink_uv": BLINK_UV,
            "emg_beta": EMG_BETA,
            "burst_z": BURST_Z,
            "burst_impact": BURST_IMPACT,
            "coinc_z": COINC_Z,
        },
    }


def _channel_payload(s: ChannelSummary) -> dict:
    return {
        "channel": s.channel,
        "band": s.band,
        "surviving": s.surviving,
        "excluded": s.excluded,
        "surviving_by_condition": s.surviving_by_condition,
        "excluded_by_condition": s.excluded_by_condition,
        "exclusion_pct_con": _safe(round(s.exclusion_pct_con, 1)),
        "exclusion_pct_inc": _safe(round(s.exclusion_pct_inc, 1)),
        "balance_flag": s.balance_flag,
        "abs_median_con": _safe(round(s.abs_median_con, 3)),
        "abs_median_inc": _safe(round(s.abs_median_inc, 3)),
        "rel_median_con": _safe(round(s.rel_median_con, 4)),
        "rel_median_inc": _safe(round(s.rel_median_inc, 4)),
    }


def _trial_row(t: TrialResult) -> dict:
    """Trial payload for the frontend (rounded for JSON size)."""
    return {
        "trial": t.trial,
        "btrial": t.btrial,
        "block": t.block,
        "cond": t.cond,
        "condition": _condition_label(t.cond),
        "onset": round(t.onset, 4),
        "key": round(t.key, 3),
        "rt_ms": t.rt_ms,
        "fz_ptp": round(t.fz_ptp, 2),
        "theta_fft": round(t.theta_fft, 2),
        "beta_fft": round(t.beta_fft, 2),
        "maxz": round(t.maxz, 3),
        "impact": round(t.impact, 2),
        "coinc": round(t.coinc, 3),
        "blink": t.blink,
        "fz_exclude": t.fz_exclude,
        "c3_exclude": t.c3_exclude,
        "reason": t.reason,
        "theta_abs": round(t.theta_abs, 3),
        "theta_rel": round(t.theta_rel, 4),
        "beta_abs":  round(t.beta_abs, 3),
        "beta_rel":  round(t.beta_rel, 4),
    }


def _spectra_payload(r: PipelineResult) -> dict:
    """Wavelet spectra (averaged across surviving trials) for plotting."""
    # Per-trial spectra rounded to 4 decimals to keep the JSON small.
    def round_matrix(arr):
        return [[round(float(v), 4) for v in row] for row in arr]

    return {
        "freqs": r.spectrum_freqs.tolist(),
        "theta_congruent":   r.theta_spectrum_con.tolist(),
        "theta_incongruent": r.theta_spectrum_inc.tolist(),
        "beta_congruent":    r.beta_spectrum_con.tolist(),
        "beta_incongruent":  r.beta_spectrum_inc.tolist(),
        "theta_excluded":    r.theta_spectrum_excluded.tolist(),
        "beta_excluded":     r.beta_spectrum_excluded.tolist(),
        # Per-trial spectra so the frontend can toggle Avg / All views
        "theta_per_trial":   round_matrix(r.theta_spec_all),
        "beta_per_trial":    round_matrix(r.beta_spec_all),
    }


# ============================================================
#  Endpoints
# ============================================================
@app.post("/api/upload")
async def upload_eeg(file: UploadFile = File(...)):
    """Upload and analyse a LabChart .txt export."""
    if not file.filename.endswith(".txt"):
        raise HTTPException(400, "Please upload a .txt LabChart export file")

    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="wb") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        parsed = parse_labchart(tmp_path)
        parsed.filename = file.filename.replace(".txt", "")

        result = run_pipeline(parsed)
        result_id = parsed.filename
        _results[result_id] = result

        return {
            "status": "success",
            "result_id": result_id,
            "summary": _summary_payload(result),
            "trials": [_trial_row(t) for t in result.trials],
            "spectra": _spectra_payload(result),
        }

    except Exception as e:
        raise HTTPException(500, f"Processing error: {e}")
    finally:
        os.unlink(tmp_path)


@app.get("/api/subjects")
async def list_subjects():
    """List all uploaded subjects."""
    return {
        "subjects": [
            {
                "result_id": rid,
                "filename": r.filename,
                "recording_date": r.recording_date,
                "n_trials": r.n_trials,
                "theta_surviving": r.theta_summary.surviving,
                "beta_surviving": r.beta_summary.surviving,
            }
            for rid, r in _results.items()
        ]
    }


@app.delete("/api/subjects/{result_id}")
async def remove_subject(result_id: str):
    """Drop one subject from memory."""
    _results.pop(result_id, None)
    return {"status": "ok"}


# ============================================================
#  Demographics
# ============================================================
@app.post("/api/demographics/upload")
async def upload_demographics(file: UploadFile = File(...)):
    """
    Upload the demographics CSV. Replaces any previously loaded demographics
    in memory. Returns how many rows parsed + which uploaded LabChart files
    now have a matching demographic record.
    """
    global _demographics, _demographics_source
    raw = await file.read()
    try:
        demos = parse_demographics(raw)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse demographics CSV: {exc}")

    _demographics = demos
    _demographics_source = file.filename or "demographics.csv"

    # Report matching against currently uploaded EEG files
    matches = []
    unmatched = []
    for rid, r in _results.items():
        ids = parse_filename_ids(r.filename)
        m = match_demographics(r.filename, demos) if ids else None
        (matches if m else unmatched).append({"result_id": rid, "filename": r.filename})

    return {
        "status": "success",
        "csv_source": _demographics_source,
        "n_participants": len(demos),
        "matches": matches,
        "unmatched": unmatched,
    }


@app.get("/api/demographics")
async def get_demographics():
    """List all loaded demographic rows (compact summary)."""
    return {
        "csv_source": _demographics_source,
        "n_participants": len(_demographics),
        "participants": [
            {
                "session": d.session,
                "participant": d.participant,
                "filename_stem": f"S{d.session}P{d.participant:03d}",
                "age": d.display.get("age"),
                "sex": d.display.get("sex"),
                "block_order": {str(k): v for k, v in d.block_order.items()},
                "aborted": d.aborted,
            }
            for d in _demographics
        ],
        "fields": [
            {"key": k, "label": lbl}
            for k, _col, lbl in DISPLAY_FIELDS
        ],
    }


@app.delete("/api/demographics")
async def clear_demographics():
    """Remove the currently loaded demographics."""
    global _demographics, _demographics_source
    _demographics = []
    _demographics_source = None
    return {"status": "ok"}


@app.get("/api/compare")
async def compare_subjects():
    """Aggregate payload for the group view."""
    if len(_results) < 2:
        raise HTTPException(400, "Need at least 2 subjects to compare. Upload more files.")

    return {
        "subjects": [
            {
                "result_id": rid,
                "summary": _summary_payload(r),
                "trials": [_trial_row(t) for t in r.trials],
            }
            for rid, r in _results.items()
        ]
    }


# ============================================================
#  CSV downloads
# ============================================================
def _csv_response(rows: List[list], header: list, filename: str) -> StreamingResponse:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header)
    writer.writerows(rows)
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


TRIAL_CSV_HEADER = [
    "recording", "trial", "block", "btrial", "block_hz", "cond", "onset", "key", "rt_ms",
    "fz_ptp", "theta_fft", "beta_fft", "maxz", "impact", "coinc",
    "blink", "fz_exclude", "c3_exclude", "reason",
    "theta_abs", "theta_rel", "beta_abs", "beta_rel",
]


def _demographic_columns():
    """Return list of (header, key) tuples for demographic columns in CSV order."""
    if not _demographics:
        return []
    return [(f"demo_{key}", key) for key, _col, _label in DISPLAY_FIELDS] + [("demo_matched", "__matched")]


def _demographic_values(filename: str, key_list: list) -> list:
    """Return CSV values for a filename's demographics matching key_list order."""
    if not _demographics:
        return []
    demo = match_demographics(filename, _demographics)
    out = []
    for _hdr, key in key_list:
        if key == "__matched":
            out.append(bool(demo))
        else:
            out.append(demo.display.get(key, "") if demo else "")
    return out


def _trial_csv_row(rec: str, t: TrialResult, demo_cols=None) -> list:
    if demo_cols is None:
        demo_cols = _demographic_columns()
    demo = match_demographics(rec, _demographics) if _demographics else None
    block_hz = demo.block_order.get(t.block, "") if demo else ""
    row = [
        rec, t.trial, t.block, t.btrial, block_hz, t.cond,
        round(t.onset, 4), round(t.key, 4), t.rt_ms,
        round(t.fz_ptp, 4), round(t.theta_fft, 4), round(t.beta_fft, 4),
        round(t.maxz, 6), round(t.impact, 6), round(t.coinc, 6),
        t.blink, t.fz_exclude, t.c3_exclude, t.reason,
        round(t.theta_abs, 6), round(t.theta_rel, 6),
        round(t.beta_abs, 6),  round(t.beta_rel, 6),
    ]
    row.extend(_demographic_values(rec, demo_cols))
    return row


@app.get("/api/download-csv-trials/{result_id}")
async def download_trials_csv(result_id: str):
    """Per-trial CSV for one subject (results.csv format)."""
    if result_id not in _results:
        raise HTTPException(404, "Result not found.")
    r = _results[result_id]
    demo_cols = _demographic_columns()
    header = TRIAL_CSV_HEADER + [h for h, _ in demo_cols]
    rows = [_trial_csv_row(r.filename, t, demo_cols) for t in r.trials]
    return _csv_response(rows, header, f"{result_id}_results.csv")


@app.get("/api/download-csv-trials-all")
async def download_trials_csv_all():
    """Combined per-trial CSV across all uploaded subjects."""
    if not _results:
        raise HTTPException(404, "No results.")
    demo_cols = _demographic_columns()
    header = TRIAL_CSV_HEADER + [h for h, _ in demo_cols]
    rows = []
    for r in _results.values():
        rows.extend(_trial_csv_row(r.filename, t, demo_cols) for t in r.trials)
    return _csv_response(rows, header, "eeg_group_results.csv")


EXCL_HEADER = ["recording", "trial", "block", "btrial", "cond", "rt_ms",
               "fz_exclude", "c3_exclude", "reason"]


@app.get("/api/download-csv-exclusions/{result_id}")
async def download_exclusions_csv(result_id: str):
    """Exclusion log for one subject."""
    if result_id not in _results:
        raise HTTPException(404, "Result not found.")
    r = _results[result_id]
    rows = [
        [r.filename, t.trial, t.block, t.btrial, t.cond, t.rt_ms,
         t.fz_exclude, t.c3_exclude, t.reason]
        for t in r.trials if t.fz_exclude or t.c3_exclude
    ]
    return _csv_response(rows, EXCL_HEADER, f"{result_id}_exclusions.csv")


SUMMARY_HEADER = [
    "recording", "recording_date",
    "theta_surviving", "theta_excluded",
    "theta_rel_median_con", "theta_rel_median_inc",
    "theta_abs_median_con", "theta_abs_median_inc",
    "beta_surviving", "beta_excluded",
    "beta_rel_median_con", "beta_rel_median_inc",
    "beta_abs_median_con", "beta_abs_median_inc",
    "theta_exclusion_pct_con", "theta_exclusion_pct_inc", "theta_balance_flag",
    "beta_exclusion_pct_con",  "beta_exclusion_pct_inc",  "beta_balance_flag",
    "block1_hz", "block2_hz",
]


def _summary_row(r: PipelineResult, demo_cols=None) -> list:
    if demo_cols is None:
        demo_cols = _demographic_columns()
    ts, bs = r.theta_summary, r.beta_summary
    demo = match_demographics(r.filename, _demographics) if _demographics else None
    row = [
        r.filename, r.recording_date,
        ts.surviving, ts.excluded,
        round(ts.rel_median_con, 6), round(ts.rel_median_inc, 6),
        round(ts.abs_median_con, 6), round(ts.abs_median_inc, 6),
        bs.surviving, bs.excluded,
        round(bs.rel_median_con, 6), round(bs.rel_median_inc, 6),
        round(bs.abs_median_con, 6), round(bs.abs_median_inc, 6),
        round(ts.exclusion_pct_con, 2), round(ts.exclusion_pct_inc, 2), ts.balance_flag,
        round(bs.exclusion_pct_con, 2), round(bs.exclusion_pct_inc, 2), bs.balance_flag,
        demo.block_order.get(1, "") if demo else "",
        demo.block_order.get(2, "") if demo else "",
    ]
    row.extend(_demographic_values(r.filename, demo_cols))
    return row


@app.get("/api/download-csv/{result_id}")
async def download_summary_csv(result_id: str):
    """Single-subject summary CSV (medians + balance flags)."""
    if result_id not in _results:
        raise HTTPException(404, "Result not found.")
    r = _results[result_id]
    demo_cols = _demographic_columns()
    header = SUMMARY_HEADER + [h for h, _ in demo_cols]
    return _csv_response([_summary_row(r, demo_cols)], header, f"{result_id}_summary.csv")


@app.get("/api/download-csv-all")
async def download_summary_csv_all():
    """Group summary CSV (one row per subject)."""
    if not _results:
        raise HTTPException(404, "No results.")
    demo_cols = _demographic_columns()
    header = SUMMARY_HEADER + [h for h, _ in demo_cols]
    rows = [_summary_row(r, demo_cols) for r in _results.values()]
    return _csv_response(rows, header, "eeg_group_summary.csv")


# ============================================================
#  Static frontend
# ============================================================
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)

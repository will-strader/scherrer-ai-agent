VERSION = "v1.0.0"

import os
import uuid
import json
from pathlib import Path
from datetime import datetime, timedelta
import asyncio

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Depends, Header, Form
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from backend.models import ProcessResponse, JobStatus
from backend.extractor import extract_answers, extract_answers_async
from backend.writer import fill_template
from backend.mapping import Mapping, load_mapping
from backend.config import MAPPING_CSV, EXCEL_TEMPLATE

load_dotenv()

print(f"[config] AI Bid Assistant backend version {VERSION} initialized")

# --- API Key check system ---
API_KEY = os.getenv("API_KEY")
if API_KEY is None:
    print("[config] WARNING: No API_KEY set — authentication disabled")
else:
    print("[config] API authentication enabled")

# TEMPORARY: disable frontend token authentication
# TODO: re-enable when production-ready
def verify_frontend_token(request):
    return True

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "storage" / "uploads"
OUTPUTS = BASE / "storage" / "outputs"
JOBS_DIR = BASE / "storage" / "jobs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))

app = FastAPI(title="AI Bid Assistant (MVP)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://scherrer-ai-agent-frontend.onrender.com",
        "http://localhost:5173"
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job registry (good enough for local dev)
JOBS = {}  # job_id -> JobStatus

# --- Progress tracking helpers ---
DEFAULT_PROGRESS = {
    "pct": 0,
    "processed_chunks": 0,
    "total_chunks": 0,
    "stage": "queued",
    "log": [],  # keep short rolling log
}

def _job(job_id: str) -> dict:
    j = JOBS.get(job_id)
    if j is None:
        raise KeyError(f"Unknown job {job_id}")
    return j

def log_progress(job_id: str, note: str):
    job = _job(job_id)
    prog = job.setdefault("progress", dict(DEFAULT_PROGRESS))
    # append note (trim list to last 50 entries)
    prog.setdefault("log", [])
    prog["log"].append(note)
    if len(prog["log"]) > 50:
        prog["log"] = prog["log"][-50:]
    save_job_state(job_id)

def set_progress(job_id: str, *, pct: int | None = None, stage: str | None = None,
                 processed_chunks: int | None = None, total_chunks: int | None = None,
                 note: str | None = None):
    job = _job(job_id)
    prog = job.setdefault("progress", dict(DEFAULT_PROGRESS))
    if pct is not None:
        prog["pct"] = max(0, min(100, int(pct)))
    if stage is not None:
        prog["stage"] = stage
    if processed_chunks is not None:
        prog["processed_chunks"] = int(processed_chunks)
    if total_chunks is not None:
        prog["total_chunks"] = int(total_chunks)
    if note:
        prog.setdefault("log", [])
        prog["log"].append(note)
        if len(prog["log"]) > 50:
            prog["log"] = prog["log"][-50:]
    save_job_state(job_id)

def save_job_state(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return
    # Save only job_id, status, message, output_paths, progress
    data = {
        "job_id": job.get("job_id"),
        "status": job.get("status"),
        "message": job.get("message"),
        "output_paths": job.get("output_paths", {}),
        "progress": job.get("progress", DEFAULT_PROGRESS),
    }
    job_file = JOBS_DIR / f"{job_id}.json"
    with job_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"[{job_id}] Job state saved to disk at {job_file}")

def load_job_state(job_id: str):
    job_file = JOBS_DIR / f"{job_id}.json"
    if job_file.exists():
        with job_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        JOBS[job_id] = {
            "job_id": data.get("job_id"),
            "status": data.get("status"),
            "message": data.get("message"),
            "output_paths": data.get("output_paths", {}),
            "progress": data.get("progress", dict(DEFAULT_PROGRESS)),
        }
        print(f"[{job_id}] Job state loaded from disk at {job_file}")
        return JOBS[job_id]
    return None

@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>AI Bid Assistant Backend</h3><p>POST /process with a PDF to get started.</p>"

@app.get("/version")
def version():
    return {"version": VERSION, "backend": True}

@app.get("/ping")
def ping():
    return {"ok": True}

@app.post(
    "/process",
    response_model=ProcessResponse,
)
async def process_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    concurrency: int | None = Form(None),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a .pdf")

    job_id = str(uuid.uuid4())
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    pdf_name = f"{ts}__{job_id}__{file.filename}"
    pdf_path = UPLOADS / pdf_name
    with pdf_path.open("wb") as f:
        f.write(await file.read())

    # read requested concurrency from form (optional) and clamp to safe range
    if concurrency is None:
        requested_concurrency = 2  # default (Normal)
    else:
        try:
            requested_concurrency = int(concurrency)
        except (TypeError, ValueError):
            requested_concurrency = 2
    # clamp between 1 and 6 to avoid overloading free-tier dynos
    requested_concurrency = max(1, min(6, requested_concurrency))

    # initialize job with progress
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "output_paths": {},
        "message": None,
        "progress": dict(DEFAULT_PROGRESS),
        "concurrency": requested_concurrency,
    }
    set_progress(job_id, pct=1, stage="queued", note=f"Job created; file saved (concurrency={requested_concurrency})")
    save_job_state(job_id)

    import threading
    threading.Thread(target=lambda: asyncio.run(_process_job(job_id, pdf_path)), daemon=True).start()
    return ProcessResponse(job_id=job_id, status="queued")

async def _process_job(job_id: str, pdf_path: Path):
    try:
        JOBS[job_id]["status"] = "processing"
        JOBS[job_id]["message"] = "Loading mapping"
        save_job_state(job_id)
        set_progress(job_id, pct=5, stage="loading mapping", note="Loading mapping CSV")
        print(f"[{job_id}] Loading mapping")

        # 1) load mapping (handles your Numbers/CSV quirks)
        mapping = load_mapping(MAPPING_CSV)
        JOBS[job_id]["message"] = "Mapping loaded"
        save_job_state(job_id)
        set_progress(job_id, pct=10, stage="mapping loaded", note="Mapping loaded")
        print(f"[{job_id}] Mapping loaded")

        # 2) extract answers from the PDF using OpenAI (returns dict keyed by json_key)
        JOBS[job_id]["message"] = "Extracting answers from PDF"
        save_job_state(job_id)
        desired_concurrency = JOBS.get(job_id, {}).get("concurrency", 2)
        set_progress(job_id, pct=15, stage="extracting", note=f"Starting extraction (concurrency={desired_concurrency})")
        print(f"[{job_id}] Extracting answers from PDF")

        def progress_cb(done: int, total: int, note: str | None = None):
            # Map chunk progress to 15..85% range
            pct = 15 if total <= 0 else 15 + int((done / max(1, total)) * 70)
            set_progress(job_id, pct=pct, stage="extracting",
                         processed_chunks=done, total_chunks=total,
                         note=note or f"Processed {done}/{total}")

        raw_answers = await extract_answers_async(pdf_path, mapping, progress_cb=progress_cb, concurrency=desired_concurrency)
        JOBS[job_id]["message"] = "Extraction complete"
        save_job_state(job_id)
        set_progress(job_id, pct=86, stage="post-processing", note="Extraction complete")
        print(f"[{job_id}] Extraction complete")

        # Prepare structured answers including answer, confidence, and source
        structured_answers = {}
        for key, val in raw_answers.items():
            if isinstance(val, dict) and all(k in val for k in ("answer", "confidence", "source")):
                structured_answers[key] = val
            else:
                # Wrap raw value into structured format with defaults
                structured_answers[key] = {
                    "answer": val,
                    "confidence": None,
                    "source": None,
                }

        JOBS[job_id]["message"] = "Writing JSON results"
        save_job_state(job_id)
        set_progress(job_id, pct=88, stage="writing json", note="Writing JSON output")
        print(f"[{job_id}] Writing JSON results")

        # 3) write raw JSON for debugging/auditing
        json_out = OUTPUTS / f"{pdf_path.stem}__{job_id}.json"
        json_out.write_text(json.dumps(structured_answers, indent=2))
        JOBS[job_id]["message"] = "JSON results written"
        save_job_state(job_id)
        set_progress(job_id, pct=90, stage="writing excel", note="Filling Excel template")
        print(f"[{job_id}] JSON results written")

        # 4) fill the real Excel template (preserves formatting/formulas)
        JOBS[job_id]["message"] = "Filling Excel template"
        save_job_state(job_id)
        print(f"[{job_id}] Filling Excel template")
        fill_template(mapping, structured_answers, xlsx_out := OUTPUTS / f"{pdf_path.stem}__{job_id}.xlsx")
        JOBS[job_id]["message"] = "Excel template filled"
        save_job_state(job_id)
        set_progress(job_id, pct=98, stage="finalizing", note="Preparing download links")
        print(f"[{job_id}] Excel template filled")

        JOBS[job_id]["output_paths"] = {
            "json": f"/download/{json_out.name}",
            "excel": f"/download/{xlsx_out.name}",
        }
        JOBS[job_id]["message"] = "Completed"
        JOBS[job_id]["status"] = "done"
        set_progress(job_id, pct=100, stage="done", note="Completed")
        save_job_state(job_id)
        print(f"[{job_id}] Background job started successfully on thread")
    except Exception as e:
        import traceback
        traceback.print_exc()
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["message"] = str(e)
        set_progress(job_id, stage="error", note=str(e))
        save_job_state(job_id)

@app.get("/status/{job_id}", response_model=JobStatus)
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        job = load_job_state(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job")
    if "progress" not in job:
        job["progress"] = dict(DEFAULT_PROGRESS)
    return JobStatus(**job)

@app.get(
    "/download/{filename}",
)
def download(filename: str):
    path = OUTPUTS / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if filename.endswith(".xlsx") else "application/json"
    return FileResponse(path, media_type=media, filename=filename)

@app.delete(
    "/cleanup",
)
def cleanup():
    # basic retention policy
    cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
    removed = []
    for folder in (UPLOADS, OUTPUTS):
        for p in folder.iterdir():
            if p.is_file():
                if datetime.utcfromtimestamp(p.stat().st_mtime) < cutoff:
                    p.unlink()
                    removed.append(str(p.name))
    return {"removed": removed}
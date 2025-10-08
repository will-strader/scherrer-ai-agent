import os
import uuid
import json
from pathlib import Path
from datetime import datetime, timedelta
import asyncio

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Depends, Header
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from backend.models import ProcessResponse, JobStatus
from backend.extractor import extract_answers, extract_answers_async
from backend.writer import fill_template
from backend.mapping import Mapping
from backend.config import MAPPING_CSV, EXCEL_TEMPLATE

load_dotenv()

# --- API Key check system ---
API_KEY = os.getenv("API_KEY")
if API_KEY is None:
    print("[config] WARNING: No API_KEY set — authentication disabled")
else:
    print("[config] API authentication enabled")

# TEMPORARY: disable frontend token authentication
# TODO: re-enable when production-ready
def verify_frontend_token(request: Request):
    return True

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "storage" / "uploads"
OUTPUTS = BASE / "storage" / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

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

@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>AI Bid Assistant Backend</h3><p>POST /process with a PDF to get started.</p>"

@app.get("/ping")
def ping():
    return {"ok": True}

@app.post(
    "/process",
    response_model=ProcessResponse,
    dependencies=[Depends(verify_key)],
)
async def process_pdf(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a .pdf")

    job_id = str(uuid.uuid4())
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    pdf_name = f"{ts}__{job_id}__{file.filename}"
    pdf_path = UPLOADS / pdf_name
    with pdf_path.open("wb") as f:
        f.write(await file.read())

    # initialize job
    JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "output_paths": {},
        "message": None,
    }

    asyncio.create_task(_process_job(job_id, pdf_path))
    return ProcessResponse(job_id=job_id, status="queued")

async def _process_job(job_id: str, pdf_path: Path):
    try:
        JOBS[job_id]["status"] = "processing"
        JOBS[job_id]["message"] = "Loading mapping"
        print(f"[{job_id}] Loading mapping")

        # 1) load mapping (handles your Numbers/CSV quirks)
        mapping = load_mapping(MAPPING_CSV)
        JOBS[job_id]["message"] = "Mapping loaded"
        print(f"[{job_id}] Mapping loaded")

        # 2) extract answers from the PDF using OpenAI (returns dict keyed by json_key)
        JOBS[job_id]["message"] = "Extracting answers from PDF"
        print(f"[{job_id}] Extracting answers from PDF")
        raw_answers = await extract_answers_async(pdf_path, mapping)
        JOBS[job_id]["message"] = "Extraction complete"
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
        print(f"[{job_id}] Writing JSON results")

        # 3) write raw JSON for debugging/auditing
        json_out = OUTPUTS / f"{pdf_path.stem}__{job_id}.json"
        json_out.write_text(json.dumps(structured_answers, indent=2))
        JOBS[job_id]["message"] = "JSON results written"
        print(f"[{job_id}] JSON results written")

        # 4) fill the real Excel template (preserves formatting/formulas)
        JOBS[job_id]["message"] = "Filling Excel template"
        print(f"[{job_id}] Filling Excel template")
        fill_template(mapping, structured_answers, xlsx_out := OUTPUTS / f"{pdf_path.stem}__{job_id}.xlsx")
        JOBS[job_id]["message"] = "Excel template filled"
        print(f"[{job_id}] Excel template filled")

        JOBS[job_id]["output_paths"] = {
            "json": f"/download/{json_out.name}",
            "excel": f"/download/{xlsx_out.name}",
        }
        JOBS[job_id]["message"] = "Completed"
        JOBS[job_id]["status"] = "done"
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["message"] = str(e)

@app.get("/status/{job_id}", response_model=JobStatus)
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job")
    return JobStatus(**job)

@app.get(
    "/download/{filename}",
    dependencies=[Depends(verify_key)],
)
def download(filename: str):
    path = OUTPUTS / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if filename.endswith(".xlsx") else "application/json"
    return FileResponse(path, media_type=media, filename=filename)

@app.delete(
    "/cleanup",
    dependencies=[Depends(verify_key)],
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
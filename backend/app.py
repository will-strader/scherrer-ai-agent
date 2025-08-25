import os
import uuid
import json
from pathlib import Path
from datetime import datetime, timedelta

from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from .models import ProcessResponse, JobStatus
from .extractor import extract_answers
from .writer import fill_template
from .mapping import load_mapping
from .config import MAPPING_CSV, EXCEL_TEMPLATE

load_dotenv()

BASE = Path(__file__).resolve().parent
UPLOADS = BASE / "storage" / "uploads"
OUTPUTS = BASE / "storage" / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))

app = FastAPI(title="AI Bid Assistant (MVP)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# In-memory job registry (good enough for local dev)
JOBS = {}  # job_id -> JobStatus

@app.get("/", response_class=HTMLResponse)
def home():
    return "<h3>AI Bid Assistant Backend</h3><p>POST /process with a PDF to get started.</p>"

@app.post("/process", response_model=ProcessResponse)
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

    background_tasks.add_task(_process_job, job_id, pdf_path)
    return ProcessResponse(job_id=job_id, status="queued")

def _process_job(job_id: str, pdf_path: Path):
    try:
        JOBS[job_id]["status"] = "processing"

        # 1) load mapping (handles your Numbers/CSV quirks)
        mapping = load_mapping(MAPPING_CSV)

        # 2) extract answers from the PDF using OpenAI (returns dict keyed by json_key)
        answers = extract_answers(pdf_path, mapping)

        # 3) write raw JSON for debugging/auditing
        json_out = OUTPUTS / f"{pdf_path.stem}__{job_id}.json"
        json_out.write_text(json.dumps(answers, indent=2))

        # 4) fill the real Excel template (preserves formatting/formulas)
        xlsx_out = OUTPUTS / f"{pdf_path.stem}__{job_id}.xlsx"
        fill_template(EXCEL_TEMPLATE, mapping, answers, xlsx_out)

        JOBS[job_id]["output_paths"] = {
            "json": f"/download/{json_out.name}",
            "excel": f"/download/{xlsx_out.name}",
        }
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

@app.get("/download/{filename}")
def download(filename: str):
    path = OUTPUTS / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if filename.endswith(".xlsx") else "application/json"
    return FileResponse(path, media_type=media, filename=filename)

@app.delete("/cleanup")
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
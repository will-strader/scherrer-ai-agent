from pydantic import BaseModel, Field
from typing import Optional, Dict

class ProcessResponse(BaseModel):
    job_id: str
    status: str = Field(default="queued")

class ExtractResult(BaseModel):
    # placeholder schema; later we’ll replace with your real keys
    project_name: Optional[str] = None
    bid_due_date: Optional[str] = None
    bid_bond_pct: Optional[float] = None
    notes: Optional[str] = None
    raw_preview: Optional[str] = None  # first few KB of text for debugging

class JobStatus(BaseModel):
    job_id: str
    status: str                   # queued | processing | done | error
    output_paths: Dict[str, str]  # e.g. {"excel": "/download/..", "json": "/download/.."}
    message: Optional[str] = None
from pydantic import BaseModel, Field
from typing import Optional, Dict

class ProcessResponse(BaseModel):
    job_id: str
    status: str = Field(default="queued")

class JobStatus(BaseModel):
    job_id: str
    status: str                   # queued | processing | done | error
    output_paths: Dict[str, str]  # e.g. {"excel": "/download/..", "json": "/download/.."}
    message: Optional[str] = None
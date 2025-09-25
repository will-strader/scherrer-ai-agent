import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

BASE = Path(__file__).resolve().parent


UPLOADS = BASE / "storage" / "uploads"
OUTPUTS = BASE / "storage" / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_PROJECT = os.getenv("OPENAI_PROJECT", "").strip()
MODEL_NAME     = os.getenv("MODEL_NAME", "gpt-4o-mini").strip()
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))

# Explicitly anchor repo root to the folder containing backend/
REPO_ROOT = (Path(__file__).resolve().parent / "..").resolve()
print(f"[config] Repo root resolved to: {REPO_ROOT}")

def _resolve_repo_path(val: str, default_name: str) -> Path:
    """Resolve a path that may be absolute or relative to the repo root."""
    if not val or val.strip() == "":
        p = REPO_ROOT / default_name
    else:
        p = Path(val)
        if not p.is_absolute():
            p = (REPO_ROOT / p).resolve()
    return p.resolve()

MAPPING_CSV    = _resolve_repo_path(os.getenv("MAPPING_CSV") or "", "question_mapping_template.csv")
EXCEL_TEMPLATE = _resolve_repo_path(os.getenv("EXCEL_TEMPLATE") or "", "bid checklist.xlsx")

print(f"[config] Using model: {MODEL_NAME} (temperature={MODEL_TEMPERATURE:.2f})")
print(f"[config] Mapping CSV: {MAPPING_CSV}")
print(f"[config] Excel template: {EXCEL_TEMPLATE}")

# Sanity checks
if not OPENAI_API_KEY:
    print("[config] ERROR: OPENAI_API_KEY is empty. Set it in backend/.env")
elif OPENAI_API_KEY.startswith("sk-admin--"):
    print("[config] ERROR: Admin keys (sk-admin--) are not valid for API calls. Use sk-proj- or sk-svcacct- with OPENAI_PROJECT.")
elif OPENAI_API_KEY.startswith(("sk-proj-", "sk-svcacct-")):
    if not OPENAI_PROJECT:
        print("[config] ERROR: Detected project/service-account key but OPENAI_PROJECT is not set. Requests will 401.")

# OpenAI client (singleton)
if OPENAI_PROJECT:
    CLIENT = OpenAI(api_key=OPENAI_API_KEY, project=OPENAI_PROJECT)
else:
    CLIENT = OpenAI(api_key=OPENAI_API_KEY)
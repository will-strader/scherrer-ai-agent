import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

BASE = Path(__file__).resolve().parent

# Where uploads/outputs live
UPLOADS = BASE / "storage" / "uploads"
OUTPUTS = BASE / "storage" / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

# --- Env ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_PROJECT = os.getenv("OPENAI_PROJECT", "").strip()
MODEL_NAME     = os.getenv("MODEL_NAME", "gpt-5.1-mini").strip()
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))

# These can be absolute or relative to backend/
MAPPING_CSV    = Path(os.getenv("MAPPING_CSV", BASE / "../question_mapping_template.csv")).resolve()
EXCEL_TEMPLATE = Path(os.getenv("EXCEL_TEMPLATE", BASE / "../bid checklist.xlsx")).resolve()

print(f"[config] Using model: {MODEL_NAME}")
print(f"[config] Mapping CSV: {MAPPING_CSV}")
print(f"[config] Excel template: {EXCEL_TEMPLATE}")

# --- Sanity checks ---
if not OPENAI_API_KEY:
    print("[config] ERROR: OPENAI_API_KEY is empty. Set it in backend/.env")
elif OPENAI_API_KEY.startswith("sk-admin--"):
    print("[config] ERROR: Admin keys (sk-admin--) are not valid for API calls. Use sk-proj- or sk-svcacct- with OPENAI_PROJECT.")
elif OPENAI_API_KEY.startswith("sk-proj-") or OPENAI_API_KEY.startswith("sk-svcacct-"):
    if not OPENAI_PROJECT:
        print("[config] ERROR: Detected project/service-account key but OPENAI_PROJECT is not set. Requests will 401.")

# --- OpenAI client (singleton) ---
if OPENAI_PROJECT:
    CLIENT = OpenAI(api_key=OPENAI_API_KEY, project=OPENAI_PROJECT)
else:
    CLIENT = OpenAI(api_key=OPENAI_API_KEY)
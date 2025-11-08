import os
from pathlib import Path
import sys
from dotenv import load_dotenv
from openai import OpenAI

try:
    import keyring  # optional, for Windows Credential Manager
except Exception:
    keyring = None

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

BASE = Path(__file__).resolve().parent

# Version & server defaults
BACKEND_VERSION = os.getenv("BACKEND_VERSION", "v1.0.0").strip()
HOST = os.getenv("HOST", "127.0.0.1").strip()
PORT = int(os.getenv("PORT", "18000").strip() or "18000")

UPLOADS = BASE / "storage" / "uploads"
OUTPUTS = BASE / "storage" / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

# Processing knobs (used by extractor/app; safe if unused)
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
MIN_CONCURRENCY = int(os.getenv("MIN_CONCURRENCY", "1"))
CHUNK_BATCH_BYTES = int(os.getenv("CHUNK_BATCH_BYTES", "2000000"))  # ~2MB per request target

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_PROJECT = os.getenv("OPENAI_PROJECT", "").strip()
MODEL_NAME     = os.getenv("MODEL_NAME", "gpt-4o-mini").strip()
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))
FRONTEND_ACCESS_TOKEN = os.getenv("FRONTEND_ACCESS_TOKEN", "").strip()

# Try Windows Credential Manager if no key in env
if not OPENAI_API_KEY and sys.platform == "win32" and keyring:
    try:
        stored = keyring.get_password("AI Bid Assistant", "openai_api_key")
        if stored:
            OPENAI_API_KEY = stored.strip()
            print("[config] OPENAI_API_KEY loaded from Windows Credential Manager")
    except Exception as e:
        print(f"[config] WARNING: keyring lookup failed: {e}")

# Explicitly anchor repo root to the folder containing backend/
REPO_ROOT = (Path(__file__).resolve().parent / "..").resolve()
print(f"[config] Repo root resolved to: {REPO_ROOT}")
print(f"[config] AI Bid Assistant backend {BACKEND_VERSION} initialized")
print(f"[config] Using model: {MODEL_NAME} (temperature={MODEL_TEMPERATURE:.2f})")
print(f"[config] Server: http://{HOST}:{PORT}")

def _resolve_repo_path(val: str, default_name: str) -> Path:
    """Resolve a path that may be absolute or relative to the repo root."""
    if not val or val.strip() == "":
        p = REPO_ROOT / default_name
    else:
        p = Path(val)
        if not p.is_absolute():
            p = (REPO_ROOT / p).resolve()
    return p.resolve()

MAPPING_CSV    = _resolve_repo_path(os.getenv("MAPPING_CSV") or "", "question_mapping_template_new.csv")
EXCEL_TEMPLATE = _resolve_repo_path(os.getenv("EXCEL_TEMPLATE") or "", "bid checklist.xlsx")

print(f"[config] Mapping CSV: {MAPPING_CSV}")
print(f"[config] Excel template: {EXCEL_TEMPLATE}")
print(f"[config] Concurrency: MIN={MIN_CONCURRENCY} MAX={MAX_CONCURRENCY}, target batch bytes={CHUNK_BATCH_BYTES}")

if FRONTEND_ACCESS_TOKEN:
    print(f"[config] Frontend access token loaded ({len(FRONTEND_ACCESS_TOKEN)} chars)")
else:
    print("[config] WARNING: FRONTEND_ACCESS_TOKEN not set — frontend authentication disabled")

REQUIRE_API_AUTH = os.getenv("REQUIRE_API_AUTH", "").strip().lower() in {"1","true","yes"}
print(f"[config] API authentication {'enabled' if REQUIRE_API_AUTH else 'disabled'}")

def _assert_exists(path: Path, label: str):
    if not path.exists():
        print(f"[config] WARNING: {label} not found at {path}.")
_assert_exists(MAPPING_CSV, "Mapping CSV")
_assert_exists(EXCEL_TEMPLATE, "Excel template")

# Sanity checks
if not OPENAI_API_KEY:
    print(f"[config] ERROR: OPENAI_API_KEY is empty. Set it in backend/.env or Windows Credential Manager")
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
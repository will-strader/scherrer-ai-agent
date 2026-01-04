import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent

# In dev we load backend/.env; in packaged Tauri builds we may place `.env` in the resources root.
# Try the most likely locations in order.
_def_env_paths = []
_tauri_res = (os.getenv("TAURI_RESOURCES_DIR") or "").strip()
if _tauri_res:
    _def_env_paths.append(Path(_tauri_res) / ".env")
    _def_env_paths.append(Path(_tauri_res) / "resources" / ".env")
_def_env_paths.append(BASE / ".env")

_loaded_env = False
for _p in _def_env_paths:
    try:
        if _p.exists():
            load_dotenv(dotenv_path=_p)
            print(f"[config] Loaded .env from: {_p}")
            _loaded_env = True
            break
    except Exception:
        pass
if not _loaded_env:
    # No .env found; proceed with environment variables only.
    load_dotenv(dotenv_path=BASE / ".env")

# Version & server defaults
BACKEND_VERSION = os.getenv("BACKEND_VERSION", "v1.0.0").strip()
HOST = os.getenv("HOST", "127.0.0.1").strip()
PORT = int(os.getenv("PORT", "18000").strip() or "18000")

# Storage directories
# IMPORTANT: Do NOT write to the installation directory on Windows (e.g., Program Files).
# Use a per-user data directory when packaged.
APP_NAME = (os.getenv("APP_NAME") or "Scherrer Bid Assistant").strip() or "Scherrer Bid Assistant"

# --- User config (plain text) ---
# Stored in a per-user directory (DATA_DIR) so installs under Program Files remain read-only.
# Example path on Windows:
#   %LOCALAPPDATA%\Scherrer Bid Assistant\config.json
CONFIG_FILE: Path | None = None


def _read_json(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_user_config() -> dict:
    if CONFIG_FILE is None:
        return {}
    return _read_json(CONFIG_FILE)


def save_user_config(cfg: dict) -> None:
    if CONFIG_FILE is None:
        raise RuntimeError("CONFIG_FILE not initialized")
    _atomic_write_json(CONFIG_FILE, cfg)


def set_openai_api_key(api_key: str) -> None:
    """Persist the OpenAI API key in the user config file (plain text).

    NOTE: This is a convenience for local desktop use. Do not commit keys into the repo."""
    key = (api_key or "").strip()
    cfg = load_user_config()
    if key:
        cfg["openai_api_key"] = key
    else:
        cfg.pop("openai_api_key", None)
    save_user_config(cfg)


def get_openai_api_key() -> str:
    """Resolve the OpenAI API key.

    Precedence:
      1) Environment variable OPENAI_API_KEY
      2) Per-user config file (CONFIG_FILE)
    """
    env = (os.getenv("OPENAI_API_KEY") or "").strip()
    if env:
        return env
    cfg = load_user_config()
    val = (cfg.get("openai_api_key") or "").strip() if isinstance(cfg, dict) else ""
    return val


def _default_user_data_dir() -> Path:
    # Allow explicit override for debugging / enterprise deployments.
    override = (os.getenv("SCHERRER_DATA_DIR") or os.getenv("APP_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform == "win32":
        base = (os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or "").strip()
        if base:
            return (Path(base) / APP_NAME).resolve()
        return (Path.home() / "AppData" / "Local" / APP_NAME).resolve()

    # macOS/Linux
    return (Path.home() / f".{APP_NAME.lower().replace(' ', '-')}").resolve()


def _should_use_user_data_dir() -> bool:
    # If running in a packaged context, prefer user data dir.
    if (os.getenv("TAURI_RESOURCES_DIR") or "").strip():
        return True
    if getattr(sys, "frozen", False):
        return True
    # If installed under Program Files, we definitely cannot write there as a normal user.
    if sys.platform == "win32" and "program files" in str(BASE).lower():
        return True
    # If the repo/app directory isn't writable, fall back.
    try:
        return not os.access(str(BASE), os.W_OK)
    except Exception:
        return True


if _should_use_user_data_dir():
    DATA_DIR = _default_user_data_dir()
    STORAGE_ROOT = DATA_DIR / "storage"
else:
    DATA_DIR = BASE
    STORAGE_ROOT = BASE / "storage"

# Now that we know DATA_DIR, define the per-user config file location.
CONFIG_FILE = (DATA_DIR / "config.json").resolve()

UPLOADS = STORAGE_ROOT / "uploads"
OUTPUTS = STORAGE_ROOT / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)
print(f"[config] Storage root: {STORAGE_ROOT}")

# Processing knobs (used by extractor/app; safe if unused)
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
MIN_CONCURRENCY = int(os.getenv("MIN_CONCURRENCY", "1"))
CHUNK_BATCH_BYTES = int(os.getenv("CHUNK_BATCH_BYTES", "2000000"))  # ~2MB per request target

OPENAI_API_KEY = get_openai_api_key()
OPENAI_PROJECT = os.getenv("OPENAI_PROJECT", "").strip()
MODEL_NAME     = os.getenv("MODEL_NAME", "gpt-4o-mini").strip()
MODEL_TEMPERATURE = float(os.getenv("MODEL_TEMPERATURE", "0"))
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))
FRONTEND_ACCESS_TOKEN = os.getenv("FRONTEND_ACCESS_TOKEN", "").strip()

# Resolve the base path for bundled assets.
# - In dev: repo root (folder containing backend/)
# - In packaged builds: TAURI_RESOURCES_DIR (set by the Tauri wrapper)
_tauri_res_root = (os.getenv("TAURI_RESOURCES_DIR") or "").strip()
if _tauri_res_root:
    REPO_ROOT = Path(_tauri_res_root).resolve()
    print(f"[config] Resources root resolved to: {REPO_ROOT}")
else:
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
EXCEL_TEMPLATE = _resolve_repo_path(os.getenv("EXCEL_TEMPLATE") or "", "bid_checklist.xlsx")

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
    print("[config] WARNING: OPENAI_API_KEY is empty. The backend will start, but requests that use OpenAI will fail until a key is provided.")
elif OPENAI_API_KEY.startswith("sk-admin--"):
    print("[config] ERROR: Admin keys (sk-admin--) are not valid for API calls. Use sk-proj- or sk-svcacct- with OPENAI_PROJECT.")
elif OPENAI_API_KEY.startswith(("sk-proj-", "sk-svcacct-")):
    if not OPENAI_PROJECT:
        print("[config] ERROR: Detected project/service-account key but OPENAI_PROJECT is not set. Requests will 401.")

# OpenAI client
# IMPORTANT: Do not initialize the client at import time.
# If the API key is missing, the backend should still boot and only error when a request
# actually needs OpenAI.
CLIENT = None


def get_openai_client():
    """Return an OpenAI client, creating it on demand."""
    global CLIENT
    if CLIENT is not None:
        return CLIENT

    api_key = get_openai_api_key()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Add it in the app Settings (recommended) or set it as an environment variable."
        )

    # Import lazily so config import never fails in packaged builds.
    from openai import OpenAI  # type: ignore

    if OPENAI_PROJECT:
        CLIENT = OpenAI(api_key=api_key, project=OPENAI_PROJECT)
    else:
        CLIENT = OpenAI(api_key=api_key)
    return CLIENT
# run_backend.py
"""Entry-point to run the local FastAPI backend for the desktop app.

Designed to work well on Windows/macOS/Linux and when frozen by PyInstaller.
It binds ONLY to localhost and uses a fixed default port (18000) unless
overridden by env vars.
"""
from __future__ import annotations

import os
import sys
import asyncio
import logging
from pathlib import Path
from multiprocessing import freeze_support

import uvicorn

# Robust import of the FastAPI app
# In dev, `backend/` is a normal package.
# In PyInstaller onedir builds, the executable lives next to `_internal/` and
# we may be launched with a working directory that is not the package root.
# Ensure common roots are on sys.path, then import `backend.app`.

def _ensure_import_paths() -> None:
    candidates: list[Path] = []

    # Repo/dev: .../backend/run_backend.py -> add repo root
    try:
        candidates.append(Path(__file__).resolve().parent.parent)
    except Exception:
        pass

    # PyInstaller onefile uses _MEIPASS; onedir often doesn't need it but it's safe.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass).resolve())

    # Executable directory (PyInstaller onedir)
    try:
        candidates.append(Path(sys.executable).resolve().parent)
    except Exception:
        pass

    # Current working directory (Tauri may set this to resources root)
    try:
        candidates.append(Path.cwd().resolve())
    except Exception:
        pass

    for p in candidates:
        if p and p.exists():
            sp = str(p)
            if sp not in sys.path:
                sys.path.insert(0, sp)


_ensure_import_paths()

try:
    from backend.app import app  # type: ignore
except Exception as e:  # pragma: no cover
    raise RuntimeError(f"Unable to import FastAPI app (backend.app): {e}")


# --- Platform quirks (especially Windows) ----------------------------------
if sys.platform.startswith("win"):
    # Ensure a compatible event loop on older Python/Windows combos
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass


# --- Logging to a user-writable directory ----------------------------------
def _setup_logging() -> Path:
    # IMPORTANT: Do not write logs next to the executable if installed under
    # Program Files. Use a per-user directory.
    app_name = (os.getenv("APP_NAME") or "Scherrer Bid Assistant").strip() or "Scherrer Bid Assistant"

    override = (os.getenv("SCHERRER_DATA_DIR") or os.getenv("APP_DATA_DIR") or "").strip()
    if override:
        base_dir = Path(override).expanduser().resolve()
    elif sys.platform.startswith("win"):
        base = (os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or "").strip()
        base_dir = (Path(base) / app_name).resolve() if base else (Path.home() / "AppData" / "Local" / app_name).resolve()
    else:
        base_dir = (Path.home() / f".{app_name.lower().replace(' ', '-')}").resolve()

    log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "backend.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def main() -> None:
    log_file = _setup_logging()
    logging.getLogger(__name__).info("Writing logs to %s", log_file)

    host = os.getenv("BID_ASSISTANT_HOST", "127.0.0.1")  # localhost only
    port = int(os.getenv("BID_ASSISTANT_PORT", "18000"))

    # Build an explicit Uvicorn config so we can control signal handling
    # (desktop shells like Tauri manage their own) and keep things single-process.
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        workers=1,               # single process for desktop usage
        log_level="info",
        access_log=False,
        lifespan="on",
        loop="asyncio",
        http="h11",
        timeout_keep_alive=65,
        # No SSL here — desktop app talks to 127.0.0.1 only.
    )

    server = uvicorn.Server(config)
    # Run inside our own event loop (no signal handlers for PyInstaller/Windows)
    asyncio.run(server.serve())


if __name__ == "__main__":
    freeze_support()  # important for Windows + PyInstaller
    main()
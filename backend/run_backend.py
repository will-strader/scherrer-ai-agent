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
# When frozen (PyInstaller), modules may be laid out differently. We try the
# package import first, then fall back to a relative import.
try:
    from backend.app import app  # type: ignore
except Exception:  # pragma: no cover
    # If frozen, __file__ can be inside a temp dir; _MEIPASS points to bundle.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()
    sys.path.insert(0, str(base))
    try:
        from app import app  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"Unable to import FastAPI app: {e}")


# --- Platform quirks (especially Windows) ----------------------------------
if sys.platform.startswith("win"):
    # Ensure a compatible event loop on older Python/Windows combos
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass


# --- Logging to a rotating file next to the executable ---------------------
def _setup_logging() -> Path:
    exe_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve()).parent)
    log_dir = exe_dir / "logs"
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
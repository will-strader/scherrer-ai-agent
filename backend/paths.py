# backend/paths.py
import os, sys
def app_dir():
    # Works for normal run and PyInstaller (_MEIPASS)
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    # If assets live beside the exe, use os.path.dirname(sys.executable)
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else base

def resolve_asset(name: str) -> str:
    return os.path.join(app_dir(), name)
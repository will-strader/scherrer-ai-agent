import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE = Path(__file__).resolve().parent

# Where uploads/outputs live (app.py already uses these too — that's fine)
UPLOADS = BASE / "storage" / "uploads"
OUTPUTS = BASE / "storage" / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

# Env/config
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
MODEL_NAME     = os.getenv("MODEL_NAME", "gpt-5")
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "60"))

# Paths to your artifacts (adjust as needed)
# These can be absolute or relative to this file
MAPPING_CSV    = Path(os.getenv("MAPPING_CSV", BASE / "../question_mapping_template.csv")).resolve()
EXCEL_TEMPLATE = Path(os.getenv("EXCEL_TEMPLATE", BASE / "../bid checklist.xlsx")).resolve()
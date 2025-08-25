from pathlib import Path
from openpyxl import Workbook
from .models import ExtractResult

# ---- Minimal writer ----
# Creates a basic Excel file with whatever fields we have today.
# Later we’ll switch this to "fill your template.xlsx with openpyxl".

def write_excel(result: ExtractResult, out_path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Bid Info"

    ws["A1"] = "Project Name"
    ws["B1"] = result.project_name or ""

    ws["A2"] = "Bid Due Date"
    ws["B2"] = result.bid_due_date or ""

    ws["A3"] = "Bid Bond %"
    ws["B3"] = result.bid_bond_pct if result.bid_bond_pct is not None else ""

    ws["A5"] = "Notes"
    ws["B5"] = result.notes or ""

    ws["A7"] = "Raw Preview (first 4k chars)"
    ws["A8"] = result.raw_preview or ""

    wb.save(out_path)
    return out_path
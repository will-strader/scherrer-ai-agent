from __future__ import annotations
from pathlib import Path
from typing import Any, Dict
from datetime import datetime
from openpyxl import load_workbook

from .mapping import Mapping

def _try_parse_number(value: Any) -> Any:
  if value is None:
    return None
  if isinstance(value, (int, float)):
    return value
  s = str(value).strip()
  if not s:
    return None
  # Remove currency symbols and commas
  s = s.replace("$", "").replace(",", "").replace("%", "")
  try:
    if "." in s:
      return float(s)
    return int(s)
  except Exception:
    return value  # fall back to original

def _try_parse_date(value: Any):
  """Return a python date for common formats so Excel can format it nicely, else return original."""
  if value is None:
    return None
  if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
    return value
  s = str(value).strip()
  if not s:
    return None
  for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%m-%d-%Y"):
    try:
      return datetime.strptime(s, fmt).date()
    except Exception:
      continue
  # If parsing fails, just return the original string
  return s

def _normalize_yesno(value: Any) -> str | None:
  if value is None:
    return None
  s = str(value).strip().lower()
  if s in ("yes", "y", "true", "1"):
    return "Yes"
  if s in ("no", "n", "false", "0"):
    return "No"
  # Leave as-is if it's already something like "Yes"/"No" with different casing
  if s:
    return "Yes" if s == "yes" else ("No" if s == "no" else str(value))
  return None

def _coerce_for_cell(answer_value: Any, answer_type: str):
  at = (answer_type or "text").lower().strip()
  if at == "date":
    return _try_parse_date(answer_value)
  if at in ("number", "currency"):
    return _try_parse_number(answer_value)
  if at == "yesno":
    return _normalize_yesno(answer_value)
  if at == "list":
    if isinstance(answer_value, list):
      return ", ".join([str(x) for x in answer_value])
    return str(answer_value) if answer_value is not None else None
  # text, email, phone, default
  return answer_value if answer_value is not None else None

def fill_template(excel_template: Path, mapping: Mapping, answers: Dict[str, Any], out_path: Path) -> Path:
  """
  Open the real Excel template, write values for all mapping rows marked as questions,
  preserve formatting/formulas, and save to out_path.
  """
  wb = load_workbook(excel_template, data_only=False, keep_vba=False)

  # Iterate over mapping rows and drop values into the appropriate cells
  for row in mapping.question_rows:
    key = row.json_key
    if not key:
      continue
    value = answers.get(key, None)
    value = _coerce_for_cell(value, row.answer_type)

    # Pick sheet (fallback to first sheet if mapping name not found)
    sheet_name = row.sheet if row.sheet in wb.sheetnames else wb.sheetnames[0]
    ws = wb[sheet_name]

    if not row.cell:
      # If a cell wasn't specified, skip safely
      continue
    try:
      ws[row.cell].value = value
      # If it's a parsed date, set a simple date number format so it renders nicely
      if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
        ws[row.cell].number_format = "mm/dd/yyyy"
    except Exception:
      # Don't fail the whole job if one mapping cell is off
      continue

  # Ensure parent directory exists and save
  out_path = Path(out_path)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  wb.save(out_path)
  return out_path
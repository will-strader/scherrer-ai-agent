import pdfplumber
from pathlib import Path
from .models import ExtractResult

# ---- Minimal stub extractor ----
# For now: read PDF text, return a tiny structured result.
# Later: replace with chunking + OpenAI extraction that fills all json_keys.

def extract_from_pdf(pdf_path: Path) -> ExtractResult:
    texts = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            try:
                txt = page.extract_text() or ""
            except Exception:
                txt = ""
            texts.append(txt)
            if i >= 25:  # cap preview work on huge PDFs during dev
                break
    doc_text = "\n".join(texts)
    preview = doc_text[:4000]

    # dumb heuristics for now; just placeholders:
    project_name = None
    bid_due_date = None
    bid_bond_pct = None

    # Return the placeholder structure
    return ExtractResult(
        project_name=project_name,
        bid_due_date=bid_due_date,
        bid_bond_pct=bid_bond_pct,
        notes="Stub extraction. Replace with AI JSON mapping later.",
        raw_preview=preview,
    )
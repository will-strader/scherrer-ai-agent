from __future__ import annotations
from pathlib import Path
import re
import json
import pdfplumber
from typing import List, Dict
from .config import MODEL_NAME, CLIENT
from .mapping import Mapping


def _read_pdf_text(pdf_path: Path, max_pages: int | None = None) -> List[str]:
    """Extract text per page using pdfplumber. Returns a list of page texts."""
    texts: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        limit = min(n, max_pages) if max_pages else n
        for i in range(limit):
            try:
                t = pdf.pages[i].extract_text() or ""
            except Exception:
                t = ""
            # normalize whitespace and drop extra spaces
            t = re.sub(r"[ \t]+", " ", t)
            texts.append(t)
    return texts

def _chunk_text(pages: List[str], target_chars: int = 12000) -> List[str]:
    """Group pages into chunks of ~target_chars to keep prompts small."""
    chunks: List[str] = []
    buf = ""
    for p in pages:
        if len(buf) + len(p) + 1 > target_chars:
            if buf:
                chunks.append(buf)
            buf = p
        else:
            buf += ("\n" if buf else "") + p
    if buf:
        chunks.append(buf)
    return chunks

def _build_instructions(mapping: Mapping) -> str:
    lines = [
        "You are extracting answers for a construction bid checklist.",
        "Return ONLY a single JSON object whose keys exactly match the provided keys.",
        "If an answer is not explicitly present in the text, set it to null or an empty string (for text). Do not invent.",
        "Dates should be YYYY-MM-DD if only a date is present. Yes/No fields must be 'Yes' or 'No'.",
    ]
    return "\n".join(lines)

def extract_answers(pdf_path: Path, mapping: Mapping) -> Dict[str, object]:
    """
    Read the PDF, send relevant chunks to the model, and return a dict keyed by mapping.json_keys().
    """
    # 1) Read and chunk PDF text
    pages = _read_pdf_text(pdf_path)
    chunks = _chunk_text(pages, target_chars=12000)

    # 2) Build prompt
    keys = mapping.json_keys()
    system_msg = _build_instructions(mapping)

    # Keep the first few chunks to start (we can get smarter later)
    used_chunks = chunks[:5] if chunks else [""]

    messages = [{"role": "system", "content": system_msg}]
    messages.append({
        "role": "user",
        "content": (
            "Answer ONLY these keys: " + ", ".join(keys) +
            "\nUse ONLY the document text below. If unknown, use null/empty string.\n\n" +
            "\n\n---\n\n".join(used_chunks)
        )
    })

    # 3) Call OpenAI for a strict JSON object
    resp = CLIENT.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=0,
        response_format={"type": "json_object"}
    )

    raw = resp.choices[0].message.content
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    # 4) Ensure all requested keys are present (fill missing with None)
    out = {k: data.get(k, None) for k in keys}
    return out
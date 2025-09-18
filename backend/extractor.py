from __future__ import annotations
from pathlib import Path
import re
import json
import pdfplumber
from typing import List, Dict
from .config import MODEL_NAME
from openai import AsyncOpenAI
import random, time
async_client = AsyncOpenAI()
from .mapping import Mapping
import asyncio
import sys


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

async def extract_answers_async(pdf_path: Path, mapping: Mapping, job_status: dict | None = None) -> Dict[str, object]:
    """
    Read the PDF, send relevant chunks to the model, and return a dict keyed by mapping.json_keys().
    """
    # 1) Read and chunk PDF text
    pages = _read_pdf_text(pdf_path)
    chunks = _chunk_text(pages, target_chars=12000)

    # 2) Build prompt
    keys = mapping.json_keys()
    system_msg = _build_instructions(mapping)

    # Initialize output dictionary with None for all keys
    out = {k: None for k in keys}

    semaphore = asyncio.Semaphore(3)

    async def _process_chunk(idx: int, chunk: str) -> Dict[str, object]:
        messages = [{"role": "system", "content": system_msg}]
        messages.append({
            "role": "user",
            "content": (
                "Answer ONLY these keys: " + ", ".join(keys) +
                "\nUse ONLY the document text below. If unknown, use null/empty string.\n\n" +
                chunk
            )
        })

        print(f"[extractor] Starting chunk {idx+1}/{len(chunks)}", file=sys.stderr)
        if job_status is not None:
            job_status["progress"] = f"Starting chunk {idx+1}/{len(chunks)}"
        attempt = 0
        max_attempts = 5
        while True:
            try:
                async with semaphore:
                    resp = await async_client.chat.completions.create(
                        model=MODEL_NAME,
                        messages=messages,
                        temperature=0,
                        response_format={"type": "json_object"}
                    )
                break
            except Exception as e:
                code = getattr(e, 'code', None)
                if (code == 429 or 'RateLimitError' in type(e).__name__) and attempt < max_attempts:
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    print(f"[extractor] Rate limit hit, retrying in {delay:.2f}s (attempt {attempt+1})", file=sys.stderr)
                    if job_status is not None:
                        job_status["progress"] = f"Rate limit hit, retrying chunk {idx+1} in {delay:.2f}s (attempt {attempt+1})"
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                else:
                    raise

        raw = resp.choices[0].message.content
        # Debug logging raw response truncated to 300 chars
        print(f"[extractor] Raw response chunk {idx+1} (truncated): {raw[:300]!r}", file=sys.stderr)

        try:
            data = json.loads(raw)
        except Exception:
            # Attempt to extract first JSON object substring with regex
            match = re.search(r"\{.*?\}", raw, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except Exception:
                    print(f"[extractor] Failed to parse JSON from extracted substring in chunk {idx+1}", file=sys.stderr)
                    print(f"[extractor] Raw response: {raw}", file=sys.stderr)
                    data = {}
            else:
                print(f"[extractor] Failed to parse JSON and no JSON substring found in chunk {idx+1}", file=sys.stderr)
                print(f"[extractor] Raw response: {raw}", file=sys.stderr)
                data = {}

        print(f"[extractor] Processing chunk {idx+1} of {len(chunks)}", file=sys.stderr)
        if job_status is not None:
            job_status["progress"] = f"Processing chunk {idx+1}/{len(chunks)}"
        return data

    async def _run_all():
        return await asyncio.gather(*[_process_chunk(i, chunk) for i, chunk in enumerate(chunks)])

    results = await _run_all()

    # Merge answers across all chunks, allowing later chunks to overwrite if the current value is empty/null.
    for data in results:
        for k in keys:
            val = data.get(k)
            if val is None or val == "" or (isinstance(val, str) and val.strip().lower() == "null"):
                continue
            # Update if current out[k] is still missing or null/blank/"null"
            if out[k] is None or out[k] == "" or (isinstance(out[k], str) and out[k].strip().lower() == "null"):
                out[k] = val

    if job_status is not None:
        job_status["result"] = out

    return out

def extract_answers(pdf_path: Path, mapping: Mapping, job_status: dict | None = None) -> Dict[str, object]:
    return asyncio.run(extract_answers_async(pdf_path, mapping, job_status))
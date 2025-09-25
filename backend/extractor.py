from __future__ import annotations
from pathlib import Path
import re
import json
import pdfplumber
from typing import List, Dict
from .config import MODEL_NAME, MODEL_TEMPERATURE
from openai import AsyncOpenAI
import random, time
async_client = AsyncOpenAI()
from .mapping import Mapping
import asyncio
import sys

# Ensure MODEL_TEMPERATURE is a valid float between 0 and 1, else default to 0.3
try:
    temp = float(MODEL_TEMPERATURE)
    if not (0 <= temp <= 1):
        raise ValueError
except Exception:
    temp = 0.3
MODEL_TEMPERATURE = temp

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
        "Each value must be an object with fields: 'answer' (string), 'confidence' (1-10 integer), and 'source' (short string, e.g. 'Page 12').",
        "If an answer is not explicitly present in the text, still guess the best possible answer. Never output 'not specified', 'unknown', or 'N/A'. Always provide your best inference (even low confidence 1-3). Keep answers concise, not long explanations.",
        "Keep 'source' under 10 words: usually just page number(s) like 'Page 12' or a short phrase like 'bid form'. Do not output long sentences in 'source'.",
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
                ". Each key must map to {\"answer\": ..., \"confidence\": ..., \"source\": ...}. Confidence must be 1-10, source should be page numbers or context reference.\n" +
                "Use ONLY the document text below.\n\n" +
                chunk
            )
        })

        print(f"[extractor] Starting chunk {idx+1}/{len(chunks)}", file=sys.stderr)
        print(f"[extractor] Using MODEL_TEMPERATURE={MODEL_TEMPERATURE}", file=sys.stderr)
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
                        temperature=MODEL_TEMPERATURE,
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

    # Merge answers across all chunks: parse each value as a dict with answer, confidence, source.
    def _normalize_answer(val):
        # Ensure val is a dict with answer, confidence, source
        answer = ""
        confidence = 3
        source = ""
        if isinstance(val, dict):
            answer = val.get("answer", "")
            try:
                confidence = int(val.get("confidence", 3))
            except Exception:
                confidence = 3
            if not (1 <= confidence <= 10):
                confidence = 3
            source = val.get("source", "")
            if not isinstance(source, str):
                source = str(source) if source is not None else ""
        elif isinstance(val, str):
            answer = val
        return {
            "answer": answer if isinstance(answer, str) else str(answer),
            "confidence": confidence,
            "source": source
        }

    for data in results:
        for k in keys:
            val = data.get(k)
            if val is None:
                continue
            val_obj = _normalize_answer(val)
            # If out[k] is missing, set directly
            if out[k] is None:
                out[k] = val_obj
                continue
            # If out[k] is not a dict (legacy/empty), replace if blank/None
            if not isinstance(out[k], dict):
                if out[k] is None or out[k] == "" or (isinstance(out[k], str) and out[k].strip().lower() == "null"):
                    out[k] = val_obj
                continue
            # If current answer is blank/None, replace
            if (out[k].get("answer") is None) or (isinstance(out[k].get("answer"), str) and out[k].get("answer").strip() == ""):
                out[k] = val_obj
                continue
            # Otherwise, prefer the answer with higher confidence
            if val_obj["confidence"] > out[k].get("confidence", 0):
                out[k] = val_obj
                continue
            # Otherwise, keep existing answer but merge sources
            existing_source = out[k].get("source", "")
            new_source = val_obj.get("source", "")
            if new_source and new_source not in existing_source:
                if existing_source.strip() == "":
                    out[k]["source"] = new_source
                else:
                    out[k]["source"] = existing_source + "; " + new_source
            continue

    # Ensure all keys have a structured answer object
    for k in keys:
        if out[k] is None:
            out[k] = {"answer": "", "confidence": 1, "source": ""}

    if job_status is not None:
        job_status["result"] = out

    return out

def extract_answers(pdf_path: Path, mapping: Mapping, job_status: dict | None = None) -> Dict[str, object]:
    return asyncio.run(extract_answers_async(pdf_path, mapping, job_status))
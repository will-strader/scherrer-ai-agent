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


# --- Adaptive concurrency fallback ---
async def _extract_with_limit(pdf_path: Path, mapping: Mapping, job_status: dict | None, max_concurrency: int) -> Dict[str, object]:
    """
    Core extractor that:
    - reads and chunks the PDF,
    - queries the model for each chunk with up to max_concurrency parallel tasks,
    - merges all partial answers into one dict.

    This DOES NOT retry with different concurrency. Caller handles retries.
    """

    # 1) Read and chunk PDF
    pages = _read_pdf_text(pdf_path)
    chunks = _chunk_text(pages, target_chars=12000)

    keys = mapping.json_keys()
    system_msg = _build_instructions(mapping)

    # Initialize output for merge later
    out = {k: None for k in keys}

    # limit parallel work
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _process_chunk(idx: int, chunk: str) -> Dict[str, object]:
        # Build answer type guidance
        type_guidance = "Answer each question using the correct type:\n"
        for row in mapping.question_rows:
            atype = getattr(row, "answer_type", "").lower().strip()
            key = getattr(row, "json_key", "").strip()
            if not key:
                continue
            if atype == "number":
                type_guidance += f"- {key}: numeric value only (e.g., 5 or 10.0).\n"
            elif atype == "currency":
                type_guidance += f"- {key}: numeric currency (e.g., 5000 or 120000).\n"
            elif atype in ("yes/no", "yesno"):
                type_guidance += f"- {key}: strictly 'Yes' or 'No'.\n"
            elif atype == "text":
                type_guidance += f"- {key}: short text or name only.\n"
            elif atype == "date":
                type_guidance += f"- {key}: use YYYY-MM-DD format.\n"

        messages = [
            {"role": "system", "content": system_msg},
            {
                "role": "user",
                "content": (
                    "Answer ONLY these keys: " + ", ".join(keys) +
                    ". Each key must map to {\"answer\": ..., \"confidence\": ..., \"source\": ...}. "
                    "Confidence must be 1-10, source should be page numbers or context reference.\n"
                    + type_guidance +
                    "\nUse ONLY the document text below.\n\n" +
                    chunk
                ),
            },
        ]

        # progress message for this chunk
        if job_status is not None:
            job_status["progress"] = (
                f"Chunk {idx+1}/{len(chunks)} queued (parallel={max_concurrency})"
            )

        attempt = 0
        max_attempts = 5
        while True:
            try:
                async with semaphore:
                    # Update to 'running this chunk'
                    if job_status is not None:
                        job_status["progress"] = (
                            f"Chunk {idx+1}/{len(chunks)} running (parallel={max_concurrency})"
                        )

                    resp = await async_client.chat.completions.create(
                        model=MODEL_NAME,
                        messages=messages,
                        temperature=MODEL_TEMPERATURE,
                        response_format={"type": "json_object"},
                    )
                break
            except Exception as e:
                code = getattr(e, "code", None)
                if (code == 429 or "RateLimitError" in type(e).__name__) and attempt < max_attempts:
                    delay = (2 ** attempt) + random.uniform(0, 1)
                    if job_status is not None:
                        job_status["progress"] = (
                            f"Rate limit on chunk {idx+1}/{len(chunks)}, retry {attempt+1} in {delay:.2f}s"
                        )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                else:
                    raise

        raw = resp.choices[0].message.content

        # Try to parse JSON
        try:
            data = json.loads(raw)
        except Exception:
            match = re.search(r"\{.*?\}", raw, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except Exception:
                    data = {}
            else:
                data = {}

        # mark processed
        if job_status is not None:
            job_status["progress"] = (
                f"Chunk {idx+1}/{len(chunks)} processed (parallel={max_concurrency})"
            )
        return data

    # launch tasks under this concurrency setting
    if job_status is not None:
        job_status["progress"] = f"Extracting with concurrency={max_concurrency}"
    results = await asyncio.gather(*[_process_chunk(i, ch) for i, ch in enumerate(chunks)])

    # --- merge logic: same as before ---
    def _normalize_answer(val):
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
            "source": source,
        }

    for data in results:
        for k in keys:
            val = data.get(k)
            if val is None:
                continue
            val_obj = _normalize_answer(val)

            if out[k] is None:
                out[k] = val_obj
                continue

            if not isinstance(out[k], dict):
                if out[k] is None or out[k] == "" or (
                    isinstance(out[k], str) and out[k].strip().lower() == "null"
                ):
                    out[k] = val_obj
                continue

            if (
                out[k].get("answer") is None
                or (
                    isinstance(out[k].get("answer"), str)
                    and out[k].get("answer").strip() == ""
                )
            ):
                out[k] = val_obj
                continue

            if val_obj["confidence"] > out[k].get("confidence", 0):
                out[k] = val_obj
                continue

            existing_source = out[k].get("source", "")
            new_source = val_obj.get("source", "")
            if new_source and new_source not in existing_source:
                if existing_source.strip() == "":
                    out[k]["source"] = new_source
                else:
                    out[k]["source"] = existing_source + "; " + new_source

    for k in keys:
        if out[k] is None:
            out[k] = {"answer": "", "confidence": 1, "source": ""}

    if job_status is not None:
        job_status["result"] = out

    return out

async def extract_answers_async(pdf_path: Path, mapping: Mapping, job_status: dict | None = None) -> Dict[str, object]:
    """
    Adaptive wrapper:
    - try concurrency levels [3, 2, 1]
    - if one fails (OOM / killed mid-flight), fall back to the next
    """

    concurrency_plan = [3, 2, 1]
    last_err = None

    for level in concurrency_plan:
        try:
            if job_status is not None:
                job_status["progress"] = f"Starting extraction with concurrency={level}"
            result = await _extract_with_limit(pdf_path, mapping, job_status, max_concurrency=level)
            if job_status is not None:
                job_status["progress"] = f"Extraction finished with concurrency={level}"
            return result
        except Exception as e:
            last_err = e
            if job_status is not None:
                job_status["progress"] = f"Concurrency {level} failed ({type(e).__name__}). Trying lower."
            # loop continues to next level

    # if we get here, they all failed
    if job_status is not None:
        job_status["progress"] = f"All concurrency levels failed: {type(last_err).__name__}"
        job_status["error"] = f"extraction_failed:{type(last_err).__name__}"

    raise last_err

def extract_answers(pdf_path: Path, mapping: Mapping, job_status: dict | None = None) -> Dict[str, object]:
    return asyncio.run(extract_answers_async(pdf_path, mapping, job_status))
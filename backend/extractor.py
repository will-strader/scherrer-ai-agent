from __future__ import annotations
from pathlib import Path
import re
import json
import pdfplumber
from typing import List, Dict
import asyncio
import random
import time
from openai import AsyncOpenAI

from .config import MODEL_NAME, MODEL_TEMPERATURE
from .mapping import Mapping

async_client = AsyncOpenAI()

# --- safety clamp on MODEL_TEMPERATURE from env ---
try:
    _t = float(MODEL_TEMPERATURE)
    if not (0.0 <= _t <= 1.0):
        raise ValueError
except Exception:
    _t = 0.3
MODEL_TEMPERATURE = _t


def _read_pdf_text(pdf_path: Path, max_pages: int | None = None) -> List[str]:
    """Extract text per page using pdfplumber. Returns a list of page texts (one string per page)."""
    texts: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        limit = min(total_pages, max_pages) if max_pages else total_pages
        for i in range(limit):
            try:
                raw = pdf.pages[i].extract_text() or ""
            except Exception:
                raw = ""
            # normalize whitespace
            norm = re.sub(r"[ \t]+", " ", raw)
            texts.append(norm)
    return texts


def _chunk_text(pages: List[str], target_chars: int = 12000) -> List[str]:
    """
    Group consecutive pages into chunks of about target_chars characters.
    This keeps each prompt reasonably sized.
    """
    chunks: List[str] = []
    buf = ""
    for page_text in pages:
        if len(buf) + len(page_text) + 1 > target_chars:
            if buf:
                chunks.append(buf)
            buf = page_text
        else:
            buf += ("\n" if buf else "") + page_text
    if buf:
        chunks.append(buf)
    return chunks


def _build_instructions(mapping: Mapping) -> str:
    """
    System prompt sent to the model for each chunk.
    Keep this strict and SHORT. (We already tuned wording to avoid 'not specified').
    """
    lines = [
        "You are extracting answers for a construction bid checklist.",
        "Return ONLY one JSON object.",
        "Each key must EXACTLY match the provided keys.",
        "Each value must be an object with: 'answer' (string), 'confidence' (1-10 int), 'source' (short string).",
        "NEVER say 'unknown', 'not specified', 'N/A'. If unclear, make your best guess and give low confidence (1-3).",
        "Keep answers short (no paragraphs).",
        "Keep 'source' under 10 words (like 'Page 12', 'Bid Form').",
        "Dates: YYYY-MM-DD. Yes/No fields: strictly 'Yes' or 'No'.",
    ]
    return "\n".join(lines)


def _normalize_answer(val):
    """
    Take whatever the model gave for a single field and coerce it into:
    { 'answer': str, 'confidence': int 1-10, 'source': str }
    """
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
    else:
        # weird value -> dump to string
        answer = str(val)

    if not isinstance(answer, str):
        answer = str(answer)

    return {
        "answer": answer,
        "confidence": confidence,
        "source": source,
    }


def _merge_answer(into_val: dict | None, new_val: dict) -> dict:
    """
    Merge a new answer for a given key into the running result.
    Rules:
    - If slot is empty, take new.
    - Prefer higher confidence.
    - If same/close confidence but different answer, keep original answer but append new source.
    - Always try to union sources (without repeating).
    """
    if into_val is None:
        return new_val

    # if into_val isn't a dict, just replace it if new_val looks valid
    if not isinstance(into_val, dict):
        return new_val

    # if existing answer is empty but new has something
    if (
        into_val.get("answer") is None
        or (
            isinstance(into_val.get("answer"), str)
            and into_val.get("answer").strip() == ""
        )
    ):
        return new_val

    # prefer higher confidence
    old_conf = into_val.get("confidence", 0)
    new_conf = new_val.get("confidence", 0)
    if new_conf > old_conf:
        return new_val

    # same or lower confidence:
    # merge sources if different
    old_src = into_val.get("source", "") or ""
    new_src = new_val.get("source", "") or ""
    if new_src and new_src not in old_src:
        if not old_src.strip():
            into_val["source"] = new_src
        else:
            into_val["source"] = f"{old_src}; {new_src}"

    return into_val


async def _process_chunk(
    idx: int,
    total_chunks: int,
    chunk_text: str,
    keys: List[str],
    mapping: Mapping,
    system_msg: str,
    semaphore: asyncio.Semaphore,
    job_status: dict | None,
    max_concurrency: int,
) -> Dict[str, object]:
    """
    Run the model on ONE chunk of text.
    Returns a dict like {key: {answer, confidence, source}, ...}
    """

    # build answer type guidance for this chunk
    type_guidance = "Answer using the correct type:\n"
    for row in mapping.question_rows:
        atype = getattr(row, "answer_type", "").lower().strip()
        key = getattr(row, "json_key", "").strip()
        if not key:
            continue
        if atype == "number":
            type_guidance += f"- {key}: numeric only (e.g. 5 or 10.0)\n"
        elif atype == "currency":
            type_guidance += f"- {key}: numeric currency (e.g. 5000)\n"
        elif atype in ("yes/no", "yesno"):
            type_guidance += f"- {key}: strictly 'Yes' or 'No'\n"
        elif atype == "text":
            type_guidance += f"- {key}: short text / name\n"
        elif atype == "date":
            type_guidance += f"- {key}: YYYY-MM-DD\n"

    user_prompt = (
        "Answer ONLY these keys: " + ", ".join(keys) + ". "
        "Each key must map to "
        '{"answer": "...", "confidence": <1-10>, "source": "..."}.\n'
        "Confidence must be 1-10.\n"
        "Source should be short page refs.\n"
        + type_guidance
        + "\nUse ONLY the document text below.\n\n"
        + chunk_text
    )

    if job_status is not None:
        job_status["progress"] = (
            f"Chunk {idx+1}/{total_chunks} queued (parallel={max_concurrency})"
        )

    # retry loop / rate limit backoff
    attempt = 0
    max_attempts = 5
    while True:
        try:
            async with semaphore:
                if job_status is not None:
                    job_status["progress"] = (
                        f"Chunk {idx+1}/{total_chunks} running (parallel={max_concurrency})"
                    )

                resp = await async_client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=MODEL_TEMPERATURE,
                    response_format={"type": "json_object"},
                )
            break
        except Exception as e:
            # simple rate limit heuristic
            code = getattr(e, "code", None)
            if (code == 429 or "RateLimitError" in type(e).__name__) and attempt < max_attempts:
                delay = (2 ** attempt) + random.uniform(0, 1)
                if job_status is not None:
                    job_status["progress"] = (
                        f"Rate limit on chunk {idx+1}/{total_chunks}, "
                        f"retry {attempt+1} in {delay:.2f}s"
                    )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            raise

    raw = resp.choices[0].message.content

    # parse JSON
    try:
        data = json.loads(raw)
    except Exception:
        # emergency fallback: grab first {...} block
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = {}
        else:
            data = {}

    if job_status is not None:
        job_status["progress"] = (
            f"Chunk {idx+1}/{total_chunks} processed (parallel={max_concurrency})"
        )

    # normalize each answer in this chunk
    cleaned: Dict[str, object] = {}
    for k in keys:
        if k in data:
            cleaned[k] = _normalize_answer(data[k])
    return cleaned


async def _extract_with_limit(
    pdf_path: Path,
    mapping: Mapping,
    job_status: dict | None,
    max_concurrency: int,
) -> Dict[str, object]:
    """
    Core extractor for ONE concurrency level.
    - Read + chunk PDF
    - Process chunks in batches of size = max_concurrency (so memory doesn't explode)
    - Merge partial answers into running result dict
    - Return final merged dict
    """

    # 1. read + chunk
    pages = _read_pdf_text(pdf_path)
    chunks = _chunk_text(pages, target_chars=12000)
    total_chunks = len(chunks)

    keys = mapping.json_keys()
    system_msg = _build_instructions(mapping)

    # this will hold final merged answers
    merged: Dict[str, dict | None] = {k: None for k in keys}

    # step size = max_concurrency
    if job_status is not None:
        job_status["progress"] = (
            f"Extracting with concurrency={max_concurrency}, total_chunks={total_chunks}"
        )

    semaphore = asyncio.Semaphore(max_concurrency)

    batch_index = 0
    for start in range(0, total_chunks, max_concurrency):
        end = min(start + max_concurrency, total_chunks)
        current_specs = [(i, chunks[i]) for i in range(start, end)]

        # prepare tasks for this batch
        tasks = [
            _process_chunk(
                idx=i,
                total_chunks=total_chunks,
                chunk_text=ch_text,
                keys=keys,
                mapping=mapping,
                system_msg=system_msg,
                semaphore=semaphore,
                job_status=job_status,
                max_concurrency=max_concurrency,
            )
            for (i, ch_text) in current_specs
        ]

        # run this batch in parallel
        batch_results: List[Dict[str, object]] = await asyncio.gather(*tasks)

        # merge this batch into `merged`
        for chunk_result in batch_results:
            for k in keys:
                if k not in chunk_result:
                    continue
                merged[k] = _merge_answer(merged[k], chunk_result[k])

        batch_index += 1
        if job_status is not None:
            job_status["progress"] = (
                f"Merged batch {batch_index} "
                f"({end}/{total_chunks} chunks done, parallel={max_concurrency})"
            )

    # fill any missing keys with fallback object
    for k in keys:
        if merged[k] is None:
            merged[k] = {"answer": "", "confidence": 1, "source": ""}

    if job_status is not None:
        job_status["result"] = merged
        job_status["progress"] = "Extraction complete (merged all batches)"

    return merged


async def extract_answers_async(
    pdf_path: Path,
    mapping: Mapping,
    job_status: dict | None = None,
) -> Dict[str, object]:
    """
    Adaptive wrapper.
    Try concurrency = 3, then 2, then 1.
    If an attempt fails (e.g. memory kill), fall back to the next.
    """

    plan = [3, 2, 1]
    last_err: Exception | None = None

    for level in plan:
        try:
            if job_status is not None:
                job_status["progress"] = (
                    f"Starting extraction with concurrency={level}"
                )
            result = await _extract_with_limit(
                pdf_path=pdf_path,
                mapping=mapping,
                job_status=job_status,
                max_concurrency=level,
            )
            if job_status is not None:
                job_status["progress"] = (
                    f"Extraction finished with concurrency={level}"
                )
            return result
        except Exception as e:
            last_err = e
            if job_status is not None:
                job_status["progress"] = (
                    f"Concurrency {level} failed ({type(e).__name__}). Trying lower."
                )
            # keep looping

    # all levels failed
    if job_status is not None:
        job_status["progress"] = (
            f"All concurrency levels failed: {type(last_err).__name__ if last_err else 'unknown'}"
        )
        job_status["error"] = (
            f"extraction_failed:{type(last_err).__name__ if last_err else 'unknown'}"
        )
    raise last_err if last_err else RuntimeError("Unknown extraction failure")


def extract_answers(
    pdf_path: Path,
    mapping: Mapping,
    job_status: dict | None = None,
) -> Dict[str, object]:
    """
    Sync convenience wrapper (used by local/manual calls).
    """
    return asyncio.run(extract_answers_async(pdf_path, mapping, job_status))
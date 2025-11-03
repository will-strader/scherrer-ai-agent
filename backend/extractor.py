from __future__ import annotations
from pathlib import Path
import re
import json
import pdfplumber
from typing import List, Dict, Optional, Callable
import asyncio
import random
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
    chunk_text: str,
    keys: List[str],
    mapping: Mapping,
    system_msg: str,
    job_status: dict | None,
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
        job_status["progress"] = f"Running model on chunk {idx+1}"

    # retry loop / rate limit backoff
    attempt = 0
    max_attempts = 5
    while True:
        try:
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
                        f"Rate limit on chunk {idx+1}, retry {attempt+1} in {delay:.2f}s"
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
        job_status["progress"] = f"Chunk {idx+1} processed"

    # normalize each answer in this chunk
    cleaned: Dict[str, object] = {}
    for k in keys:
        if k in data:
            cleaned[k] = _normalize_answer(data[k])
    return cleaned


def _iter_chunks_from_pdf(pdf_path: Path, target_chars: int = 12000):
    buf = ""
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        for p_idx in range(total_pages):
            try:
                raw = pdf.pages[p_idx].extract_text() or ""
            except Exception:
                raw = ""
            norm = re.sub(r"[ \t]+", " ", raw)
            # if adding this page would push us over target_chars, yield current buf
            if buf and (len(buf) + len(norm) + 1 > target_chars):
                yield buf
                buf = norm
            else:
                buf = (buf + "\n" + norm) if buf else norm
        if buf:
            yield buf


async def _extract_with_concurrency(
    pdf_path: Path,
    mapping: Mapping,
    job_status: dict | None,
    max_concurrency: int,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, object]:
    """
    Bounded-concurrency extractor using an async queue and a small worker pool.
    Memory safety: we never accumulate all chunks in memory; the queue has a tiny
    bounded size and workers hold at most one chunk each.
    """
    keys = mapping.json_keys()
    system_msg = _build_instructions(mapping)

    merged: Dict[str, dict | None] = {k: None for k in keys}

    # Track total chunks so the UI can display a proper progress bar.
    total_chunks: int = 0

    # Small queue so we don't buffer the whole PDF.
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue(maxsize=max_concurrency * 2)

    results: Dict[int, Dict[str, object]] = {}
    exception_holder: list[BaseException] = []

    async def worker(worker_id: int):
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                break
            idx, chunk_text = item
            try:
                partial = await _process_chunk(idx, chunk_text, keys, mapping, system_msg, job_status)
                results[idx] = partial
                if progress_cb:
                    progress_cb(len(results), total_chunks, note=f"chunk {idx+1} complete")
            except BaseException as e:  # capture and propagate later
                exception_holder.append(e)
            finally:
                queue.task_done()

    # Start workers
    workers = [asyncio.create_task(worker(i)) for i in range(max(1, max_concurrency))]

    # Producer: stream chunks and feed queue
    if job_status is not None:
        job_status["progress"] = f"Preparing chunks (concurrency={max_concurrency})"
    if progress_cb:
        progress_cb(0, 0, note=f"preparing chunks (conc={max_concurrency})")

    idx = 0
    for chunk_text in _iter_chunks_from_pdf(pdf_path):
        await queue.put((idx, chunk_text))
        idx += 1

    # Record total number of chunks produced
    total_chunks = idx
    if progress_cb:
        progress_cb(0, total_chunks, note=f"queued {total_chunks} chunks")

    # Tell workers to stop
    for _ in workers:
        await queue.put(None)

    # Wait for all work to finish
    await queue.join()

    # Propagate any exception that occurred inside workers
    if exception_holder:
        # Cancel workers before raising
        for w in workers:
            w.cancel()
        raise exception_holder[0]

    # Ensure workers are fully done
    for w in workers:
        try:
            await w
        except asyncio.CancelledError:
            pass

    # Merge results in index order so later chunks can override with higher confidence
    for i in range(idx):
        partial = results.get(i, {})
        for k in keys:
            if k in partial:
                merged[k] = _merge_answer(merged[k], partial[k])

    # Fill any missing keys
    for k in keys:
        if merged[k] is None:
            merged[k] = {"answer": "", "confidence": 1, "source": ""}

    if job_status is not None:
        job_status["result"] = merged
        job_status["progress"] = "Extraction complete"

    if progress_cb:
        progress_cb(len(results), total_chunks, note="extraction complete")

    return merged


async def extract_answers_async(
    pdf_path: Path,
    mapping: Mapping,
    job_status: dict | None = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    concurrency: Optional[int] = None,
) -> Dict[str, object]:
    """
    Async extractor with bounded concurrency and automatic fallback
    (3 → 2 → 1) if an exception occurs.
    """
    # Build the concurrency attempt list.
    if concurrency is not None:
        try:
            start = int(concurrency)
        except Exception:
            start = 3
        start = max(1, min(start, 6))  # clamp to [1,6]
        conc_list = list(range(start, 0, -1))  # e.g., 5,4,3,2,1
    else:
        conc_list = [3, 2, 1]

    for conc in conc_list:
        try:
            if job_status is not None:
                job_status["progress"] = f"Starting extraction (concurrency={conc})"
                job_status["concurrency"] = conc
            return await _extract_with_concurrency(
                pdf_path, mapping, job_status, conc, progress_cb=progress_cb
            )
        except BaseException as e:
            if job_status is not None:
                job_status["progress"] = (
                    f"Error on concurrency={conc}: {type(e).__name__}. Retrying with lower concurrency..."
                )
            # On last attempt, re-raise
            if conc == 1:
                raise
            # Otherwise, small pause before retry
            await asyncio.sleep(0.5)

    raise RuntimeError("Extraction failed after concurrency fallback")



def extract_answers(
    pdf_path: Path,
    mapping: Mapping,
    job_status: dict | None = None,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    concurrency: Optional[int] = None,
) -> Dict[str, object]:
    """
    Sync convenience wrapper (used by local/manual calls).
    Runs concurrency fallback extraction (3 → 2 → 1).
    """
    return asyncio.run(
        extract_answers_async(
            pdf_path,
            mapping,
            job_status,
            progress_cb=progress_cb,
            concurrency=concurrency,
        )
    )
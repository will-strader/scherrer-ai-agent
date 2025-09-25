from __future__ import annotations
import csv, io, re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict
import chardet  # make sure 'chardet' is in requirements.txt

REQUIRED_HEADERS = ["sheet","cell","text","is_question","json_key","answer_type","notes"]
ALLOWED_TYPES = {"text","date","number","currency","yesno","list","email","phone","location"}

@dataclass
class MapRow:
    sheet: str
    cell: str
    text: str
    is_question: bool
    json_key: str
    answer_type: str
    notes: str
    confidence: float = 0.0
    source: str = ""

@dataclass
class Mapping:
    rows: List[MapRow]

    @property
    def question_rows(self) -> List[MapRow]:
        return [r for r in self.rows if r.is_question]

    def json_keys(self) -> List[str]:
        return [r.json_key for r in self.question_rows if r.json_key]

    def schema(self) -> Dict:
        tmap = {
            "text": {"type":"string"},
            "date": {"type":"string", "format":"date"},
            "number": {"type":"number"},
            "currency": {"type":"number"},
            "yesno": {"type":"string", "enum":["Yes","No"]},
            "list": {"type":"array", "items":{"type":"string"}},
            "email": {"type":"string", "format":"email"},
            "phone": {"type":"string"},
            "location": {"type":"string"},
        }
        props = {}
        for r in self.question_rows:
            at = (r.answer_type or "text").lower().strip()
            base_type = tmap.get(at, {"type":"string"})
            props[r.json_key] = {
                "type": "object",
                "properties": {
                    "answer": base_type,
                    "confidence": {"type": "number"},
                    "source": {"type": "string"},
                }
            }
        return {"type":"object","properties":props}

    @property
    def by_sheet(self) -> Dict[str, List[MapRow]]:
        sheet_map: Dict[str, List[MapRow]] = {}
        for row in self.rows:
            sheet_map.setdefault(row.sheet, []).append(row)
        return sheet_map

    def by_json_key(self) -> Dict[str, MapRow]:
        return {row.json_key: row for row in self.rows if row.json_key}

    def __repr__(self) -> str:
        return f"<Mapping rows={len(self.rows)} questions={len(self.question_rows)} keys={self.json_keys()[:5]}...>"

def _sniff_and_split_singlecol(lines: list[str]) -> list[dict]:
    # Try common delimiters to split a single “all-in-one” column file
    for delim in [",",";","\t","|"]:
        parts = [row.split(delim) for row in lines if row is not None]
        if not parts:
            continue
        headers = [h.strip().lower() for h in parts[0]]
        if len(headers) < 3:  # too few to be our mapping
            continue
        rows = []
        for row in parts[1:]:
            row += [""] * (len(headers)-len(row))
            rows.append({headers[i]: row[i].strip() for i in range(len(headers))})
        return rows
    return []

def _norm_bool(v: str) -> bool:
    s = (v or "").strip().lower()
    return s in ("y","yes","true","1")

def load_mapping(path: Path) -> Mapping:
    raw = path.read_bytes()
    enc = chardet.detect(raw).get("encoding") or "utf-8"
    text = raw.decode(enc, errors="replace")

    # If the first line is a lone token (no typical delimiters) but the second line looks like a header,
    # drop the first line to tolerate exports with a stray title row.
    _lines = text.splitlines()
    if len(_lines) >= 2:
        first, second = _lines[0], _lines[1]
        delims = [",", ";", "\t", "|"]
        if all(d not in first for d in delims) and any(d in second for d in delims):
            text = "\n".join(_lines[1:])

    # First try normal CSV
    rows: list[dict]
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0] if text else ",")
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in reader]
        if not rows:
            raise ValueError("Empty mapping CSV")
        # If it looks like a single column dump, try manual split
        if len(rows[0].keys()) == 1 and next(iter(rows[0].keys())) not in REQUIRED_HEADERS:
            rows = _sniff_and_split_singlecol(text.splitlines())
    except Exception:
        rows = _sniff_and_split_singlecol(text.splitlines())

    if not rows:
        raise ValueError("Could not parse mapping CSV")

    # Map common header variants → required names
    header_map = {
        "sheet name":"sheet","worksheet":"sheet",
        "cell address":"cell",
        "question":"text","label":"text","prompt":"text",
        "isquestion":"is_question","yes/no":"is_question","is question":"is_question",
        "json key":"json_key","json-key":"json_key",
        "answer type":"answer_type","type":"answer_type",
    }
    norm_rows = []
    for r in rows:
        nr = {}
        for k, v in r.items():
            k2 = header_map.get(k, k)
            nr[k2] = v
        # ensure required headers exist
        for h in REQUIRED_HEADERS:
            nr.setdefault(h, "")
        # set defaults for new fields
        nr.setdefault("confidence", "0.0")
        nr.setdefault("source", "")
        norm_rows.append(nr)

    # Build structured rows and validate keys/types
    key_re = re.compile(r"^[a-z0-9_]+$")
    cell_re = re.compile(r"^[A-Za-z]{1,3}[0-9]{1,6}$")
    warnings: list[str] = []
    out: list[MapRow] = []
    seen_keys: list[str] = []

    for r in norm_rows:
        isq = _norm_bool(r.get("is_question",""))
        key = (r.get("json_key","") or "").strip()

        at_raw = (r.get("answer_type", "") or "text").strip().lower()
        # normalize common variants: remove spaces, slashes, hyphens for alias matching
        at_simple = at_raw.replace(" ", "").replace("/", "").replace("-", "")
        alias_map = {
            "yesno": "yesno",
            "yesn": "yesno",           # occasional typo
            "yn": "yesno",
            "boolean": "yesno",
            "pct": "number",
            "percentage": "number",
            "percent": "number",
            "money": "currency",
            "usd": "currency",
            "dollars": "currency",
            "phonenumber": "phone",
            "telephone": "phone",
            "tel": "phone",
            "emailaddress": "email",
            "email": "email",
            "e-mail": "email",
            "locationaddress": "location",
            "addr": "location",
            # date/time combos & variants
            "datetime": "date",
            "dateandtime": "date",
            "date_time": "date",
            "datetimelocation": "text",   # treat combined date/time/location as free text
            "dateandtimelocation": "text",
            "date/time": "date",
            "date-time": "date",
        }
        at = alias_map.get(at_simple, at_raw)

        if isq:
            if key and not key_re.match(key):
                raise ValueError(f"Invalid json_key '{key}' (use lowercase/underscores only).")
            if at not in ALLOWED_TYPES:
                raise ValueError(f"Unknown answer_type '{at}' for key '{key}'.")

        # Warn on missing/invalid cell for question rows (do not fail the load)
        if isq:
            if not (r.get("cell", "").strip()):
                warnings.append(f"Missing cell for question key '{key}' on sheet '{(r.get('sheet','') or 'Bid Information').strip()}'")
            elif not cell_re.match((r.get("cell", "") or "").strip()):
                warnings.append(f"Suspicious cell address '{(r.get('cell','') or '').strip()}' for key '{key}'")

        try:
            conf_val = float(r.get("confidence", "0.0"))
        except ValueError:
            conf_val = 0.0

        out.append(MapRow(
            sheet=(r.get("sheet","") or "Bid Information").strip(),
            cell=(r.get("cell","") or "").strip(),
            text=(r.get("text","") or "").strip(),
            is_question=isq,
            json_key=key,
            answer_type=at,
            notes=(r.get("notes","") or "").strip(),
            confidence=conf_val,
            source=(r.get("source","") or "").strip(),
        ))
        if isq and key:
            seen_keys.append(key)

    # duplicate key guard
    dups = {k for k in seen_keys if seen_keys.count(k) > 1}
    if dups:
        raise ValueError(f"Duplicate json_key(s): {sorted(dups)}")

    if warnings:
        print("[mapping] Warnings:")
        for w in warnings:
            print(f"  - {w}")

    return Mapping(rows=out)
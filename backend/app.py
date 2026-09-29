from __future__ import annotations

import io
import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import boto3
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from openpyxl import load_workbook
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

RELEASE_COLUMNS = {
    "functional_area": "Functional Area (Domain)", "product_line": "Product Line", "product_area": "Product Area",
    "using_workday": "Using Workday", "title": "Title", "description": "Description",
    "business_benefits": "Business Benefits", "changes": "Changes", "impact": "Impact",
    "release_number": "Release Number", "release_note_url": "Release Note URL",
}
E2E_COLUMNS = {"item": "Item", "work_stream": "Work-Stream", "e2e": "E2E"}
STOP_WORDS = {"the", "and", "for", "with", "from", "that", "this", "into", "your", "are", "now", "can", "you", "workday", "users", "user", "new", "using", "use", "when", "more", "only", "all", "not", "will", "their", "they", "has", "have", "been", "than"}
WORKSTREAM_HINTS = {
    "payroll": {"payroll", "pay", "payment", "tax", "wage", "settlement", "earning", "deduction"},
    "hcm core": {"hcm", "human", "capital", "staffing", "job", "worker", "employee", "organization", "hire", "termination"},
    "compensation": {"compensation", "salary", "bonus", "reward", "merit", "pay"},
    "advanced compensation": {"compensation", "salary", "bonus", "reward", "merit"},
    "absence": {"absence", "leave", "time off", "return from leave"},
    "time tracking": {"time", "hours", "reported", "elapsed", "exception"},
    "benefits": {"benefit", "enrollment", "coverage", "dependent"},
    "recruiting": {"recruit", "candidate", "job requisition", "interview", "offer", "hire"},
    "talent and performance": {"talent", "performance", "succession", "objective", "development", "disciplinary"},
    "learning": {"learning", "course", "program", "lesson", "topic"},
    "expenses": {"expense", "credit card", "expense report", "reimbursement"},
}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def normalize_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean(value).lower())


def tokens(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", value.lower()) if len(word) > 2 and word not in STOP_WORDS}


def table_from_upload(contents: bytes, kind: str) -> dict[str, Any]:
    try:
        workbook = load_workbook(io.BytesIO(contents), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not read workbook: {exc}") from exc
    sheet = next((item for item in workbook.worksheets if item.max_row and item.max_column), None)
    if sheet is None:
        raise HTTPException(status_code=400, detail=f"{kind.title()} workbook has no non-empty worksheets")
    rows = list(sheet.iter_rows(values_only=True))
    headers = [clean(value) for value in rows[0]] if rows else []
    header_index = {normalize_header(header): index for index, header in enumerate(headers) if header}
    required = RELEASE_COLUMNS if kind == "release" else E2E_COLUMNS
    missing = [label for label in required.values() if normalize_header(label) not in header_index]
    if missing:
        raise HTTPException(status_code=400, detail=f"{kind.title()} workbook is missing columns: {', '.join(missing)}")
    data = []
    for source_row, row in enumerate(rows[1:], start=2):
        if not any(clean(value) for value in row):
            continue
        record = {key: clean(row[header_index[normalize_header(label)]]) for key, label in required.items()}
        record["source_row"] = source_row
        data.append(record)
    return {"sheet": sheet.title, "sheets": workbook.sheetnames, "headers": headers, "rows": data, "row_count": len(data)}


def release_text(row: dict[str, Any]) -> str:
    return " ".join(row.get(key, "") for key in RELEASE_COLUMNS if key not in {"release_note_url", "release_number"})


def shortlist(release: dict[str, Any], e2e_rows: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    source = release_text(release)
    source_tokens = tokens(source)
    functional = clean(release.get("functional_area", "")).lower()
    product = clean(release.get("product_area", "")).lower()
    scored = []
    for candidate in e2e_rows:
        name = candidate["e2e"]
        candidate_text = f"{candidate.get('work_stream', '')} {name}"
        overlap = len(source_tokens & tokens(candidate_text)) / max(1, len(source_tokens | tokens(candidate_text)))
        stream = clean(candidate.get("work_stream", "")).lower()
        hint_words = WORKSTREAM_HINTS.get(stream, set())
        hint_bonus = 0.22 if any(hint in source.lower() for hint in hint_words) else 0.0
        if stream in functional or stream in product:
            hint_bonus += 0.28
        fuzzy = SequenceMatcher(None, clean(release.get("title", "")).lower(), name.lower()).ratio()
        scored.append((overlap * 0.55 + hint_bonus + fuzzy * 0.2, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [{**candidate, "shortlist_score": round(score, 4)} for score, candidate in scored[:limit]]


def bedrock_model_id() -> str:
    return os.getenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0").strip()


def review_threshold() -> int:
    try:
        return int(os.getenv("REVIEW_THRESHOLD", "70"))
    except ValueError:
        return 70


def fallback_result(reason: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {"decision": "unable_to_identify", "business_process_name": None, "e2e_name": None, "confidence": 0, "confidence_band": "low", "reasoning": reason, "evidence": [], "review_required": True, "shortlisted_candidates": [c["e2e"] for c in candidates]}


def extract_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.DOTALL).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Gemini response did not contain a JSON object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("Gemini response was not an object")
    return value


def validate_result(value: dict[str, Any], candidates: list[dict[str, Any]] | list[str], source_text: str = "") -> dict[str, Any]:
    candidate_names = [candidate["e2e"] if isinstance(candidate, dict) else candidate for candidate in candidates]
    decision = value.get("decision") if value.get("decision") in {"match", "unable_to_identify", "no_matching_e2e"} else "unable_to_identify"
    selected = value.get("e2e_name") if value.get("e2e_name") in candidate_names else None
    if decision == "match" and not selected:
        decision = "no_matching_e2e"
    try:
        confidence = max(0, min(100, int(float(value.get("confidence", 0)))))
    except (ValueError, TypeError):
        confidence = 0
    raw_evidence = value.get("evidence") or []
    if isinstance(raw_evidence, str):
        raw_evidence = [raw_evidence]
    if not isinstance(raw_evidence, list):
        raw_evidence = []
    evidence = [clean(item) for item in raw_evidence if clean(item)][:5]
    evidence_grounding = round(100 * sum(1 for item in evidence if item.lower() in source_text.lower()) / max(1, len(evidence))) if source_text else 0
    ranked = [candidate for candidate in candidates if isinstance(candidate, dict)]
    selected_candidate = next((candidate for candidate in ranked if candidate.get("e2e") == selected), None)
    rank = next((index + 1 for index, candidate in enumerate(ranked) if candidate.get("e2e") == selected), None)
    top_score = float(ranked[0].get("shortlist_score", 0)) if ranked else 0
    selected_score = float(selected_candidate.get("shortlist_score", 0)) if selected_candidate else 0
    second_score = float(ranked[1].get("shortlist_score", 0)) if len(ranked) > 1 else 0
    lexical_relevance = round(min(100, max(0, selected_score / 1.05 * 100)))
    selection_agreement = 100 if selected and rank == 1 else 60 if selected else 0
    model_confidence = confidence
    confidence = round(0.35 * model_confidence + 0.40 * lexical_relevance + 0.15 * selection_agreement + 0.10 * evidence_grounding)
    if decision != "match":
        confidence = min(confidence, 49)
    band = "high" if confidence >= 80 else "medium" if confidence >= 60 else "low"
    review_required = True if decision != "match" else bool(value.get("review_required", False)) or confidence < review_threshold() or evidence_grounding < 20 or rank != 1
    return {"decision": decision, "business_process_name": clean(value.get("business_process_name")) or None, "e2e_name": selected, "confidence": confidence, "confidence_band": band, "reasoning": clean(value.get("reasoning")) or "The model did not provide sufficient grounded reasoning.", "evidence": evidence, "review_required": review_required, "evaluation": {"model_confidence": model_confidence, "lexical_relevance": lexical_relevance, "candidate_rank": rank, "candidate_margin": round(max(0, top_score - second_score), 4), "evidence_grounding": evidence_grounding, "selection_agreement": selection_agreement}}


async def ask_bedrock(release: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "ap-south-1"))
    except Exception as exc:
        return fallback_result(f"Amazon Bedrock is not configured: {exc}", candidates)
    candidate_names = [candidate["e2e"] for candidate in candidates]
    prompt = f"""You are a cautious Workday regression-test analyst. Identify the single best matching E2E job/test case for this release note, but do not guess. Choose only an exact value from the candidate list. Return JSON only with keys decision, business_process_name, e2e_name, confidence, confidence_band, reasoning, evidence, review_required. decision must be match, unable_to_identify, or no_matching_e2e. confidence must be an integer 0-100. Use low confidence and review_required=true when evidence is weak. evidence must be an array of short exact phrases copied from the release note, not a single string. Keep reasoning grounded in the supplied release note.

Release note:
{json.dumps(release, ensure_ascii=False)}

Candidate E2E catalog values:
{json.dumps(candidate_names, ensure_ascii=False)}
"""
    try:
        response = client.converse(
            modelId=bedrock_model_id(),
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 1200, "temperature": 0.1},
        )
        text = next(part["text"] for part in response["output"]["message"]["content"] if "text" in part)
        return validate_result(extract_json(text), candidates, release_text(release))
    except Exception as exc:
        return fallback_result(f"Amazon Bedrock could not be reached: {exc}", candidates)


app = FastAPI(title="Workday Release Regression Agent API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=[os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
async def health():
    return {"status": "ok", "bedrock_region": os.getenv("AWS_REGION", "ap-south-1"), "bedrock_model": bedrock_model_id(), "review_threshold": review_threshold()}


@app.post("/api/preview")
async def preview(release_file: UploadFile = File(...), e2e_file: UploadFile = File(...)):
    if not (release_file.filename or "").lower().endswith(".xlsx") or not (e2e_file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Both files must be .xlsx workbooks")
    release = table_from_upload(await release_file.read(), "release")
    e2e = table_from_upload(await e2e_file.read(), "e2e")
    return {"release": {key: release[key] for key in ["sheet", "sheets", "row_count", "headers"]}, "e2e": {key: e2e[key] for key in ["sheet", "sheets", "row_count", "headers"]}}


@app.post("/api/analyze")
async def analyze(release_file: UploadFile = File(...), e2e_file: UploadFile = File(...)):
    if not (release_file.filename or "").lower().endswith(".xlsx") or not (e2e_file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Both files must be .xlsx workbooks")
    release = table_from_upload(await release_file.read(), "release")
    e2e = table_from_upload(await e2e_file.read(), "e2e")
    results = []
    for row in release["rows"]:
        candidates = shortlist(row, e2e["rows"])
        result = await ask_bedrock(row, candidates)
        result.update({"source_row": row["source_row"], "release": row, "shortlisted_candidates": [candidate["e2e"] for candidate in candidates]})
        result["review_required"] = result["review_required"] or result["confidence"] < review_threshold()
        results.append(result)
    return {"release_count": len(release["rows"]), "e2e_count": len(e2e["rows"]), "review_threshold": review_threshold(), "results": results}

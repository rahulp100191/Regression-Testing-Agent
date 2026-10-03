import hashlib
import inspect
import json
import os
from pathlib import Path
from uuid import UUID, uuid4
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field
from eval_store import database, encode
from telemetry import stage, event, carrier, trace_id, failure

router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])
ROOT = Path(__file__).resolve().parents[1]

def implementation_version():
    return hashlib.sha256(b"".join((ROOT / "backend" / name).read_bytes() for name in ["app.py", "providers.py", "evaluate.py", "eval_worker.py", "telemetry.py", "eval_store.py", "safety.py", "feedback_api.py", "analysis_batch.py"])).hexdigest()

class EvaluationRequest(BaseModel):
    split: str = "test"
    k: int = Field(default=8, ge=1, le=100)
    include_controls: bool = False
    dataset_source: str = 'baseline'

@router.post("", status_code=202)
def create(request: EvaluationRequest, x_reviewer_key: str = Header(default='')):
    from app import shortlist, WORKSTREAM_HINTS, STOP_WORDS, tokens, release_text
    from providers import model_name, provider_name, model_routes
    if request.split not in {"test", "development", "all"}:
        raise HTTPException(422, "Invalid split")
    if request.dataset_source not in {'baseline','approved_feedback'}:
        raise HTTPException(422, 'Invalid dataset source')
    with stage('dataset.snapshot'):
        if request.dataset_source == 'approved_feedback':
            from feedback_api import export, reviewer_access
            reviewer_access(x_reviewer_key)
            data = export()
            if not data['cases'] or request.split == 'test':
                raise HTTPException(422, 'Approved feedback requires cases and development/all split; it is not held-out test data')
            dataset_bytes = json.dumps(data,sort_keys=True).encode()
        else:
            dataset_bytes = (ROOT / "outputs/workday_2025_eval/workday_2025_golden_eval.json").read_bytes()
        event('dataset.loaded',sha256=hashlib.sha256(dataset_bytes).hexdigest())
    with stage('prompt.snapshot'):
        prompt_bytes = (ROOT / "prompts/regression/v1/prompt.txt").read_bytes()
        event('prompt.loaded',prompt_version='regression-v1',sha256=hashlib.sha256(prompt_bytes).hexdigest())
    with stage('retrieval.fingerprint'):
        retrieval = inspect.getsource(shortlist) + inspect.getsource(tokens) + inspect.getsource(release_text) + repr(sorted(STOP_WORDS)) + json.dumps({k: sorted(v) for k,v in WORKSTREAM_HINTS.items()}, sort_keys=True)
    snapshot = {**request.model_dump(), "dataset": json.loads(dataset_bytes), "dataset_version": hashlib.sha256(dataset_bytes).hexdigest(),
                "prompt_template": prompt_bytes.decode(), "prompt_version": "regression-v1:" + hashlib.sha256(prompt_bytes).hexdigest(),
                "retrieval_version": hashlib.sha256(retrieval.encode()).hexdigest(), "provider": provider_name(), "model_version": model_name(),
                "report_version": "2.1", "implementation_version": implementation_version(),
                "model_routes":model_routes(),"trace_context":carrier(),"trace_id":trace_id(),
                "http_attempts":os.getenv('LLM_HTTP_ATTEMPTS','1'),"timeout_seconds":os.getenv('LLM_TIMEOUT_SECONDS','45'),
                "max_output_tokens": os.getenv("LLM_MAX_OUTPUT_TOKENS", "4096"), "review_threshold": os.getenv("REVIEW_THRESHOLD", "70"),
                "gemma_thinking_level":os.getenv('GEMMA_THINKING_LEVEL','minimal'),
                "safety_settings": {key:os.getenv(key,'') for key in ['SAFETY_REQUIRE_MANAGED','SAFETY_MAX_TEXT_CHARS','BEDROCK_GUARDRAIL_ID','BEDROCK_GUARDRAIL_VERSION']},
                "input_price": os.getenv("EVAL_INPUT_USD_PER_MILLION"), "output_price": os.getenv("EVAL_OUTPUT_USD_PER_MILLION")}
    run_id = uuid4()
    try:
        with stage('evaluation.enqueue',eval_run_id=str(run_id)):
            with database() as db:
                db.execute("INSERT INTO eval_runs(eval_run_id,status,snapshot) VALUES (%s,'queued',%s)", (run_id, encode(snapshot)))
            event('evaluation.queued',eval_run_id=str(run_id),model_version=snapshot['model_version'])
    except Exception as exc:
        failure(exc,component='postgres')
        raise HTTPException(503, "Evaluation storage unavailable. Start PostgreSQL and configure DATABASE_URL.") from None
    return {"eval_run_id": str(run_id), "status": "queued", "trace_id":trace_id()}

@router.get("")
def history():
    try:
        with database() as db:
            rows = db.execute("SELECT eval_run_id,status,timestamp,completed_at,snapshot->>'model_version' AS model_version,snapshot->>'trace_id' AS trace_id FROM eval_runs ORDER BY timestamp DESC LIMIT 100").fetchall()
        return rows
    except Exception as exc:
        failure(exc,component='postgres')
        raise HTTPException(503, "Evaluation storage unavailable") from None

@router.get("/{run_id}")
def detail(run_id: UUID, x_reviewer_key: str = Header(default='')):
    try:
        with database() as db:
            row = db.execute("SELECT eval_run_id,status,timestamp,completed_at,error,report,progress,snapshot->>'trace_id' AS trace_id,snapshot->>'dataset_source' AS dataset_source FROM eval_runs WHERE eval_run_id=%s", (run_id,)).fetchone()
    except Exception as exc:
        failure(exc,component='postgres')
        raise HTTPException(503, "Evaluation storage unavailable") from None
    if row is None:
        raise HTTPException(404, "Evaluation run not found")
    if row.get('dataset_source') == 'approved_feedback':
        from feedback_api import reviewer_access
        reviewer_access(x_reviewer_key)
    return row

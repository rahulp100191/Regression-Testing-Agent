"""Only processes runs explicitly queued by the UI/API; never schedules evaluations."""
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from string import Template
from eval_store import database, encode
from telemetry import initialize, stage, event, failure, flush

async def evaluate(job):
    with stage('evaluation.run',parent=job['snapshot'].get('trace_context'),eval_run_id=str(job['eval_run_id']),
               model_version=job['snapshot']['model_version'],prompt_version=job['snapshot']['prompt_version'],
               dataset_version=job['snapshot']['dataset_version'],retrieval_version=job['snapshot']['retrieval_version']):
        event('evaluation.claimed',eval_run_id=str(job['eval_run_id']),queue_wait_ms=(datetime.now(timezone.utc)-job['timestamp']).total_seconds()*1000)
        try:
            report = await evaluate_cases(job)
            status = 'completed_with_errors' if report['diagnostics']['model_error_count'] else 'completed'
            with stage('evaluation.persist',eval_run_id=str(job['eval_run_id'])):
                with database() as db:
                    db.execute("UPDATE eval_runs SET status=%s,report=%s,completed_at=now() WHERE eval_run_id=%s", (status, encode(report), job['eval_run_id']))
            event('evaluation.finished',eval_run_id=str(job['eval_run_id']),status=status,case_count=len(report['per_case']))
            return report
        except Exception as exc:
            failure(exc,eval_run_id=str(job['eval_run_id']))
            with stage('evaluation.persist_failure'):
                with database() as db:
                    db.execute("UPDATE eval_runs SET status='failed',error=%s,completed_at=now() WHERE eval_run_id=%s",
                        (f'Evaluation failed ({type(exc).__name__}); see trace for the failing stage',job['eval_run_id']))
            raise

async def evaluate_cases(job):
    from app import shortlist, release_text, guarded_prediction
    from evaluate import score, EXPLANATIONS
    from providers import model_name, model_routes
    s = job['snapshot']
    if s.get('gemma_thinking_level') != os.getenv('GEMMA_THINKING_LEVEL','minimal'):
        raise ValueError('Gemma thinking configuration changed since queueing')
    if s.get('safety_settings') != {key:os.getenv(key,'') for key in ['SAFETY_REQUIRE_MANAGED','SAFETY_MAX_TEXT_CHARS','BEDROCK_GUARDRAIL_ID','BEDROCK_GUARDRAIL_VERSION']}:
        raise ValueError('Safety configuration changed since queueing')
    from eval_api import implementation_version
    if implementation_version() != s['implementation_version']:
        raise ValueError('Worker implementation differs from queued version')
    if os.getenv('LLM_MAX_OUTPUT_TOKENS', '4096') != s['max_output_tokens']:
        raise ValueError('Model generation configuration changed since queueing')
    if model_name(s['provider']) != s['model_version']:
        raise ValueError('Worker model configuration differs from queued model version')
    if model_routes(s['provider']) != s.get('model_routes'):
        raise ValueError('Worker fallback routes differ from queued versions')
    if os.getenv('LLM_HTTP_ATTEMPTS','1') != s.get('http_attempts') or os.getenv('LLM_TIMEOUT_SECONDS','45') != s.get('timeout_seconds'):
        raise ValueError('Model timeout/retry configuration changed since queueing')
    # Recompute the retrieval fingerprint before using executable worker code.
    import inspect
    from app import WORKSTREAM_HINTS, STOP_WORDS, tokens
    retrieval = inspect.getsource(shortlist) + inspect.getsource(tokens) + inspect.getsource(release_text) + repr(sorted(STOP_WORDS)) + json.dumps({k: sorted(v) for k,v in WORKSTREAM_HINTS.items()}, sort_keys=True)
    if hashlib.sha256(retrieval.encode()).hexdigest() != s['retrieval_version']:
        raise ValueError('Retrieval changed since queueing; create a new run')
    if os.getenv('REVIEW_THRESHOLD', '70') != s['review_threshold']:
        raise ValueError('Review threshold changed since queueing; create a new run')
    cases = [c for c in s['dataset']['cases'] if c['task']=='release_to_e2e_mapping' and (s['include_controls'] or c['benchmark_scope'] in {'2025_primary','production_feedback'}) and (s['split']=='all' or c['split']==s['split'])]
    if not cases:
        raise ValueError('No eligible cases')
    predictions = {}
    started = time.perf_counter()
    for case in cases:
        with stage('evaluation.case',case_id=case['id'],eval_run_id=str(job['eval_run_id'])):
            with stage('retrieval.shortlist',k=s['k']):
                candidates = shortlist(case['input']['release_note'], s['dataset']['e2e_catalog'], s['k'])
                event('retrieval.completed',candidate_count=len(candidates))
            with stage('prompt.render',prompt_version=s['prompt_version']):
                prompt = Template(s['prompt_template']).substitute(release=json.dumps(case['input']['release_note'], ensure_ascii=False), candidates=json.dumps([c['e2e'] for c in candidates], ensure_ascii=False))
                event('prompt.rendered',characters=len(prompt),sha256=hashlib.sha256(prompt.encode()).hexdigest())
            tick = time.perf_counter()
            result = await guarded_prediction(case['input']['release_note'], candidates, s['provider'], s['prompt_template'])
            usage = result['model_usage']
            cost = None
            if usage.get('actual_provider') == 'ollama':
                cost = 0.0 # No hosted API charge; excludes local compute/electricity.
            elif usage.get('actual_provider') == s['provider'] and usage.get('actual_model') == s['model_version'] and all(usage.get(k) is not None for k in ['input_tokens','output_tokens']) and s['input_price'] is not None and s['output_price'] is not None:
                cost = (usage['input_tokens']*float(s['input_price']) + usage['output_tokens']*float(s['output_price']))/1_000_000
            predictions[case['id']] = {'shortlist':[c['e2e'] for c in candidates], 'result':result, 'latency_ms':(time.perf_counter()-tick)*1000, 'usage':usage, 'cost_usd':cost, 'timestamp':datetime.now(timezone.utc).isoformat()}
            with stage('evaluation.checkpoint',completed_cases=len(predictions)):
                with database() as db:
                    db.execute("UPDATE eval_runs SET heartbeat=now(),progress=%s WHERE eval_run_id=%s", (encode({'completed_cases':len(predictions), 'total_cases':len(cases), 'predictions':predictions}), job['eval_run_id']))
            event('case.saved',case_id=case['id'],model=usage.get('actual_model'),fallback=usage.get('fallback_used',False))
    with stage('evaluation.score',case_count=len(cases)):
        report = score(cases, predictions, {c['e2e'] for c in s['dataset']['e2e_catalog']}, s['k'])
        event('metrics.generated',precision=report['metrics']['precision']['value'],recall=report['metrics']['recall']['value'],model_errors=report['diagnostics']['model_error_count'])
    latencies = sorted(p['latency_ms'] for p in predictions.values())
    costs = [p['cost_usd'] for p in predictions.values()]
    report['metrics']['latency'] = {'mean_ms':sum(latencies)/len(latencies), 'p50_ms':latencies[(len(latencies)-1)//2], 'p95_ms':latencies[max(0, __import__('math').ceil(.95*len(latencies))-1)], 'total_seconds':time.perf_counter()-started}
    report['metrics']['cost'] = {'estimated_usd':sum(costs) if all(c is not None for c in costs) else None, 'known_estimated_usd':sum(c for c in costs if c is not None), 'currency':'USD', 'basis':'Successful response tokens × matching model prices; excludes failed attempts, retries and local compute. Ollama has zero hosted API charge.', 'coverage_cases':sum(c is not None for c in costs), 'input_usd_per_million':s['input_price'], 'output_usd_per_million':s['output_price']}
    from collections import Counter
    report['diagnostics']['actual_model_counts'] = dict(Counter(p['usage'].get('actual_model','failed') for p in predictions.values()))
    report['diagnostics']['fallback_case_count'] = sum(bool(p['usage'].get('fallback_used')) for p in predictions.values())
    report['diagnostics']['evaluation_mode'] = 'model_with_fallback_chain'
    for case in report['per_case']:
        p = predictions[case['case_id']]
        case.update(timestamp=p['timestamp'], latency_ms=p['latency_ms'], token_usage=p['usage'], cost_usd=p['cost_usd'], raw_prediction=p['result'].get('raw_prediction'), final_prediction=p['result'])
        case.update(actual_provider=p['usage'].get('actual_provider'), actual_model_version=p['usage'].get('actual_model'),
                    actual_model_digest=p['usage'].get('model_digest'), fallback_used=p['usage'].get('fallback_used',False),
                    model_attempts=p['usage'].get('attempts',[]))
    report['run'] = {k:v for k,v in s.items() if k not in {'dataset','prompt_template','trace_context'}}
    report['run'].update(eval_run_id=str(job['eval_run_id']), timestamp=job['timestamp'].isoformat(), completed_at=datetime.now(timezone.utc).isoformat(), dataset_id=s['dataset']['dataset_id'], evaluated_cases=len(cases), gold_status=s['dataset'].get('gold_status','Candidate labels; SME approval pending'))
    report['metric_explanations'] = {k:{'formula':v[0], 'why_it_matters':v[1]} for k,v in EXPLANATIONS.items()}
    return report

def main():
    initialize('regression-worker')
    with stage('worker.startup'):
        event('worker.started')
    while True:
        try:
            with database(traced=False) as db:
                # Interrupted runs are failed explicitly, never silently rerun billed model calls.
                db.execute("UPDATE eval_runs SET status='failed',error='Worker interrupted or heartbeat expired',completed_at=now() WHERE status='running' AND heartbeat < now()-interval '15 minutes'")
                job = db.execute("SELECT * FROM eval_runs WHERE status='queued' ORDER BY timestamp FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
                if job:
                    db.execute("UPDATE eval_runs SET status='running',heartbeat=now() WHERE eval_run_id=%s", (job['eval_run_id'],))
            if not job:
                time.sleep(2); continue
            try:
                asyncio.run(evaluate(job))
            except Exception:
                pass # evaluate() traces the failure and persists terminal status.
            finally:
                flush()
        except Exception as exc:
            with stage('worker.poll_failure'):
                failure(exc,component='queue')
            time.sleep(5)

if __name__ == '__main__':
    main()

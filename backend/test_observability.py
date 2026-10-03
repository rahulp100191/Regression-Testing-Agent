"""Tests use mock transports only: no model, no offline benchmark, no database."""
import asyncio
import json
from contextlib import contextmanager
import pytest
import providers
import telemetry

def test_primary_http_failure_uses_fallback_and_records_model(monkeypatch):
    monkeypatch.setenv('LLM_FALLBACK_MODELS','ollama/qwen3:0.6b,ollama/gemma3:1b')
    calls=[]
    def fake_generate(prompt,provider,model=None):
        calls.append((provider,model))
        if provider=='gemma':
            raise providers.ModelError('HTTP 429', 'http',429)
        providers.usage_capture.get().update(input_tokens=11,output_tokens=7)
        return '{"decision":"unable_to_identify","confidence":0}'
    monkeypatch.setattr(providers,'_generate',fake_generate)
    usage={}; token=providers.usage_capture.set(usage)
    try:
        result=asyncio.run(providers.generate('mock','gemma'))
    finally:
        providers.usage_capture.reset(token)
    assert json.loads(result)['decision']=='unable_to_identify'
    assert usage['actual_model']=='qwen3:0.6b'
    assert usage['fallback_used']
    assert usage['attempts'][0]['http_status']==429
    assert len(calls)==2 # Stops as soon as a fallback succeeds.

def test_invalid_json_uses_fallback_and_keeps_attempt_failure(monkeypatch):
    monkeypatch.setenv('LLM_FALLBACK_MODELS','ollama/qwen3:0.6b')
    monkeypatch.setattr(providers,'_generate',lambda prompt,provider,model=None: 'invalid' if provider=='gemma' else '{"decision":"unable_to_identify","confidence":0}')
    usage={}; token=providers.usage_capture.set(usage)
    try:
        assert json.loads(asyncio.run(providers.generate('mock','gemma')))['decision']=='unable_to_identify'
    finally:
        providers.usage_capture.reset(token)
    assert usage['attempts'][0]['status']=='failed'
    assert usage['attempts'][0]['error_category']=='invalid_response'

def test_all_routes_fail_without_exposing_transport_secrets(monkeypatch):
    monkeypatch.setenv('LLM_FALLBACK_MODELS','ollama/qwen3:0.6b')
    def fail(*args):
        raise RuntimeError('secret-api-key-must-not-leak')
    monkeypatch.setattr(providers,'_generate',fail)
    with pytest.raises(providers.ModelError,match='All configured model routes failed') as error:
        asyncio.run(providers.generate('mock','gemma'))
    assert 'secret-api-key' not in str(error.value)

def test_wrong_mapping_is_kept_for_evaluation_without_fallback(monkeypatch):
    monkeypatch.setenv('LLM_FALLBACK_MODELS','ollama/qwen3:0.6b')
    calls=[]
    def response(prompt,provider,model=None):
        calls.append(provider)
        return '{"decision":"match","confidence":99,"e2e_name":"invented catalog test"}'
    monkeypatch.setattr(providers,'_generate',response)
    raw=json.loads(asyncio.run(providers.generate('mock','gemma')))
    assert raw['e2e_name']=='invented catalog test'
    assert calls==['gemma'] # Accuracy errors must remain measurable before guardrails.

def test_queue_context_continues_same_trace_in_worker():
    telemetry.initialize('test-agent')
    with telemetry.stage('request'):
        trace_id=telemetry.trace_id()
        carrier=telemetry.carrier()
    with telemetry.stage('worker',parent=carrier):
        assert telemetry.trace_id()==trace_id
        assert trace_id is not None

def test_error_response_has_correlated_trace_without_model_call(monkeypatch):
    from app import app
    from fastapi.testclient import TestClient
    import eval_api
    @contextmanager
    def unavailable():
        raise RuntimeError('private-password')
        yield
    monkeypatch.setattr(eval_api,'database',unavailable)
    with TestClient(app) as client:
        response=client.get('/api/evaluations')
    assert response.status_code==503
    assert len(response.headers['x-trace-id'])==32
    assert response.headers['x-request-id']
    assert 'private-password' not in response.text

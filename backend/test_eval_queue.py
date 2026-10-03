"""Queue contract tests: no benchmark, database, or model calls."""
from contextlib import contextmanager
import asyncio
import json
import pytest
import eval_api
import providers

def test_queue_snapshots_versions_without_running_model(monkeypatch):
    captured = []
    class Connection:
        def execute(self, sql, values):
            captured.append((sql, values))
    @contextmanager
    def fake_database():
        yield Connection()
    monkeypatch.setattr(eval_api, 'database', fake_database)
    monkeypatch.setattr(eval_api, 'encode', lambda value: value)
    monkeypatch.setattr(providers, 'model_name', lambda provider=None: 'test-model-v1')
    monkeypatch.setattr(providers, 'provider_name', lambda: 'test-provider')
    result = eval_api.create(eval_api.EvaluationRequest())
    assert result['status'] == 'queued'
    snapshot = captured[0][1][1]
    assert snapshot['model_version'] == 'test-model-v1'
    assert len(snapshot['dataset_version']) == 64
    assert snapshot['prompt_version'].startswith('regression-v1:')
    assert len(snapshot['retrieval_version']) == 64
    assert len(snapshot['implementation_version']) == 64
    assert '$release' in snapshot['prompt_template']
    assert snapshot['dataset']['cases']

def test_invalid_split_does_not_touch_storage():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        eval_api.create(eval_api.EvaluationRequest(split='unknown'))
    assert error.value.status_code == 422

def test_provider_usage_isolated_and_propagates_through_thread(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'mock-key')
    monkeypatch.setattr(providers, 'post_json', lambda *args, **kwargs: {
        'candidates': [{'content': {'parts': [{'text': '{"decision":"unable_to_identify","confidence":0}'}]}}],
        'usageMetadata': {'promptTokenCount': 12, 'candidatesTokenCount': 4}})
    usage = {}
    token = providers.usage_capture.set(usage)
    try:
        assert json.loads(asyncio.run(providers.generate('mock prompt', 'gemma')))['decision'] == 'unable_to_identify'
    finally:
        providers.usage_capture.reset(token)
    assert usage['input_tokens'] == 12
    assert usage['output_tokens'] == 4
    assert usage['actual_provider'] == 'gemma'
    assert not usage['fallback_used']
    assert providers.usage_capture.get() is None

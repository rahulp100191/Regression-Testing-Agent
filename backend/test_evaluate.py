import asyncio
import json

import pytest

from app import ask_agent
from evaluate import calibration, score


def case(ident, decision='match', alternatives=None):
    return {'id':ident, 'source_row':2, 'input':{'release_note':{'title':ident}},
            'expected':{'decision':decision, 'e2e_name':'A' if decision=='match' else None, 'acceptable_e2e_names':alternatives or (['A'] if decision=='match' else [])}}


def pred(ident, decision, selection=None, shortlist=None, raw=None, error=None, confidence=80):
    final = {'decision':decision, 'e2e_name':selection, 'confidence':confidence, 'model_error':error}
    final['raw_prediction'] = raw if raw is not None else dict(final) if not error else None
    return {'case_id':ident, 'shortlist':shortlist or [], 'result':final}


def test_metrics_alternatives_misses_hallucinations_and_errors():
    cases = [case('alternative', alternatives=['A','B']), case('wrong'), case('missing'),
             case('none','no_matching_e2e'), case('invented','no_matching_e2e'), case('failure','no_matching_e2e')]
    predictions = {
        'alternative':pred('alternative','match','B',['B']),
        'wrong':pred('wrong','match','C',['A','C']),
        'missing':pred('missing','unable_to_identify',shortlist=['C']),
        'none':pred('none','no_matching_e2e'),
        'invented':pred('invented','unable_to_identify',raw={'decision':'match','e2e_name':'fictional','confidence':90}),
        'failure':pred('failure','unable_to_identify',error='Connection failed'),
    }
    report = score(cases,predictions,{'A','B','C'},2)
    m = report['metrics']
    assert m['recall']['value'] == pytest.approx(1/3)
    assert m['precision']['value'] == .5
    assert m['false_negatives']['count'] == 2
    assert m['false_positives']['count'] == 1
    assert m['exact_match_rate']['value'] == pytest.approx(3/6)  # empty selections agree, but decisions can differ
    assert m['top_k_retrieval_recall']['value'] == pytest.approx(2/3)
    assert m['no_match_accuracy']['value'] == pytest.approx(1/3)
    assert m['hallucination_rate']['value'] == pytest.approx(1/5)
    assert report['diagnostics']['pipeline_exact_match_rate'] == pytest.approx(2/6)
    assert report['per_case'][2]['miss_stage'] == 'retrieval'
    assert report['per_case'][1]['miss_stage'] == 'model_or_guardrail'
    assert m['confidence_calibration']['raw_model']['sample_count'] == 5


def test_calibration_known_outcomes():
    result = calibration([(.8,1),(.8,0)])
    assert result['expected_calibration_error'] == pytest.approx(.3)
    assert result['brier_score'] == pytest.approx(.34)
    assert result['high_confidence_accuracy'] == .5
    assert sum(b['count'] for b in calibration([(0,1),(1,1)])['bins']) == 2


def test_undefined_metrics_are_null_not_perfect():
    report = score([case('none','no_matching_e2e')], {'none':pred('none','no_matching_e2e')}, {'A'}, 8)
    assert report['metrics']['precision']['value'] is None
    assert report['metrics']['recall']['value'] is None
    assert report['metrics']['top_k_retrieval_recall']['value'] is None


def test_future_multi_test_sets():
    multi = case('multi')
    multi['expected']['acceptable_e2e_sets'] = [['A','B']]
    entry = pred('multi','match','A',['A'])
    report = score([multi], {'multi':entry}, {'A','B'},8)
    assert report['metrics']['recall']['value'] == .5
    assert report['metrics']['exact_match_rate']['value'] == 0
    assert report['metrics']['top_k_retrieval_recall']['value'] == .5


def test_agent_preserves_raw_hallucination_and_uses_only_inputs(monkeypatch):
    async def fake_generate(prompt, provider=None):
        assert 'golden' not in prompt
        return json.dumps({'decision':'match','e2e_name':'invented','confidence':99})
    monkeypatch.setattr('app.generate', fake_generate)
    result = asyncio.run(ask_agent({'title':'Course change'}, [{'e2e':'A'}]))
    assert result['e2e_name'] is None
    assert result['decision'] == 'unable_to_identify'
    assert result['raw_prediction']['e2e_name'] == 'invented'


def test_gemma_transport_keeps_key_out_of_url(monkeypatch):
    import providers
    monkeypatch.setenv('GEMINI_API_KEY','test-secret')
    monkeypatch.setenv('GEMMA_MODEL','gemma-test')
    def fake_post(url,payload,headers=None):
        assert 'test-secret' not in url
        assert headers['x-goog-api-key'] == 'test-secret'
        assert payload['contents'][0]['parts'][0]['text'] == 'hello'
        return {'candidates':[{'content':{'parts':[{'text':'ok'}]}}]}
    monkeypatch.setattr(providers,'post_json',fake_post)
    assert providers._generate('hello','gemma') == 'ok'


def test_ollama_and_bedrock_routing(monkeypatch):
    import sys
    import types
    import providers
    def fake_post(url,payload,headers=None):
        assert url.endswith('/api/chat') and payload['format']=='json' and not payload['stream']
        return {'message':{'content':'ollama-response'}}
    monkeypatch.setattr(providers,'post_json',fake_post)
    assert providers._generate('hello','ollama') == 'ollama-response'
    class Client:
        def converse(self,**kwargs):
            assert kwargs['messages'][0]['content'][0]['text']=='hello'
            return {'output':{'message':{'content':[{'text':'bedrock-response'}]}}}
    monkeypatch.setitem(sys.modules,'boto3',types.SimpleNamespace(client=lambda *args,**kwargs:Client()))
    assert providers._generate('hello','bedrock') == 'bedrock-response'


def test_generation_replay_and_resume_use_same_metrics(tmp_path, monkeypatch):
    import evaluate
    from types import SimpleNamespace
    data = {'dataset_id':'fixture', 'e2e_catalog':[{'e2e':'A','work_stream':'Learning'}],
            'cases':[dict(case('one'), task='release_to_e2e_mapping', benchmark_scope='2025_primary', split='test')]}
    path = tmp_path/'dataset.json'
    path.write_text(json.dumps(data),encoding='utf-8')
    calls = []
    async def fake_agent(release,candidates):
        calls.append(release)
        result = {'decision':'match','e2e_name':'A','confidence':80,'model_error':None}
        return {**result,'raw_prediction':dict(result)}
    monkeypatch.setattr(evaluate,'ask_agent',fake_agent)
    args = SimpleNamespace(dataset=path,output=tmp_path/'generated',predictions=None,resume=None,split='test',k=8,limit=None,delay=0,concurrency=2,include_controls=False,continue_on_error=False)
    assert asyncio.run(evaluate.run(args)) == 0
    assert len(calls)==1
    first = json.loads((args.output/'metrics.json').read_text())
    saved = args.output/'predictions.json'
    async def forbidden_agent(*args,**kwargs):
        raise AssertionError('Replay/resume called model unnecessarily')
    monkeypatch.setattr(evaluate,'ask_agent',forbidden_agent)
    args.predictions=saved
    args.output=tmp_path/'replayed'
    assert asyncio.run(evaluate.run(args)) == 0
    replay = json.loads((args.output/'metrics.json').read_text())
    assert first['metrics']==replay['metrics']
    args.predictions=None
    args.resume=saved
    args.output=tmp_path/'resumed'
    assert asyncio.run(evaluate.run(args)) == 0
    assert first['metrics']==json.loads((args.output/'metrics.json').read_text())['metrics']

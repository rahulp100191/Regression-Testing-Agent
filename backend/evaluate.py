"""Run fixed-dataset evaluation, or rescore saved predictions without a model/network."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import html
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app import ask_agent, build_prompt, shortlist
from providers import model_name, provider_name

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / 'outputs/workday_2025_eval/workday_2025_golden_eval.json'


def ratio(n, d):
    return n / d if d else None


def expected_sets(expected):
    if 'acceptable_e2e_sets' in expected:
        return [set(s) for s in expected['acceptable_e2e_sets']]
    if expected['decision'] != 'match':
        return [set()]
    # Existing gold labels list mutually acceptable single primary selections.
    return [{name} for name in expected.get('acceptable_e2e_names', [expected['e2e_name']])]


def predicted_set(prediction):
    if prediction.get('decision') != 'match':
        return set()
    values = prediction.get('e2e_names')
    if values is None:
        values = [prediction.get('e2e_name')]
    if not isinstance(values, list):
        values = [values]
    return {v for v in values if isinstance(v, str) and v}


def correct(prediction, expected):
    if prediction.get('decision') != 'match' and (prediction.get('e2e_name') or prediction.get('e2e_names')):
        return False
    return prediction.get('decision') == expected['decision'] and predicted_set(prediction) in expected_sets(expected)


def calibration(points):
    bins = []
    for low in range(0, 100, 10):
        sample = [(p, y) for p, y in points if low <= p * 100 < low + 10 or low == 90 and p == 1]
        if sample:
            confidence = sum(p for p, _ in sample) / len(sample)
            accuracy = sum(y for _, y in sample) / len(sample)
            bins.append({'range': f'{low}-{low+10}%', 'count': len(sample), 'mean_confidence': confidence, 'accuracy': accuracy, 'gap': abs(confidence - accuracy)})
        else:
            bins.append({'range': f'{low}-{low+10}%', 'count': 0, 'mean_confidence': None, 'accuracy': None, 'gap': None})
    high = [(p, y) for p,y in points if p >= .8]
    return {'sample_count': len(points), 'bins': bins,
            'expected_calibration_error': ratio(sum(b['count'] * (b['gap'] or 0) for b in bins), len(points)),
            'brier_score': ratio(sum((p-y)**2 for p,y in points), len(points)),
            'high_confidence_threshold': .8, 'high_confidence_count': len(high),
            'high_confidence_accuracy': ratio(sum(y for _,y in high), len(high))}


def score(cases, predictions, catalog_names, k):
    records = []
    raw_points, final_points = [], []
    confusion = Counter()
    for case in cases:
        prediction = predictions[case['id']]
        final = prediction['result']
        failed = bool(final.get('model_error'))
        raw = final.get('raw_prediction')
        if raw is None and not failed:
            raise ValueError('Successful saved prediction lacks raw_prediction; raw hallucination cannot be measured honestly.')
        chosen = predicted_set(final) if not failed else set()
        options = expected_sets(case['expected'])
        # For multiple allowed complete sets, use minimum symmetric difference.
        gold = min(options, key=lambda s: (len(s ^ chosen), sorted(s)))
        tp, fp, fn = len(chosen & gold), len(chosen - gold), len(gold - chosen)
        exact = not failed and chosen in options
        decision_correct = not failed and final.get('decision') == case['expected']['decision']
        pipeline_correct = not failed and correct(final, case['expected'])
        candidates = prediction['shortlist'][:k]
        acceptable = set().union(*options)
        retrieved = bool(acceptable & set(candidates)) if acceptable else None
        retrieval_fraction = max((ratio(len(s & set(candidates)), len(s)) or 0 for s in options), default=0) if acceptable else None
        raw_values = []
        if raw:
            raw_values = raw.get('e2e_names', [raw.get('e2e_name')])
            if not isinstance(raw_values, list):
                raw_values = [raw_values]
        hallucinations = [n for n in raw_values if isinstance(n, str) and n and n not in catalog_names]
        raw_is_correct = correct(raw, case['expected']) if raw and not failed else False
        for data, points, outcome in [(raw or {}, raw_points, raw_is_correct), (final, final_points, pipeline_correct)]:
            value = data.get('confidence')
            if not failed and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100:
                points.append((value/100, int(outcome)))
        confusion[(case['expected']['decision'], 'model_error' if failed else final.get('decision', 'invalid'))] += 1
        records.append({'case_id': case['id'], 'source_row': case['source_row'], 'title': case['input']['release_note']['title'],
                        'gold_decision': case['expected']['decision'], 'predicted_decision': final.get('decision'),
                        'accepted_gold_sets': [sorted(s) for s in options], 'selected': sorted(chosen),
                        'true_positives': tp, 'false_positives': fp, 'false_negatives': fn,
                        'missed_tests': sorted(gold-chosen), 'unnecessary_tests': sorted(chosen-gold),
                        'set_exact_match': exact, 'decision_correct': decision_correct, 'pipeline_exact_match': pipeline_correct,
                        'shortlist': candidates, 'retrieval_hit': retrieved, 'retrieval_recall': retrieval_fraction,
                        'miss_stage': 'retrieval' if fn and not retrieved else 'model_or_guardrail' if fn else None,
                        'raw_hallucinations': hallucinations, 'raw_correct': raw_is_correct,
                        'model_confidence': (raw or {}).get('confidence'), 'pipeline_confidence': final.get('confidence'),
                        'model_error': final.get('model_error')})
    tp,fp,fn = [sum(r[field] for r in records) for field in ['true_positives','false_positives','false_negatives']]
    no_match = [r for r in records if r['gold_decision']=='no_matching_e2e']
    unresolved = [r for r in records if r['gold_decision']=='unable_to_identify']
    retrieval = [r for r in records if r['retrieval_hit'] is not None]
    successes = [r for r in records if not r['model_error']]
    metrics = {
        'recall': {'value': ratio(tp,tp+fn), 'numerator': tp, 'denominator': tp+fn},
        'precision': {'value': ratio(tp,tp+fp), 'numerator': tp, 'denominator': tp+fp},
        'false_negatives': {'count': fn, 'case_ids': [r['case_id'] for r in records if r['false_negatives']]},
        'false_positives': {'count': fp, 'case_ids': [r['case_id'] for r in records if r['false_positives']]},
        'exact_match_rate': {'value': ratio(sum(r['set_exact_match'] for r in records),len(records)), 'numerator': sum(r['set_exact_match'] for r in records), 'denominator': len(records)},
        'top_k_retrieval_recall': {'k': k, 'value': ratio(sum(r['retrieval_recall'] for r in retrieval),len(retrieval)), 'retrieved_cases': sum(r['retrieval_hit'] for r in retrieval), 'eligible_cases': len(retrieval)},
        'no_match_accuracy': {'value': ratio(sum(r['decision_correct'] for r in no_match),len(no_match)), 'numerator': sum(r['decision_correct'] for r in no_match), 'denominator': len(no_match)},
        'hallucination_rate': {'value': ratio(sum(bool(r['raw_hallucinations']) for r in successes),len(successes)), 'hallucinated_responses': sum(bool(r['raw_hallucinations']) for r in successes), 'successful_raw_responses': len(successes)},
        'confidence_calibration': {'raw_model': calibration(raw_points), 'pipeline_heuristic': calibration(final_points)},
    }
    diagnostics = {'true_positive_count':tp, 'case_count':len(records), 'successful_model_responses':len(successes), 'model_error_count':len(records)-len(successes),
                   'pipeline_exact_match_rate':ratio(sum(r['pipeline_exact_match'] for r in records),len(records)),
                   'gold_match_only_exact_rate':ratio(sum(r['pipeline_exact_match'] for r in records if r['gold_decision']=='match'),sum(r['gold_decision']=='match' for r in records)),
                   'unable_to_identify_accuracy':ratio(sum(r['decision_correct'] for r in unresolved),len(unresolved)),
                   'confusion_matrix':[{'gold': g, 'prediction': p, 'count': n} for (g,p),n in sorted(confusion.items())]}
    return {'metrics':metrics, 'diagnostics':diagnostics, 'per_case':records}


EXPLANATIONS = {
 'recall': ('TP / (TP + FN)', 'Measures missed required coverage. Low recall can leave changed payroll or security behavior untested.'),
 'precision': ('TP / (TP + FP)', 'Measures useful recommendations. Low precision consumes consultant time on unnecessary tests.'),
 'false_negatives': ('Count of required golden selections that were not returned', 'Review these first. Each case shows the missed test and whether retrieval or later selection failed.'),
 'false_positives': ('Count of returned selections outside the accepted golden answer', 'Shows wasted regression effort. A wrong test on a positive case produces both an FP and an FN.'),
 'exact_match_rate': ('Cases whose complete selected set equals any accepted golden set / evaluated cases', 'Measures full set agreement. Empty matches count, so also inspect positive-case accuracy and the decision confusion matrix.'),
 'top_k_retrieval_recall': ('Average fraction of an accepted required set present in the actual K-item shortlist, across gold-match cases', 'Separates retrieval problems from LLM problems. The LLM cannot choose a correct test that it never sees.'),
 'no_match_accuracy': ('Correct no_matching_e2e decisions / gold no_matching_e2e cases', 'Checks catalog-gap recognition. unable_to_identify is a different decision and receives no credit here.'),
 'hallucination_rate': ('Successful raw responses naming an out-of-catalog test / successful raw responses', 'Measures invention before guardrails remove invalid names. A valid catalog test outside the shortlist is a retrieval/selection violation, not a catalog hallucination.'),
 'confidence_calibration': ('ECE = sum(bin_count/N * abs(mean_confidence - accuracy)); Brier = mean((confidence - correctness)^2)', 'Checks whether reported certainty matches observed correctness. Both scores are better near zero. The app score is a heuristic, not a calibrated probability.'),
}


def write_report(report, out):
    out.mkdir(parents=True, exist_ok=True)
    (out/'metrics.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
    fields = ['case_id','source_row','title','gold_decision','predicted_decision','selected','missed_tests','unnecessary_tests','miss_stage','pipeline_exact_match','model_confidence','pipeline_confidence','raw_hallucinations','model_error']
    with (out/'case_results.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in report['per_case']:
            values = {k: json.dumps(r[k], ensure_ascii=False) if isinstance(r[k], list) else r[k] for k in fields}
            # Prevent worksheet formula execution if a model produces formula-like text.
            writer.writerow({k: "'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for k,v in values.items()})
    def pct(v):
        return 'N/A (no eligible samples)' if v is None else f'{v:.1%}'
    parts = ['<!doctype html><meta charset="utf-8"><title>Workday offline evaluation</title><style>body{font:16px system-ui;margin:40px auto;max-width:1150px;padding:0 24px;color:#172c3c}table{border-collapse:collapse;width:100%;margin:24px 0}td,th{border:1px solid #ccd5dc;padding:12px;text-align:left;vertical-align:top}th{background:#edf3f7}code{background:#edf3f7;padding:2px 5px}.bad{color:#a52222}small{color:#526373}</style><h1>Workday offline evaluation</h1>',
             '<p>'+html.escape(json.dumps(report['run'], ensure_ascii=False))+'</p>',
             '<p><b>Candidate golden labels:</b> not yet SME-approved. The current agent selects one primary test per note. Alternative gold names are mutually acceptable choices, not multiple mandatory tests. Supporting tests are excluded from required selections. Knowledge-QA cases are excluded because this agent performs mapping only.</p>',
             '<p>Model errors count as failures, never as correct no-match responses. Undefined denominators display N/A. Saved-prediction replay needs no network; hosted Gemma generation requires internet.</p><table><tr><th>Metric</th><th>Result</th><th>Calculation</th><th>Why it matters</th></tr>']
    for name, (formula, why) in EXPLANATIONS.items():
        metric = report['metrics'][name]
        if name == 'confidence_calibration':
            raw = metric['raw_model']
            result = f"Raw ECE: {pct(raw['expected_calibration_error'])}; Brier: {raw['brier_score']}; high-confidence accuracy: {pct(raw['high_confidence_accuracy'])} ({raw['high_confidence_count']} samples)"
        else:
            result = str(metric['count']) if 'count' in metric else pct(metric['value'])
            if 'denominator' in metric:
                result += f" ({metric['numerator']}/{metric['denominator']})"
        parts.append('<tr>'+''.join('<td>'+html.escape(str(t))+'</td>' for t in [name,result,formula,why])+'</tr>')
    parts += ['</table><h2>Confidence reliability bins</h2><p>For each bin, compare mean reported confidence with observed decision-and-selection accuracy. Tiny bins provide weak evidence.</p><table><tr><th>Confidence bin</th><th>Samples</th><th>Mean confidence</th><th>Actual accuracy</th></tr>']
    for b in report['metrics']['confidence_calibration']['raw_model']['bins']:
        parts.append('<tr>'+''.join('<td>'+html.escape(str(t))+'</td>' for t in [b['range'],b['count'],pct(b['mean_confidence']),pct(b['accuracy'])])+'</tr>')
    parts += ['</table><h2>Missed tests and incorrect recommendations</h2><table><tr><th>Case / change</th><th>Missed</th><th>Unnecessary</th><th>Stage / error</th></tr>']
    for r in report['per_case']:
        if r['false_negatives'] or r['false_positives'] or r['model_error']:
            parts.append('<tr>'+''.join('<td>'+html.escape(str(t))+'</td>' for t in [r['case_id']+' '+r['title'], ', '.join(r['missed_tests']), ', '.join(r['unnecessary_tests']),r['model_error'] or r['miss_stage'] or 'selection'])+'</tr>')
    parts += ['</table><h2>Interpretation limits</h2><p>The primary 2025 set has only seven positive mapping cases and many catalog gaps. Overall exact agreement can look high while important tests are missed. Inspect recall and the missed-test table first. False negatives identify gold-required tests, not a measured severity ranking. Tenant applicability and test steps need SME review.</p>']
    (out/'report.html').write_text('\n'.join(parts), encoding='utf-8')


async def run(args):
    dataset = json.loads(args.dataset.read_text(encoding='utf-8'))
    cases = [c for c in dataset['cases'] if c['task']=='release_to_e2e_mapping' and (args.include_controls or c['benchmark_scope'] in {'2025_primary', 'production_feedback'}) and (args.split=='all' or c['split']==args.split)]
    if args.limit:
        cases = cases[:args.limit]
    if not cases or args.k < 1:
        raise ValueError('No eligible cases or invalid K')
    digest = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    catalog = dataset['e2e_catalog']
    catalog_names = {c['e2e'] for c in catalog}
    if args.predictions:
        payload = json.loads(args.predictions.read_text(encoding='utf-8'))
        if payload['dataset_sha256'] != digest:
            raise ValueError('Saved predictions belong to a different dataset snapshot')
        predictions = {p['case_id']: p for p in payload['predictions']}
        if len(predictions) != len(payload['predictions']):
            raise ValueError('Duplicate prediction case IDs')
        missing = {c['id'] for c in cases} - set(predictions)
        if missing:
            raise ValueError(f'Missing predictions for {len(missing)} selected cases')
        if payload['k'] != args.k:
            raise ValueError('Replay K must equal the K used to generate predictions')
        provider, model = payload['provider'], payload['model']
    else:
        predictions = {}
        provider, model = provider_name(), model_name()
        payload = {'dataset_sha256': digest, 'provider':provider, 'model':model, 'k':args.k, 'predictions':[]}
        if args.resume:
            previous = json.loads(args.resume.read_text(encoding='utf-8'))
            if any(previous.get(key) != payload[key] for key in ['dataset_sha256','provider','model','k']):
                raise ValueError('Resume dataset/provider/model/K differs from current configuration')
            for entry in previous['predictions']:
                case = next((c for c in cases if c['id']==entry['case_id']), None)
                if case is None or entry['result'].get('model_error'):
                    continue
                candidates = shortlist(case['input']['release_note'], catalog, limit=args.k)
                fingerprint = hashlib.sha256(build_prompt(case['input']['release_note'],candidates).encode()).hexdigest()
                if entry['prompt_sha256'] != fingerprint:
                    raise ValueError('Resume prompt changed; start a fresh run')
                if entry['case_id'] in predictions:
                    raise ValueError('Duplicate resume prediction')
                predictions[entry['case_id']] = entry
                payload['predictions'].append(entry)
        args.output.mkdir(parents=True, exist_ok=True)
        semaphore = asyncio.Semaphore(args.concurrency)
        start_lock = asyncio.Lock()
        stopped = asyncio.Event()
        async def evaluate_case(case):
            if case['id'] in predictions:
                return
            async with semaphore:
                if stopped.is_set():
                    return
                async with start_lock:
                    await asyncio.sleep(args.delay)
                release = case['input']['release_note']
                candidates = shortlist(release, catalog, limit=args.k)
                result = await ask_agent(release, candidates)
                entry = {'case_id':case['id'], 'shortlist':[c['e2e'] for c in candidates], 'result':result,
                         'prompt_sha256':hashlib.sha256(build_prompt(release,candidates).encode()).hexdigest()}
                predictions[case['id']] = entry
                payload['predictions'].append(entry)
                # Atomic checkpoint prevents a stopped process leaving half-written JSON.
                checkpoint = args.output/'predictions.tmp'
                checkpoint.write_text(json.dumps(payload, indent=2, ensure_ascii=False)+'\n', encoding='utf-8')
                checkpoint.replace(args.output/'predictions.json')
                print(f"[{len(predictions)}/{len(cases)}] {case['id']}: {'ERROR: '+result['model_error'] if result.get('model_error') else result['decision']}", flush=True)
                if result.get('model_error') and not args.continue_on_error:
                    stopped.set()
        await asyncio.gather(*(evaluate_case(c) for c in cases))
        if stopped.is_set():
            print('Stopped after provider/response failure; in-flight calls were saved. Fix configuration before measuring model quality.', flush=True)
        cases = [c for c in cases if c['id'] in predictions]
    report = score(cases, predictions, catalog_names, args.k)
    report['run'] = {'dataset_id':dataset['dataset_id'], 'dataset_sha256':digest, 'provider':provider, 'model':model,
                     'split':args.split, 'include_controls':args.include_controls, 'k':args.k, 'replayed':bool(args.predictions),
                     'completed_utc':datetime.now(timezone.utc).isoformat(), 'evaluated_cases':len(cases),
                     'partial_run':bool(args.limit) or (not args.predictions and len(cases)<len([c for c in dataset['cases'] if c['task']=='release_to_e2e_mapping' and (args.include_controls or c['benchmark_scope'] in {'2025_primary', 'production_feedback'}) and (args.split=='all' or c['split']==args.split)])),
                     'quality_status':'provider_errors_present' if report['diagnostics']['model_error_count'] else 'completed_candidate_gold_evaluation'}
    report['metric_explanations'] = {k:{'formula':v[0], 'why_it_matters':v[1]} for k,v in EXPLANATIONS.items()}
    write_report(report,args.output)
    print(json.dumps({'output':str(args.output.resolve()), 'metrics':report['metrics'], 'diagnostics':report['diagnostics']}, indent=2))
    return 2 if report['diagnostics']['model_error_count'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    parser.add_argument('--output', type=Path, default=ROOT/'outputs/eval_latest')
    parser.add_argument('--predictions', type=Path, help='Score saved predictions without invoking any model')
    parser.add_argument('--resume', type=Path, help='Resume compatible saved predictions, retrying failed cases')
    parser.add_argument('--split', choices=['all','development','test'], default='test')
    parser.add_argument('--k', type=int, default=8)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--delay', type=float, default=4)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--include-controls', action='store_true')
    parser.add_argument('--continue-on-error', action='store_true')
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1 or args.delay < 0 or args.concurrency < 1 or args.predictions and args.resume:
        parser.error('limit/concurrency must be positive, delay nonnegative, and replay/resume mutually exclusive')
    raise SystemExit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()

"""Bounded multi-row inference. Each row retains independent guardrails and IDs."""
import asyncio
import json
import os
from pathlib import Path
from string import Template
from uuid import uuid4
from providers import generate, usage_capture, ModelError
from safety import screen, guard_output
from telemetry import stage, event, failure

PROMPT = Path(__file__).resolve().parents[1] / 'prompts/regression/v2-batch/prompt.txt'

def settings():
    return {'batch_size':max(1,min(10,int(os.getenv('ANALYSIS_BATCH_SIZE','5')))),
            'concurrency':max(1,min(4,int(os.getenv('ANALYSIS_BATCH_CONCURRENCY','2')))),
            'max_chars':max(4000,min(60000,int(os.getenv('ANALYSIS_BATCH_MAX_CHARS','24000')))),
            'tokens_per_row':max(256,min(1024,int(os.getenv('ANALYSIS_OUTPUT_TOKENS_PER_ROW','1024'))))}

def public_prediction(record):
    return {k:v for k,v in record['prediction'].items() if k != 'raw_prediction'} | {'row_id':str(record['row_id'])}

def prompt_for(items):
    cases=[{'source_row':row['source_row'],'release':row,'candidates':[c['e2e'] for c in candidates]} for row,candidates in items]
    return Template(PROMPT.read_text(encoding='utf-8')).substitute(cases=json.dumps(cases,ensure_ascii=False,separators=(',',':')))

def pack(rows,catalog,config):
    from app import shortlist
    current=[]
    for row in rows:
        item=(row,shortlist(row,catalog))
        if current and (len(current)>=config['batch_size'] or len(prompt_for(current+[item]))>config['max_chars']):
            yield current
            current=[]
        current.append(item)
    if current:
        yield current

def validate_envelope(value,expected):
    entries=value.get('results')
    if not isinstance(entries,list) or len(entries)>len(expected):
        raise ModelError('Batch response must contain a bounded results array','invalid_response')
    ids=[]
    for entry in entries:
        identifier=entry.get('source_row') if isinstance(entry,dict) else None
        if type(identifier) is not int or identifier not in expected or identifier in ids:
            raise ModelError('Batch response contains duplicate or unknown row IDs','invalid_response')
        ids.append(identifier)

def failed(candidates,reason,report,error):
    from app import fallback_result
    result=fallback_result(reason,candidates)
    result.update(raw_prediction=None,model_error=error,model_usage={},safety={'input':report})
    return result

async def predict_batch(items,config):
    from app import validate_result,release_text,clean
    results={}; allowed=[]; reports={}
    for row,candidates in items:
        with stage('safety.input',source_row=row['source_row']):
            report=await screen({'release':row,'candidates':candidates})
        reports[row['source_row']]=report
        if report['action']=='block':
            results[row['source_row']]=failed(candidates,'Input blocked by safety screening; review the source document.',report,'input_guardrail_blocked')
        else:
            allowed.append((row,candidates))
    if not allowed:
        return results
    prompt=prompt_for(allowed)
    if len(prompt)>config['max_chars']:
        for row,candidates in allowed:
            report={**reports[row['source_row']],'action':'block','labels':['batch_input_too_large']}
            results[row['source_row']]=failed(candidates,'Release text exceeds the configured batch input budget.',report,'input_guardrail_blocked')
        return results
    batch_id=str(uuid4());usage={};token=usage_capture.set(usage)
    output_tokens=min(int(os.getenv('LLM_MAX_OUTPUT_TOKENS','4096')),len(allowed)*config['tokens_per_row'])
    try:
        expected={row['source_row'] for row,_ in allowed}
        with stage('analysis.batch',batch_id=batch_id,row_count=len(allowed)):
            event('batch.started',batch_id=batch_id,row_count=len(allowed),prompt_characters=len(prompt),max_output_tokens=output_tokens)
            text=await generate(prompt,response_validator=lambda value:validate_envelope(value,expected),max_output_tokens=output_tokens)
            from app import extract_json
            envelope=extract_json(text)
            validate_envelope(envelope,expected)
            returned={r['source_row']:r for r in envelope['results']}
            for row,candidates in allowed:
                identifier=row['source_row'];raw=returned.get(identifier)
                if raw is None:
                    results[identifier]=failed(candidates,'Model omitted this row from the batch response.',reports[identifier],'batch_row_missing')
                    continue
                output_report=await guard_output(raw,candidates,clean(release_text(row)))
                if output_report['action']=='allow':
                    result=validate_result(raw,candidates,clean(release_text(row)))
                else:
                    result=failed(candidates,'Output blocked by safety or grounding validation.',reports[identifier],None)
                result.update(raw_prediction=raw,model_error=None,safety={'input':reports[identifier],'output':output_report})
                results[identifier]=result
            event('batch.completed',batch_id=batch_id,row_count=len(allowed),returned_rows=len(returned))
    except Exception as exc:
        failure(exc,batch_id=batch_id)
        for row,candidates in allowed:
            results[row['source_row']]=failed(candidates,'Batch model request or response failed; see trace.',reports[row['source_row']],'batch_model_error')
    finally:
        usage_capture.reset(token)
    # Provider token counts belong to the whole call, not independently to every row.
    for row,_ in allowed:
        results[row['source_row']]['model_usage']={'batch_id':batch_id,'batch_row_count':len(allowed),'scope':'batch_total_do_not_sum_per_row',**usage}
    return results

async def completed_batches(rows,catalog,config):
    iterator=iter(pack(rows,catalog,config));pending={}
    def launch():
        items=next(iterator,None)
        if items is not None:
            task=asyncio.create_task(predict_batch(items,config));pending[task]=items
            return True
        return False
    try:
        for _ in range(config['concurrency']):
            if not launch():break
        while pending:
            done,_=await asyncio.wait(pending,return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                items=pending.pop(task)
                yield items,await task
                launch()
    finally:
        for task in pending:task.cancel()
        await asyncio.gather(*pending,return_exceptions=True)

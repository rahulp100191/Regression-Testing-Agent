"""Model transports. Keys stay in headers and never appear in saved predictions."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from contextvars import ContextVar

usage_capture = ContextVar("usage_capture", default=None)
output_token_override = ContextVar('output_token_override', default=None)

def output_token_limit():
    return output_token_override.get() or int(os.getenv('LLM_MAX_OUTPUT_TOKENS', '4096'))
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from telemetry import stage, event, failure

class ModelError(RuntimeError):
    def __init__(self, message, category='provider', http_status=None):
        super().__init__(message)
        self.category = category
        self.http_status = http_status
        self.safe_message = message

def model_routes(provider=None):
    primary = provider or provider_name()
    routes = [{'provider':primary, 'model':model_name(primary)}]
    for entry in os.getenv('LLM_FALLBACK_MODELS','').split(','):
        if not entry.strip():
            continue
        name, separator, model = entry.strip().partition('/')
        if not separator or name not in {'ollama','gemma','bedrock'} or not model:
            raise ValueError('LLM_FALLBACK_MODELS must contain provider/model entries')
        route = {'provider':name,'model':model}
        if route not in routes:
            routes.append(route)
    return routes


def provider_name():
    return os.getenv('LLM_PROVIDER', 'gemma' if os.getenv('GEMINI_API_KEY') else 'bedrock').strip().lower()


def model_name(provider=None):
    provider = provider or provider_name()
    if provider == 'gemma':
        return os.getenv('GEMMA_MODEL', 'gemma-4-26b-a4b-it').strip()
    if provider == 'ollama':
        return os.getenv('OLLAMA_MODEL', 'qwen3:0.6b').strip()
    if provider == 'bedrock':
        return os.getenv('BEDROCK_MODEL_ID', 'amazon.nova-lite-v1:0').strip()
    raise ValueError('LLM_PROVIDER must be gemma, ollama or bedrock')


def post_json(url, payload, headers=None):
    request = Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', **(headers or {})}, method='POST')
    attempts = max(1,min(3,int(os.getenv('LLM_HTTP_ATTEMPTS','1'))))
    for attempt in range(attempts):
        try:
            with stage('model.http', attempt=attempt+1):
                with urlopen(request, timeout=float(os.getenv('LLM_TIMEOUT_SECONDS', '45'))) as response:
                    event('model.http_response', http_status=response.status, attempt=attempt+1)
                    return json.load(response)
        except HTTPError as exc:
            event('model.http_failed', http_status=exc.code, attempt=attempt+1)
            if exc.code in {429, 500, 502, 503, 504} and attempt < attempts-1:
                event('model.retry', attempt=attempt+1)
                time.sleep(2 * (attempt + 1))
                continue
            # Do not expose the URL, response body or request headers: they may contain secrets.
            raise ModelError(f'Model endpoint returned HTTP {exc.code}. Check model access, key and quota.', 'http', exc.code) from None
        except (URLError, TimeoutError):
            raise ModelError('Model endpoint unavailable or timed out. Check connectivity and provider configuration.', 'connectivity') from None


def _generate(prompt, provider, model=None):
    model = model or model_name(provider)
    if provider == 'gemma':
        key = os.getenv('GEMINI_API_KEY', '').strip()
        if not key:
            raise ModelError('GEMINI_API_KEY is missing from .env', 'configuration')
        generation_config = {'temperature':0, 'maxOutputTokens':output_token_limit(), 'responseMimeType':'application/json'}
        if model.startswith('gemma-4-'):
            level=os.getenv('GEMMA_THINKING_LEVEL','minimal').lower()
            if level not in {'minimal','high'}:
                raise ModelError('GEMMA_THINKING_LEVEL must be minimal or high','configuration')
            generation_config['thinkingConfig']={'thinkingLevel':level}
        response = post_json(f'https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe="")}:generateContent',
                             {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
                              'generationConfig': generation_config},
                             {'x-goog-api-key': key})
        parts = response.get('candidates', [{}])[0].get('content', {}).get('parts', [])
        text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
        # Gemma may place its thought channel in plain text instead of a separate Part.
        if '<|channel>final' in text:
            text = text.split('<|channel>final', 1)[-1]
        elif '<|channel>thought' in text:
            if '<channel|>' not in text:
                raise ModelError('Model returned an unfinished thinking channel','output_truncated')
            text = text.split('<channel|>',1)[-1]
        elif '</think>' in text:
            text = text.split('</think>', 1)[-1]
        text = text.removesuffix('<turn|>').strip()
    elif provider == 'ollama':
        base = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
        capture = usage_capture.get()
        if capture is not None:
            try:
                with stage('model.version_lookup',model=model):
                    with urlopen(base+'/api/tags',timeout=3) as catalog:
                        entry = next((m for m in json.load(catalog).get('models',[]) if m.get('name')==model),{})
                        if entry.get('digest'):
                            capture['model_digest'] = entry['digest']
                            event('model.version_resolved',model=model,digest=entry['digest'])
            except Exception:
                event('model.version_unavailable',model=model)
        response = post_json(base + '/api/chat', {'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                                                 'stream': False, 'format': 'json', **({'think':False} if model.startswith(('qwen3:','deepseek-r1:')) else {}),
                                                 'options': {'temperature': 0, 'num_predict': output_token_limit()}})
        text = response.get('message', {}).get('content', '')
    elif provider == 'bedrock':
        import boto3
        from botocore.config import Config
        client = boto3.client('bedrock-runtime', region_name=os.getenv('AWS_REGION', 'ap-southeast-2'),
                              config=Config(connect_timeout=10, read_timeout=90, retries={'max_attempts': 2}))
        try:
            response = client.converse(modelId=model, messages=[{'role': 'user', 'content': [{'text': prompt}]}],
                                       inferenceConfig={'maxTokens': output_token_limit(), 'temperature': 0})
            text = ''.join(p.get('text', '') for p in response['output']['message']['content'])
        except Exception:
            raise ModelError('Bedrock request failed. Check AWS credentials, region, model access and quota.', 'bedrock_request') from None
    else:
        raise ValueError('LLM_PROVIDER must be gemma, ollama or bedrock')
    capture = usage_capture.get()
    if capture is not None:
        usage = response.get('usageMetadata', {}) if provider == 'gemma' else response.get('usage', {}) if provider == 'bedrock' else response
        capture.update(input_tokens=usage.get('promptTokenCount', usage.get('inputTokens', usage.get('prompt_eval_count'))),
                       output_tokens=usage.get('candidatesTokenCount', usage.get('outputTokens', usage.get('eval_count'))),
                       thinking_tokens=usage.get('thoughtsTokenCount'))
    if provider == 'gemma' and response.get('candidates',[{}])[0].get('finishReason') == 'MAX_TOKENS':
        raise ModelError('Model output reached its token limit; increase the output budget or reduce batch size','output_truncated')
    if not text.strip():
        raise ModelError('Model returned no text output', 'invalid_response')
    return text


async def generate(prompt, provider=None, *, response_validator=None, max_output_tokens=None):
    capture = usage_capture.get()
    attempts = []
    routes = model_routes(provider)
    with stage('model.generate', primary_provider=routes[0]['provider'], primary_model=routes[0]['model']):
        for index, route in enumerate(routes):
            usage = {}
            token = usage_capture.set(usage)
            output_token = output_token_override.set(max_output_tokens)
            tick = time.perf_counter()
            try:
                with stage('model.attempt', provider=route['provider'], model=route['model'], fallback=index>0):
                    text = await asyncio.to_thread(_generate, prompt, route['provider'], route['model'])
                    # Invalid JSON is a provider response failure; do not silently score it as no-match.
                    import re
                    stripped = re.sub(r'^```(?:json)?\s*|\s*```$', '', text.strip(), flags=re.IGNORECASE).strip()
                    try:
                        parsed=json.loads(stripped)
                    except json.JSONDecodeError:
                        # Preserve an array envelope; taking first-to-last braces loses its brackets.
                        starts=[(stripped.find(left),left,right) for left,right in [('{','}'),('[',']')] if stripped.find(left)>=0]
                        if not starts:
                            raise ModelError('Model did not return JSON','invalid_response')
                        start,left,right=min(starts)
                        parsed=json.loads(stripped[start:stripped.rfind(right)+1])
                    if response_validator is not None and isinstance(parsed,list):
                        parsed={'results':parsed}
                        text=json.dumps(parsed,ensure_ascii=False)
                    if not isinstance(parsed,dict):
                        raise ModelError('Model did not return a JSON object', 'invalid_response')
                    confidence = parsed.get('confidence')
                    if response_validator is not None:
                        response_validator(parsed)
                    elif (parsed.get('decision') not in ['match','unable_to_identify','no_matching_e2e'] or
                        not isinstance(confidence,(int,float)) or isinstance(confidence,bool) or
                        not math.isfinite(confidence) or not 0 <= confidence <= 100 or
                        (parsed['decision']=='match' and not isinstance(parsed.get('e2e_name'),str))):
                        raise ModelError('Model response lacks a valid decision, confidence or match name', 'invalid_response')
                    event('model.succeeded',provider=route['provider'],model=route['model'],fallback=index>0,**usage)
                attempts.append({**route,'status':'success','latency_ms':(time.perf_counter()-tick)*1000,**usage})
                if capture is not None:
                    capture.update(**usage, actual_provider=route['provider'], actual_model=route['model'], fallback_used=index>0, attempts=attempts)
                return text
            except Exception as exc:
                attempts.append({**route,'status':'failed','error_type':type(exc).__name__,
                                 'error_category':getattr(exc,'category','invalid_response' if isinstance(exc,json.JSONDecodeError) else 'provider'),
                                 'error_message':getattr(exc,'safe_message',None),
                                 'http_status':getattr(exc,'http_status',None),'latency_ms':(time.perf_counter()-tick)*1000,**usage})
                event('model.fallback' if index<len(routes)-1 else 'model.exhausted', provider=route['provider'],model=route['model'],
                      next_model=routes[index+1]['model'] if index<len(routes)-1 else None, error_type=type(exc).__name__, http_status=getattr(exc,'http_status',None))
            finally:
                output_token_override.reset(output_token)
                usage_capture.reset(token)
        if capture is not None:
            capture.update(attempts=attempts, fallback_used=len(routes)>1)
        raise ModelError('All configured model routes failed. See trace for attempts.', 'routes_exhausted')

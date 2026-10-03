"""OpenTelemetry spans and correlated JSON logs; never records credentials or bodies."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time
import traceback
from uuid import uuid4
from opentelemetry import trace, propagate
from opentelemetry.trace import Status, StatusCode, SpanKind

_provider = None
_service = None
logger = logging.getLogger('regression.events')

def initialize(service):
    global _provider, _service
    if _service:
        return
    _service = os.getenv('OTEL_SERVICE_NAME', service)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console)
    directory = os.getenv('APP_LOG_DIR')
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(Path(directory) / f'{_service}.jsonl', maxBytes=10_000_000, backupCount=5)
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON
    _provider = TracerProvider(resource=Resource.create({'service.name':_service}), sampler=ALWAYS_ON)
    endpoint = os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT')
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip('/')+'/v1/traces', timeout=5)))
    trace.set_tracer_provider(_provider)

def trace_id():
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, '032x') if context.is_valid else None

def carrier():
    headers = {}
    propagate.inject(headers)
    return headers

def event(name, **fields):
    # Callers supply only explicit operational metadata; request/model bodies are excluded.
    values = {k:v for k,v in fields.items() if v is not None}
    span = trace.get_current_span()
    context = span.get_span_context()
    record = {'timestamp':datetime.now(timezone.utc).isoformat(), 'service':_service,
              'event':name, 'trace_id':trace_id(), 'span_id':format(context.span_id,'016x') if context.is_valid else None, **values}
    logger.info(json.dumps(record, default=str, ensure_ascii=False))
    if span.is_recording():
        span.add_event(name, {k: v if isinstance(v,(str,int,float,bool)) else json.dumps(v,default=str) for k,v in values.items()})

def failure(exc, **fields):
    frames = [{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(exc.__traceback__)]
    trace.get_current_span().set_status(Status(StatusCode.ERROR, type(exc).__name__))
    event('error', error_type=type(exc).__name__, error_category=getattr(exc,'category',None),
          error_message=getattr(exc,'safe_message',None),
          http_status=getattr(exc,'http_status',getattr(exc,'code',None)), stack_frames=frames, **fields)

@contextmanager
def stage(name, parent=None, **attributes):
    context = propagate.extract(parent) if parent else None
    with trace.get_tracer('regression-agent').start_as_current_span(name, context=context,
            attributes={k:v for k,v in attributes.items() if v is not None},
            record_exception=False, set_status_on_exception=False) as span:
        tick = time.perf_counter()
        event('stage.started', stage=name)
        try:
            yield span
        except Exception as exc:
            failure(exc, stage=name)
            raise
        else:
            event('stage.completed', stage=name, duration_ms=round((time.perf_counter()-tick)*1000,2))

def flush():
    if _provider:
        _provider.force_flush(timeout_millis=5000)

class RequestTracingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {k.decode():v.decode() for k,v in scope.get('headers',[]) if k.lower() in {b'traceparent',b'tracestate'}}
        request_id = str(uuid4())
        status = 500
        tick = time.perf_counter()
        with trace.get_tracer('regression-agent').start_as_current_span(
                f"{scope['method']} {scope['path']}", context=propagate.extract(headers), kind=SpanKind.SERVER,
                attributes={'http.request.method':scope['method'],'url.path':scope['path'],'request.id':request_id},
                record_exception=False, set_status_on_exception=False) as span:
            event('request.received', request_id=request_id, method=scope['method'], path=scope['path'])
            async def traced_send(message):
                nonlocal status
                if message['type']=='http.response.start':
                    status = message['status']
                    message = {**message, 'headers':list(message.get('headers',[]))+[(b'x-request-id',request_id.encode()),(b'x-trace-id',(trace_id() or '').encode())]}
                    span.set_attribute('http.response.status_code',status)
                    if status >= 400:
                        span.set_status(Status(StatusCode.ERROR, f'HTTP {status}'))
                await send(message)
            try:
                await self.app(scope,receive,traced_send)
            except Exception as exc:
                failure(exc,request_id=request_id)
                raise
            finally:
                event('request.completed',request_id=request_id,http_status=status,duration_ms=round((time.perf_counter()-tick)*1000,2))

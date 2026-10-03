"""Fail-closed screening; local rules are a baseline, not a trained classifier."""
import asyncio
import json
import os
import re
import unicodedata

VERSION = 'safety-v1'
RULES = {
    'instruction_override': r'\b(ignore|disregard|override|forget)\b.{0,70}\b(?:instructions?|prompts?|system|previous|above)\b',
    'role_spoofing': r'(\[/?INST\]|<\|(?:im_start|system|im_end)\|>|<\|channel>|<channel\|>|\b(system|developer)\s*(?:message|prompt)\s*:)',
    'forced_prediction': r'\b(always|must)\s+(?:select|return|output|choose)\b.{0,60}\b(confidence|json|payroll|match|100)\b',
    'exfiltration': r'\b(reveal|send|exfiltrate|print|leak)\b.{0,80}\b(api.?key|credentials?|secrets?|system prompt|passwords?)\b',
    'code_execution': r'(?:powershell\s+-enc|curl\s+.+\|\s*(?:sh|bash)|rm\s+-rf|os\.system\s*\()',
    'credential': r'(?:AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)',
    'harmful_instruction': r'\b(?:how to|instructions? (?:for|to))\s+(?:make a bomb|steal credentials|deploy ransomware)\b',
}

def classify(text):
    normalized = unicodedata.normalize('NFKC', text)
    normalized = ''.join(c for c in normalized if unicodedata.category(c) != 'Cf')
    labels = [name for name, pattern in RULES.items() if re.search(pattern, normalized, re.I | re.S)]
    if len(text) > int(os.getenv('SAFETY_MAX_TEXT_CHARS', '60000')):
        labels.append('input_too_large')
    return {'version': VERSION, 'classifier': 'deterministic_rules', 'action': 'block' if labels else 'allow', 'labels': labels}

def _managed(text, source):
    import boto3
    from botocore.config import Config
    client = boto3.client('bedrock-runtime', region_name=os.getenv('AWS_REGION', 'ap-southeast-2'),
                          config=Config(connect_timeout=5, read_timeout=30, retries={'max_attempts': 1}))
    return client.apply_guardrail(guardrailIdentifier=os.environ['BEDROCK_GUARDRAIL_ID'],
        guardrailVersion=os.environ['BEDROCK_GUARDRAIL_VERSION'], source=source,
        content=[{'text': {'text': text, 'qualifiers': ['guard_content']}}])

async def screen(value, source='INPUT'):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    report = classify(text)
    if report['action'] == 'block':
        return report
    guardrail_id = os.getenv('BEDROCK_GUARDRAIL_ID')
    if os.getenv('SAFETY_REQUIRE_MANAGED', 'false').lower() == 'true' and not guardrail_id:
        return {**report, 'action': 'block', 'labels': ['managed_classifier_not_configured']}
    if guardrail_id:
        try:
            response = await asyncio.to_thread(_managed, text, source)
            report['managed_classifier'] = 'bedrock_guardrail'
            if response['action'] == 'GUARDRAIL_INTERVENED':
                report.update(action='block', labels=['managed_guardrail_intervened'])
        except Exception:
            report.update(action='block', labels=['managed_guardrail_unavailable'])
    return report

async def guard_output(raw, candidates, source_text):
    report = await screen(raw, 'OUTPUT')
    names = [c['e2e'] if isinstance(c, dict) else c for c in candidates]
    required = {'decision', 'business_process_name', 'e2e_name', 'confidence', 'confidence_band', 'reasoning', 'evidence', 'review_required'}
    valid = required <= raw.keys() and isinstance(raw.get('decision'),str) and raw['decision'] in {'match', 'unable_to_identify', 'no_matching_e2e'}
    valid = valid and isinstance(raw.get('confidence'), int) and not isinstance(raw.get('confidence'), bool) and 0 <= raw['confidence'] <= 100
    valid = valid and isinstance(raw.get('reasoning'), str) and len(raw['reasoning']) <= 4000
    valid = valid and isinstance(raw.get('review_required'), bool) and isinstance(raw.get('confidence_band'),str) and raw['confidence_band'] in {'low','medium','high'}
    valid = valid and (raw.get('business_process_name') is None or isinstance(raw.get('business_process_name'), str))
    valid = valid and isinstance(raw.get('evidence'), list) and len(raw['evidence']) <= 5 and all(isinstance(e,str) and 0 < len(e) <= 500 for e in raw['evidence'])
    if not valid:
        report.update(action='block', labels=report['labels'] + ['invalid_output_schema'])
    elif raw['decision'] == 'match' and (raw.get('e2e_name') not in names or not raw['evidence'] or any(e.lower() not in source_text.lower() for e in raw['evidence'])):
        report.update(action='block', labels=report['labels'] + ['ungrounded_selection_or_evidence'])
    elif raw['decision'] != 'match' and raw.get('e2e_name') is not None:
        report.update(action='block', labels=report['labels'] + ['non_match_has_selection'])
    return report

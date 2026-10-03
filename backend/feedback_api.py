"""Append-only feedback with explicit, audited golden-data promotion."""
import hashlib
import hmac
import json
import os
from uuid import UUID, uuid4
from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal
from eval_store import database, encode

router = APIRouter(prefix='/api/feedback', tags=['feedback'])

def reviewer_access(x_reviewer_key: str = Header(default='')):
    key = os.getenv('FEEDBACK_REVIEWER_KEY', '')
    if not key:
        raise HTTPException(503, 'Configure FEEDBACK_REVIEWER_KEY to enable review and golden export')
    if not hmac.compare_digest(key, x_reviewer_key):
        raise HTTPException(403, 'Reviewer access required')

class Feedback(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    author: str = Field(min_length=1, max_length=120)
    correct: bool
    expected_decision: Literal['match', 'unable_to_identify', 'no_matching_e2e']
    expected_tests: list[str] = Field(default_factory=list, max_length=50)
    missed_tests: list[str] = Field(default_factory=list, max_length=50)
    comment: str = Field(default='', max_length=4000)

class Review(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    action: Literal['approve', 'reject']
    reviewer: str = Field(min_length=1, max_length=120)
    comment: str = Field(min_length=1, max_length=4000)

def validate_labels(feedback, catalog):
    names = {c['e2e'] for c in catalog}
    for values in (feedback.expected_tests, feedback.missed_tests):
        if len(set(values)) != len(values) or any(v not in names for v in values):
            raise HTTPException(422, 'Test labels must be unique exact catalog names; describe catalog gaps in comments')
    if (feedback.expected_decision == 'match') != bool(feedback.expected_tests):
        raise HTTPException(422, 'A match needs expected tests; non-match decisions require no expected tests')
    if not set(feedback.missed_tests) <= set(feedback.expected_tests):
        raise HTTPException(422, 'Missed tests must be included in the complete expected test set')

@router.post('/rows/{row_id}', status_code=201)
def submit(row_id: UUID, feedback: Feedback):
    with database() as db:
        row = db.execute('SELECT * FROM analysis_rows WHERE row_id=%s', (row_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Analysis row not found')
        validate_labels(feedback, row['catalog'])
        prediction = row['prediction']
        if prediction.get('e2e_name') in feedback.missed_tests:
            raise HTTPException(422, 'A test already recommended by the model cannot be marked missed')
        if feedback.correct and (feedback.expected_decision != prediction['decision'] or feedback.expected_tests != ([prediction['e2e_name']] if prediction['e2e_name'] else [])):
            raise HTTPException(422, 'Correct feedback must agree with the recorded prediction')
        identifier = uuid4()
        db.execute('INSERT INTO row_feedback(feedback_id,row_id,author,correct,expected_decision,expected_tests,missed_tests,comment) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            (identifier,row_id,feedback.author.strip(),feedback.correct,feedback.expected_decision,encode(feedback.expected_tests),encode(feedback.missed_tests),feedback.comment))
    return {'feedback_id':str(identifier), 'status':'pending'}

@router.get('', dependencies=[Depends(reviewer_access)])
def history():
    with database() as db:
        return db.execute('SELECT f.*, a.release, a.prediction, a.catalog, a.versions FROM row_feedback f JOIN analysis_rows a USING(row_id) ORDER BY f.created_at DESC LIMIT 500').fetchall()

@router.get('/metrics', dependencies=[Depends(reviewer_access)])
def metrics():
    with database() as db:
        return db.execute("""SELECT count(*) AS feedback_count,
          count(*) FILTER (WHERE correct) AS reported_correct_count,
          count(*) FILTER (WHERE NOT correct) AS reported_incorrect_count,
          count(*) FILTER (WHERE status='pending') AS pending_count,
          count(*) FILTER (WHERE status='approved') AS approved_count,
          count(*) FILTER (WHERE status='rejected') AS rejected_count FROM row_feedback""").fetchone()

@router.post('/{feedback_id}/review', dependencies=[Depends(reviewer_access)])
def review(feedback_id: UUID, request: Review):
    with database() as db:
        f = db.execute('SELECT * FROM row_feedback WHERE feedback_id=%s FOR UPDATE', (feedback_id,)).fetchone()
        if f is None:
            raise HTTPException(404, 'Feedback not found')
        if f['status'] != 'pending':
            raise HTTPException(409, 'Feedback already reviewed')
        row = db.execute('SELECT * FROM analysis_rows WHERE row_id=%s FOR UPDATE', (f['row_id'],)).fetchone()
        if request.action == 'approve':
            if db.execute('SELECT case_id FROM golden_cases WHERE row_id=%s', (f['row_id'],)).fetchone():
                raise HTTPException(409, 'This row already has an approved golden case')
            if row['prediction'].get('safety', {}).get('input', {}).get('action') == 'block':
                raise HTTPException(422, 'Blocked input belongs in a security test set, not the mapping golden dataset')
            labels = Feedback(author=f['author'], correct=f['correct'], expected_decision=f['expected_decision'], expected_tests=f['expected_tests'], missed_tests=f['missed_tests'], comment=f['comment'])
            validate_labels(labels, row['catalog'])
            case_id = uuid4()
            case = {'id':f'feedback-{case_id}', 'task':'release_to_e2e_mapping', 'benchmark_scope':'production_feedback',
                'split':'development', 'source_row':row['source_row'], 'input':{'release_note':row['release']},
                'expected':{'decision':f['expected_decision'], 'e2e_name':f['expected_tests'][0] if len(f['expected_tests']) == 1 else None, 'acceptable_e2e_names':f['expected_tests'] if len(f['expected_tests']) == 1 else [],
                        'acceptable_e2e_sets':[f['expected_tests']]},
                'provenance':{'feedback_id':str(feedback_id),'reviewer':request.reviewer,'review_comment':request.comment,'versions':row['versions']}}
            db.execute('INSERT INTO golden_cases(case_id,row_id,feedback_id,case_data) VALUES (%s,%s,%s,%s)', (case_id,f['row_id'],feedback_id,encode(case)))
        status = 'approved' if request.action == 'approve' else 'rejected'
        db.execute('UPDATE row_feedback SET status=%s,reviewer=%s,review_comment=%s,reviewed_at=now() WHERE feedback_id=%s', (status,request.reviewer,request.comment,feedback_id))
    return {'status':status}

@router.get('/golden/export', dependencies=[Depends(reviewer_access)])
def export():
    with database() as db:
        rows = db.execute('SELECT g.case_data,a.catalog FROM golden_cases g JOIN analysis_rows a USING(row_id) ORDER BY g.created_at,g.case_id').fetchall()
    catalog = {}
    for row in rows:
        for item in row['catalog']:
            item = {k:v for k,v in item.items() if k != 'source_row'}
            if item['e2e'] in catalog and catalog[item['e2e']] != item:
                raise HTTPException(409, 'Catalog versions conflict; export separate catalog cohorts')
            catalog[item['e2e']] = item
    payload = {'dataset_id':'reviewed-production-feedback', 'gold_status':'human-reviewed',
               'e2e_catalog':list(catalog.values()), 'cases':[r['case_data'] for r in rows]}
    payload['dataset_version'] = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    return payload

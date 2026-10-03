"use client";
import { useState } from 'react';
const API = process.env.NEXT_PUBLIC_API_BASE ?? 'http://127.0.0.1:8000';
async function request(path: string, body?: unknown, key?: string) {
  const response = await fetch(`${API}/api/feedback${path}`, {method:body ? 'POST':'GET', headers:{'Content-Type':'application/json', ...(key ? {'X-Reviewer-Key':key}:{})}, ...(body ? {body:JSON.stringify(body)}:{})});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}
type Prediction = {decision:string; e2e_name:string|null; reasoning?:string; safety?:unknown};
type Entry = {feedback_id:string; author:string; correct:boolean; expected_decision:string; expected_tests:string[]; missed_tests:string[]; comment:string; status:string; created_at:string; release:{title:string}; catalog:unknown; versions:unknown; prediction:Prediction; reviewer?:string; review_comment?:string};
export function RowFeedback({rowId, prediction, catalog}: {rowId:string; prediction:Prediction; catalog:string[]}) {
  const [author,setAuthor] = useState('');
  const [correct,setCorrect] = useState(true);
  const [decision,setDecision] = useState(prediction.decision);
  const [tests,setTests] = useState<string[]>(prediction.e2e_name ? [prediction.e2e_name]:[]);
  const [missed,setMissed] = useState<string[]>([]);
  const [comment,setComment] = useState('');
  const [busy,setBusy] = useState(false);
  const [message,setMessage] = useState('');
  async function save() {
    setBusy(true); setMessage('');
    try { await request(`/rows/${rowId}`, {author, correct, expected_decision:correct ? prediction.decision:decision, expected_tests:correct ? (prediction.e2e_name ? [prediction.e2e_name]:[]):tests, missed_tests:correct ? []:missed, comment}); setMessage('Feedback saved for reviewer approval.'); }
    catch(e) {setMessage(e instanceof Error ? e.message:'Feedback failed');}
    finally {setBusy(false);}
  }
  return <details className="row-feedback"><summary>Give feedback</summary><div className="feedback-form">
    <label>Your name <input maxLength={120} value={author} onChange={e=>setAuthor(e.target.value)} /></label>
    <label>Was this result correct? <select value={String(correct)} onChange={e=>setCorrect(e.target.value==='true')}><option value="true">Correct</option><option value="false">Incorrect / incomplete</option></select></label>
    {!correct && <><label>Expected decision <select value={decision} onChange={e=>{setDecision(e.target.value); setTests([]); setMissed([]);}}><option value="match">Match</option><option value="unable_to_identify">Unable to identify</option><option value="no_matching_e2e">No matching E2E</option></select></label>
    {decision==='match' && <fieldset><legend>Complete expected test set</legend>{catalog.map(name=><label key={name}><input type="checkbox" checked={tests.includes(name)} onChange={e=>{setTests(v=>e.target.checked ? [...v,name]:v.filter(n=>n!==name)); if(!e.target.checked) setMissed(v=>v.filter(n=>n!==name));}} />{name}</label>)}</fieldset>}
    {tests.length>0 && <fieldset><legend>Which expected tests were missed?</legend>{tests.map(name=><label key={name}><input type="checkbox" checked={missed.includes(name)} onChange={e=>setMissed(v=>e.target.checked ? [...v,name]:v.filter(n=>n!==name))} />{name}</label>)}</fieldset>}</>}
    <label>Comment / catalog gaps <textarea maxLength={4000} value={comment} onChange={e=>setComment(e.target.value)} /></label>
    <button className="button secondary" disabled={busy || !author.trim() || (!correct && decision==='match' && tests.length===0)} onClick={save}>{busy?'Saving…':'Save feedback'}</button>
    <p role="status">{message}</p>
  </div></details>;
}
export default function FeedbackPanel() {
  const [key,setKey] = useState('');
  const [reviewer,setReviewer] = useState('');
  const [entries,setEntries] = useState<Entry[]>([]);
  const [comments,setComments] = useState<Record<string,string>>({});
  const [error,setError] = useState('');
  const [busy,setBusy] = useState(false);
  async function refresh() {setBusy(true); try {setEntries(await request('',undefined,key)); setError('');} catch(e){setError(e instanceof Error?e.message:'Request failed');} finally{setBusy(false);}}
  async function review(id:string, action:'approve'|'reject') {setBusy(true); try {await request(`/${id}/review`,{action,reviewer,comment:comments[id]},key); setEntries(await request('',undefined,key)); setError('');} catch(e){setError(e instanceof Error?e.message:'Review failed');} finally{setBusy(false);}}
  async function download() {setBusy(true); try {const data=await request('/golden/export',undefined,key); const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'})); const link=document.createElement('a');link.href=url;link.download=`golden-feedback-${data.dataset_version.slice(0,12)}.json`;link.click();URL.revokeObjectURL(url);setError('');} catch(e){setError(e instanceof Error?e.message:'Export failed');} finally{setBusy(false);}}
  return <section className="panel feedback-panel"><h2>Feedback review and golden dataset</h2><p>Review corrections against the stored prediction. Approval creates a golden case in the development split. It does not modify the held-out benchmark. Inputs blocked by safety screening cannot be promoted.</p>
    <div className="actions"><label>Reviewer name <input value={reviewer} maxLength={120} onChange={e=>setReviewer(e.target.value)} /></label><label>Reviewer key <input type="password" autoComplete="off" value={key} onChange={e=>setKey(e.target.value)} /></label><button className="button secondary" disabled={busy || !key} onClick={refresh}>Load feedback</button><button className="button secondary" disabled={busy || !key} onClick={download}>Export approved golden dataset</button></div>
    {error && <p role="alert" className="error">{error}</p>}
    {entries.length===0 && <p>No feedback loaded.</p>}
    {entries.map(entry=><article className="feedback-entry" key={entry.feedback_id}><h3>{entry.release.title}</h3><p>{entry.author} · {new Date(entry.created_at).toLocaleString()} · <b>{entry.status}</b></p><p>Result marked {entry.correct?'correct':'incorrect / incomplete'}. Expected: {entry.expected_decision}</p><p>Expected tests: {entry.expected_tests.join(', ')||'None'}</p><p>Missed tests: {entry.missed_tests.join(', ')||'None'}</p><p>Comment: {entry.comment||'None'}</p><details><summary>Source release, catalog and versions</summary><pre>{JSON.stringify({release:entry.release,catalog:entry.catalog,versions:entry.versions},null,2)}</pre></details><details><summary>Original prediction and safety results</summary><pre>{JSON.stringify(entry.prediction,null,2)}</pre></details>
      {entry.status==='pending' ? <><label>Review rationale <textarea maxLength={4000} value={comments[entry.feedback_id]??''} onChange={e=>setComments(v=>({...v,[entry.feedback_id]:e.target.value}))} /></label><div className="actions"><button className="button primary" disabled={busy||!reviewer.trim()||!comments[entry.feedback_id]?.trim()} onClick={()=>review(entry.feedback_id,'approve')}>Approve into golden dataset</button><button className="button secondary" disabled={busy||!reviewer.trim()||!comments[entry.feedback_id]?.trim()} onClick={()=>review(entry.feedback_id,'reject')}>Reject</button></div></> : <p>Reviewed by {entry.reviewer}: {entry.review_comment}</p>}
    </article>)}
  </section>;
}

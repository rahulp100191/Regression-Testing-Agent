"use client";
import EvaluationPanel from "./evaluation-panel";
import FeedbackPanel, { RowFeedback } from "./feedback-panel";

import { ChangeEvent, useEffect, useMemo, useState } from "react";

type Preview = { release: { row_count: number; sheet: string }; e2e: { row_count: number; sheet: string } };
type Result = {
  row_id: string; safety?: { input?: { action:string; labels:string[] }; output?: { action:string; labels:string[] } }; source_row: number; decision: string; business_process_name: string | null; e2e_name: string | null;
  confidence: number; confidence_band: string; reasoning: string; evidence: string[];
  review_required: boolean; shortlisted_candidates: string[]; release: Record<string, string>;
  evaluation?: { model_confidence: number; lexical_relevance: number; candidate_rank: number | null; candidate_margin: number; evidence_grounding: number; selection_agreement: number };
};

const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

function statusLabel(result: Result) {
  if (result.decision !== "match") return result.decision === "no_matching_e2e" ? "No match" : "Unable to identify";
  return result.review_required ? "Review" : "Ready";
}

export default function Home() {
  const [releaseFile, setReleaseFile] = useState<File | null>(null);
  const [e2eFile, setE2EFile] = useState<File | null>(null);
  const [tab,setTab] = useState("analysis");
  const [catalog,setCatalog] = useState<string[]>([]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [results, setResults] = useState<Result[]>([]);
  const [filter, setFilter] = useState("all");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [analysisTrace, setAnalysisTrace] = useState<string | null>(null);
  const [savedRuns,setSavedRuns] = useState<Array<{run_id:string;saved_rows:number;total_rows:number|null}>>([]);
  const [resumeRun,setResumeRun] = useState('');
  const [notice,setNotice] = useState('');
  const [activeRun,setActiveRun] = useState('');
  useEffect(()=>{
    if(!busy || !activeRun)return;
    let active=true;
    const timer=setInterval(async()=>{
      try {const response=await fetch(`${API}/api/analyses/${activeRun}`);if(!response.ok)return;const data=await response.json();if(active){setResults(data.results);setCatalog(data.catalog_names);setNotice(`${data.saved_rows}${data.total_rows ? ` / ${data.total_rows}`:''} rows saved. Batched analysis is running.`);}}
      catch {/* The main request reports terminal errors; temporary polling failures do not erase saved results. */}
    },3000);
    return ()=>{active=false;clearInterval(timer);};
  },[busy,activeRun]);

  async function refreshSaved() {
    try {const response=await fetch(`${API}/api/analyses`);if(!response.ok) throw new Error('Unable to load saved analyses');setSavedRuns(await response.json());}
    catch(e){setError(e instanceof Error?e.message:'Request failed');}
  }
  async function loadSaved() {
    if(!resumeRun)return;
    try {const response=await fetch(`${API}/api/analyses/${resumeRun}`);if(!response.ok)throw new Error('Unable to load saved rows');const data=await response.json();setResults(data.results);setCatalog(data.catalog_names);setNotice(`${data.saved_rows} saved rows loaded. Select the original workbooks and analyze to process only unsaved rows.`);setError('');}
    catch(e){setError(e instanceof Error?e.message:'Request failed');}
  }

  function chooseFile(setter: (file: File | null) => void) {
    return (event: ChangeEvent<HTMLInputElement>) => { setter(event.target.files?.[0] ?? null); setPreview(null); setResults([]); setError(""); };
  }

  async function send(endpoint: string) {
    if (!releaseFile || !e2eFile) return;
    setBusy(true); setError("");
    const form = new FormData(); form.append("release_file", releaseFile); form.append("e2e_file", e2eFile);
    if(endpoint.endsWith('analyze')) {
      const id=resumeRun || crypto.randomUUID();setActiveRun(id);setNotice('Starting batched analysis. Completed rows will appear as batches finish.');
      if(resumeRun) form.append('resume_run_id',resumeRun);else form.append('analysis_run_id',id);
    } else setActiveRun('');
    try {
      const response = await fetch(`${API}${endpoint}`, { method: "POST", body: form });
      const trace = response.headers.get('X-Trace-ID');
      const body = await response.text();
      let data;
      try { data = JSON.parse(body); } catch { throw new Error(`API returned HTTP ${response.status} with a non-JSON response. API: ${API}`); }
      if (!response.ok) throw new Error(`${data.detail ?? "Request failed"} (HTTP ${response.status}, trace ${trace ?? 'unavailable'})`);
      if (endpoint.endsWith('analyze')) setAnalysisTrace(trace);
      if (endpoint.endsWith("preview")) setPreview(data); else {setResults(data.results); setCatalog(data.catalog_names ?? []);setResumeRun(data.run_id);setNotice(`Analysis saved. ${data.reused_rows ?? 0} rows reused; batches up to ${data.batch_settings?.batch_size ?? 1} rows, ${data.batch_settings?.concurrency ?? 1} concurrent calls.`);void refreshSaved();}
    } catch (e) { setError(e instanceof Error ? e.message : "Request failed"); } finally { setBusy(false); }
  }

  const visible = useMemo(() => results.filter((result) => filter === "all" || (filter === "review" ? result.review_required : result.confidence_band === filter)), [results, filter]);

  function downloadCSV() {
    const headers = ["Source row", "Title", "Decision", "Business process", "E2E name", "Confidence", "Band", "Review required", "Reasoning"];
    const rows = results.map((result) => [result.source_row, result.release.title, result.decision, result.business_process_name ?? "", result.e2e_name ?? "", result.confidence, result.confidence_band, result.review_required ? "Yes" : "No", result.reasoning]);
    const csv = [headers, ...rows].map((row) => row.map((value) => `"${(String(value).match(/^[=+@\-\t\r]/) ? "'" + String(value):String(value)).replaceAll('"', '""')}"`).join(",")).join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); const link = document.createElement("a"); link.href = url; link.download = "workday-regression-analysis.csv"; link.click(); URL.revokeObjectURL(url);
  }

  return <main className="shell">
    <section className="hero"><div className="eyebrow">RELEASE INTELLIGENCE</div><h1>Find the regression tests that matter.</h1><p>Upload a Workday release workbook and your E2E catalog. The agent maps each change to a defensible test case, explains the evidence, and sends uncertainty to review.</p></section>
    <nav className="actions" aria-label="Workspace views">{[["analysis","Analysis"],["evaluation","Evaluation"],["feedback","Feedback review"]].map(([value,label])=><button key={value} className={`button ${tab===value?"primary":"secondary"}`} aria-pressed={tab===value} onClick={()=>setTab(value)}>{label}</button>)}</nav>
    <div hidden={tab!=="analysis"}>
    <section className="panel upload-panel"><div className="section-head"><div><span className="step">01</span><h2>Load your sources</h2></div><span className="privacy">Analysis and feedback saved in PostgreSQL</span></div>
      <div className="file-grid"><label className="file-card"><span className="file-icon">R</span><span><strong>Release notes</strong><small>{releaseFile?.name ?? "Choose a .xlsx workbook"}</small></span><input type="file" accept=".xlsx" onChange={chooseFile(setReleaseFile)} /></label><label className="file-card"><span className="file-icon blue">E</span><span><strong>E2E test catalog</strong><small>{e2eFile?.name ?? "Choose a .xlsx workbook"}</small></span><input type="file" accept=".xlsx" onChange={chooseFile(setE2EFile)} /></label></div>
      <div className="actions"><button className="button secondary" onClick={() => send("/api/preview")} disabled={!releaseFile || !e2eFile || busy}>Validate files</button><button className="button primary" onClick={() => send("/api/analyze")} disabled={!releaseFile || !e2eFile || busy}>{busy ? "Working…" : "Analyze release"}<span>↗</span></button></div>
      <p>Analysis groups up to five release rows per model call by default, with two batches in flight. Oversized batches are split to stay within the input budget.</p>
      <div className="actions"><button className="button secondary" onClick={refreshSaved}>Refresh saved analyses</button><label>Resume / view saved run <select value={resumeRun} onChange={e=>setResumeRun(e.target.value)}><option value="">Start a new analysis</option>{savedRuns.map(run=><option key={run.run_id} value={run.run_id}>{run.run_id.slice(0,8)} — {run.saved_rows}{run.total_rows ? ` / ${run.total_rows}`:''} saved rows</option>)}</select></label><button className="button secondary" disabled={!resumeRun} onClick={loadSaved}>Load saved rows</button></div>
      {notice && <p role="status">{notice}</p>}
      {preview && <div className="preview"><span>Release notes: <b>{preview.release.row_count}</b> rows · {preview.release.sheet}</span><span>E2E catalog: <b>{preview.e2e.row_count}</b> rows · {preview.e2e.sheet}</span></div>}
      {error && <div className="error">{error}</div>}
    </section>
    <section className="panel results-panel"><div className="section-head"><div><span className="step">02</span><h2>Regression map</h2></div><div className="result-actions">{results.length > 0 && <><select value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">All results</option><option value="review">Needs review</option><option value="high">High confidence</option><option value="medium">Medium confidence</option><option value="low">Low confidence</option></select><button className="button export" onClick={downloadCSV}>Download CSV</button></>}</div></div>
      {results.length === 0 ? <div className="empty"><div className="empty-mark">◎</div><h3>Your analysis will appear here</h3><p>Start by loading both workbooks above. Every release row will receive a grounded match, an explicit no-match decision, or a review flag.</p></div> : <div className="table-wrap"><table><thead><tr><th>Release change</th><th>Suggested test case</th><th>Confidence</th><th>Status</th><th></th></tr></thead><tbody>{visible.map((result) => <tr key={result.source_row}><td><div className="title-cell"><span className="row-number">{String(result.source_row).padStart(2, "0")}</span><div><strong>{result.release.title || "Untitled release note"}</strong><small>{result.release.functional_area} · {result.release.product_area}</small></div></div></td><td><div className={result.e2e_name ? "match-cell" : "match-cell muted"}><strong>{result.e2e_name ?? (result.decision === "no_matching_e2e" ? "No matching E2E case" : "Unable to identify")}</strong><small>{result.business_process_name ?? "No business process identified"}</small></div></td><td><div className="confidence"><span className={`dot ${result.confidence_band}`}></span><b>{result.confidence}%</b><small>{result.confidence_band}</small></div></td><td><span className={`status ${result.review_required ? "review" : "ready"}`}>{statusLabel(result)}</span></td><td><details><summary>View</summary><div className="details"><p>{result.reasoning}</p>{result.evidence.length > 0 && <div className="evidence">{result.evidence.map((item) => <span key={item}>“{item}”</span>)}</div>}<small>Shortlisted: {result.shortlisted_candidates.join(" · ")}</small>{result.evaluation && <div className="evaluation"><b>Evaluation signals</b><span>Catalog relevance {result.evaluation.lexical_relevance}%</span><span>Evidence grounded {result.evaluation.evidence_grounding}%</span><span>Candidate rank {result.evaluation.candidate_rank ?? "—"}</span></div>}</div></details>{result.safety && <small>Safety: {result.safety.input?.action} input / {result.safety.output?.action ?? "not called"} output</small>}<RowFeedback rowId={result.row_id} prediction={result} catalog={catalog} /></td></tr>)}</tbody></table></div>}
    </section>
    {analysisTrace && <p><a href={`http://127.0.0.1:16686/trace/${analysisTrace}`} target="_blank" rel="noreferrer">View the complete analysis trace</a></p>}
    </div>
    {tab==="evaluation" && <EvaluationPanel />}
    {tab==="feedback" && <FeedbackPanel />}
    <footer><span>Workday regression agent</span><span>Grounded suggestions · human review stays in the loop</span></footer>
  </main>;
}

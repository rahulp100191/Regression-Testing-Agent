"use client";
import { useEffect, useState } from "react";
import ObservabilityPanel from "./observability-panel";
const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
type Report = { run: Record<string, unknown>; metrics: Record<string, Record<string, unknown>>; diagnostics: Record<string, unknown>; per_case: Array<Record<string, unknown>>; metric_explanations: Record<string, { formula: string; why_it_matters: string }> };
type Run = { eval_run_id: string; status: string; timestamp?: string; error?: string; trace_id?: string; report?: Report; progress?: { completed_cases: number; total_cases: number; predictions: unknown } };
async function request(path: string, init?: RequestInit, reviewerKey?: string) {
  const response = await fetch(`${API}/api/evaluations${path}`, {...init,headers:{...init?.headers,...(reviewerKey ? {"X-Reviewer-Key":reviewerKey}:{})}});
  const text = await response.text();
  let body;
  try { body = JSON.parse(text); } catch { throw new Error(`API returned HTTP ${response.status} with a non-JSON response. Check the API address (${API}).`); }
  if (!response.ok) throw new Error(`${body.detail ?? "Evaluation request failed"} (HTTP ${response.status}, trace ${response.headers.get('X-Trace-ID') ?? 'unavailable'})`);
  return body;
}
export default function EvaluationPanel() {
  const [history, setHistory] = useState<Run[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [split, setSplit] = useState("test");
  const [datasetSource,setDatasetSource] = useState('baseline');
  const [reviewerKey,setReviewerKey] = useState('');
  const [k, setK] = useState(8);
  const [controls, setControls] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { let active = true; request("").then(data => { if (active) setHistory(data); }).catch(e => { if (active) setError(e.message); }); return () => { active = false; }; }, []);
  const id = run?.eval_run_id, status = run?.status;
  useEffect(() => {
    if (!id || !["queued", "running"].includes(status ?? "")) return;
    let active = true;
    const timer = setInterval(() => { request(`/${id}`,undefined,reviewerKey).then(data => { if (active) { setRun(data); setError(""); } }).catch(e => { if (active) setError(e.message); }); }, 2000);
    return () => { active = false; clearInterval(timer); };
  }, [id, status, reviewerKey]);
  async function refresh() { try { setHistory(await request("")); setError(""); } catch (e) { setError(e instanceof Error ? e.message : "Request failed"); } }
  async function start() {
    setBusy(true); setError("");
    try { setRun(await request("", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({split, k, include_controls: controls, dataset_source:datasetSource}) },reviewerKey)); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : "Request failed"); }
    finally { setBusy(false); }
  }
  async function open(id: string) { try { setRun(await request(`/${id}`,undefined,reviewerKey)); setError(""); } catch (e) { setError(e instanceof Error ? e.message : "Request failed"); } }
  function download() {
    if (!run?.report) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(run.report, null, 2)], {type:"application/json"}));
    const link = document.createElement("a"); link.href = url; link.download = `evaluation-${run.eval_run_id}.json`; link.click(); URL.revokeObjectURL(url);
  }
  const report = run?.report;
  return <section className="panel eval-panel">
    <ObservabilityPanel />
    <div className="section-head"><div><span className="step">03</span><h2>Offline evaluation</h2></div><button className="button secondary" onClick={refresh}>Refresh history</button></div>
    <p>Evaluate the fixed golden dataset with the configured model. Runs are queued in the regression-test container and saved in PostgreSQL. Candidate golden labels await SME approval.</p>
    <div className="actions">
      <label>Reviewer key for feedback runs <input type="password" autoComplete="off" value={reviewerKey} onChange={e=>setReviewerKey(e.target.value)} /></label>
      <label>Dataset <select value={datasetSource} onChange={e=>{setDatasetSource(e.target.value); if(e.target.value==='approved_feedback') setSplit('development');}}><option value="baseline">Original benchmark</option><option value="approved_feedback">Approved production feedback</option></select></label>
      <label>Split <select value={split} onChange={e => setSplit(e.target.value)}><option value="test">Test</option><option value="development">Development</option><option value="all">All</option></select></label>
      <label>Shortlist size <input type="number" min={1} max={100} value={k} onChange={e => setK(Number(e.target.value))} /></label>
      <label><input type="checkbox" checked={controls} onChange={e => setControls(e.target.checked)} /> Include controls</label>
      <button className="button primary" onClick={start} disabled={busy || !Number.isInteger(k) || k < 1 || k > 100 || ["queued","running"].includes(status ?? "")}>{busy ? "Queueing…" : "Run offline evaluation"}</button>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="actions"><label>Saved runs <select value={run?.eval_run_id ?? ""} onChange={e => { if(e.target.value) void open(e.target.value); }}><option value="">Select a run</option>{history.map(item => <option key={item.eval_run_id} value={item.eval_run_id}>{item.timestamp ? new Date(item.timestamp).toLocaleString() : ""} · {item.status} · {item.eval_run_id.slice(0,8)}</option>)}</select></label></div>
    {run && <p aria-live="polite">Run <code>{run.eval_run_id}</code> · <b>{run.status}</b>{run.error && ` · ${run.error}`}</p>}
    {run?.trace_id && <p><a href={`http://127.0.0.1:16686/trace/${run.trace_id}`} target="_blank" rel="noreferrer">Open this run’s complete trace</a> · <code>{run.trace_id}</code></p>}
    {run?.progress && <p>{run.progress.completed_cases} / {run.progress.total_cases} cases saved</p>}
    {run?.status === "failed" && run.progress && <details><summary>Saved results before interruption</summary><pre>{JSON.stringify(run.progress.predictions,null,2)}</pre></details>}
    {report && <>
      <button className="button secondary" onClick={download}>Download complete JSON report</button>
      <details open><summary>Versions and configuration</summary><dl className="eval-versions">{Object.entries(report.run).map(([key,value]) => <div key={key}><dt>{key}</dt><dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd></div>)}</dl></details>
      <div className="table-wrap"><table><thead><tr><th>Metric</th><th>Result</th><th>Definition</th></tr></thead><tbody>{Object.entries(report.metrics).map(([name,metric]) => <tr key={name}><td>{name.replaceAll("_", " ")}</td><td>{"value" in metric ? metric.value == null ? "N/A" : `${(Number(metric.value) * 100).toFixed(2)}%` : "count" in metric ? String(metric.count) : <pre>{JSON.stringify(metric,null,2)}</pre>}</td><td>{report.metric_explanations[name]?.formula ?? (name === "cost" ? "Provider-reported tokens × configured prices. Missing prices or usage means unknown cost. Retries may be excluded." : "Wall-clock latency includes retries; p95 uses nearest rank.")}<small>{report.metric_explanations[name]?.why_it_matters}</small></td></tr>)}</tbody></table></div>
      <details><summary>Diagnostics and confusion matrix</summary><pre>{JSON.stringify(report.diagnostics,null,2)}</pre></details>
      <h3>Every case</h3><div className="table-wrap"><table><thead><tr><th>Case / release</th><th>Expected → predicted</th><th>Missed / unnecessary tests</th><th>Latency / cost</th><th>Details</th></tr></thead><tbody>{report.per_case.map(item => <tr key={String(item.case_id)}><td><b>{String(item.case_id)}</b><small>{String(item.title)}</small></td><td>{String(item.gold_decision)} → {String(item.predicted_decision)}<small>{JSON.stringify(item.selected)}</small></td><td>Missed: {JSON.stringify(item.missed_tests)}<small>Unnecessary: {JSON.stringify(item.unnecessary_tests)}</small></td><td>{Number(item.latency_ms).toFixed(0)} ms<small>{item.cost_usd == null ? "Cost unknown" : `$${Number(item.cost_usd).toFixed(6)}`}</small></td><td><details><summary>Full case result</summary><pre>{JSON.stringify(item,null,2)}</pre></details></td></tr>)}</tbody></table></div>
    </>}
  </section>;
}

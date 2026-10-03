"use client";
import { useEffect, useState } from "react";
const API = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";
type Diagnostic = { service: string; storage: string; model_routes: Array<{provider: string; model: string; readiness?: string}>; local_model_state: string; trace_id: string; dashboard_url: string };
export default function ObservabilityPanel() {
  const [data,setData] = useState<Diagnostic | null>(null);
  const [error,setError] = useState("");
  const [busy,setBusy] = useState(false);
  useEffect(() => { let active = true; fetch(`${API}/api/diagnostics`).then(async response => {
    if (!response.ok) throw new Error(`Diagnostics returned HTTP ${response.status}`);
    return response.json();
  }).then(value => { if(active) setData(value); }).catch(e => { if(active) setError(e.message); }); return () => {active=false;}; },[]);
  async function refresh() {
    setBusy(true);
    try {
      const response = await fetch(`${API}/api/diagnostics`);
      if (!response.ok) throw new Error(`Diagnostics returned HTTP ${response.status}`);
      setData(await response.json()); setError("");
    } catch(e) { setError(e instanceof Error ? e.message : "Diagnostics unavailable"); }
    finally { setBusy(false); }
  }
  return <div className="observability">
    <h3>Agent observability</h3>
    <p>API: <code>{API}</code> · PostgreSQL: <b>{data?.storage ?? "Checking…"}</b></p>
    <div className="actions"><a className="button secondary" href={data?.dashboard_url ?? "http://127.0.0.1:16686"} target="_blank" rel="noreferrer">Open tracing dashboard</a><button className="button secondary" disabled={busy} onClick={refresh}>Refresh diagnostics</button></div>
    {error && <p className="error">{error}</p>}
    {data && <><p>Model order: {data.model_routes.map((route,index) => `${index+1}. ${route.provider}/${route.model}${route.readiness ? ` (${route.readiness.replaceAll('_',' ')})` : ''}`).join(" → ")}</p><p>Local fallbacks: <b>{data.local_model_state?.replaceAll('_',' ')}</b></p>
      <p><a href={`${data.dashboard_url}/trace/${data.trace_id}`} target="_blank" rel="noreferrer">View latest diagnostics trace</a></p></>}
    <p>Follow each request through queueing, retrieval, model attempts, validation, scoring, and persistence. Trace events include durations, safe error details, and fallback decisions.</p>
  </div>;
}

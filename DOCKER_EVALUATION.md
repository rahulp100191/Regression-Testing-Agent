Configure model credentials in `.env` and add `POSTGRES_PASSWORD` with a strong password. The stack passes credentials separately, so special characters work. Start the stack with:

```powershell
docker compose up --build -d
```

Run `docker compose up -d ollama ollama-init` once to start model-weight downloads (the VS Code tasks do this automatically). Open http://localhost:3000 and click **Run offline evaluation** when model readiness is ready in the UI. Starting containers does not queue an evaluation. The dedicated `regression-test` worker polls PostgreSQL for explicitly requested runs. No Docker socket is exposed to the API.

Reports and every per-case result persist as JSONB in `eval_runs` in the `regression-db` volume. POST `/api/evaluations` queues a run, GET `/api/evaluations` lists history, and GET `/api/evaluations/{eval_run_id}` retrieves status and report. The UI polls status, opens historical reports, and downloads complete JSON. `docker compose down` preserves history. PostgreSQL has no exposed host port.

Reports include all requested fields plus false positives, precision/recall denominators, retrieval recall, confidence calibration, confusion matrix, model errors, latency mean/p50/p95, and provider token usage. Each case includes expected, raw, and final predictions. Missing usage or prices produces unknown cost. Optional environment variables `EVAL_INPUT_USD_PER_MILLION` and `EVAL_OUTPUT_USD_PER_MILLION` enable cost estimates; estimates may exclude retries.

Prompt source lives in `prompts/regression/v1/`. Create a new version folder for revisions and update the application and API prompt paths together. Runs snapshot the template and dataset in PostgreSQL and record content hashes. Retrieval or threshold changes after queueing cause explicit failure. Interrupted running jobs become failed after 15 minutes; they are never automatically reissued. Hosted models require internet for this offline benchmark. Golden labels await SME approval.

API and worker model settings must match. Bedrock credentials must come from environment variables or deployment IAM roles; host AWS profiles are not automatically mounted. The Compose stack is for local use.
## Run and debug in VS Code

Open this folder in VS Code, install the recommended Python Debugger extension, and open **Run and Debug** (Ctrl+Shift+D). Select **Run and debug full app** and press F5. This starts PostgreSQL, the API, the evaluation worker, and the development UI, then attaches Python debuggers and opens Chrome at http://localhost:3000.

Set API breakpoints in `backend/eval_api.py` (queue creation) or `backend/app.py` (analysis). Set evaluation breakpoints in `backend/eval_worker.py` inside `evaluate`. Click the UI evaluation button when you want the worker to run; startup does not create a run. UI source maps are available through the Next.js development server.

Shift+F5 disconnects the debuggers; containers remain available. Use **Terminal → Run Task → Stop app containers** to stop them while preserving database history. The **Run app (Docker)** task starts the regular app without debugging. Individual debugger configurations attach to an already running debug stack.

Avoid pausing a worker breakpoint for more than 15 minutes because stalled-run detection can mark the run failed. Restart the debug stack task after changing Python code; UI changes reload automatically.
# Tracing, logs, and model fallback

Open http://127.0.0.1:16686 for the **Jaeger** dashboard, or use **Open tracing dashboard** in the UI. Select service `regression-api` or `regression-worker`. Every evaluation run has an **Open this run’s complete trace** link. The API returns `X-Trace-ID` and `X-Request-ID` headers, and stores queue trace context in PostgreSQL so the worker continues the same trace across processes.

The trace tree covers HTTP receipt and response, SQL operations, queue insertion, queue wait, worker claim, per-case retrieval, prompt rendering (version/hash), provider attempts, HTTP responses, retries, fallback selection, validation, checkpoints, scoring, persistence, and failure status. Structured operational log events appear on spans in the dashboard and in rotating JSONL files in the `app-logs` volume. Request bodies, workbook rows, model prompts/output, passwords, API keys, full URLs, and raw exception messages are deliberately excluded from telemetry. Raw/final predictions remain in evaluation reports in PostgreSQL.

Traces persist in the `trace-data` volume with **7-day retention**. JSON logs rotate at 10 MB, with five backups per service. View stdout with `docker compose logs -f api regression-test`. Failed spans show exception type, category, HTTP status when available, and safe stack locations. Idle queue polling is not traced; errors are traced. Debuggers can attach as before.

The primary model remains the provider/model in `.env`. The default fallback chain is **Gemma → Ollama Qwen3 0.6B → Ollama Qwen2.5 0.5B** when Gemma is the configured primary. CPU-only Ollama uses the community-maintained `alpine/ollama` image from Docker Hub, pinned to the installed image digest (Ollama 0.34.4); set the service images to `ollama/ollama` if GPU support is needed. `ollama-init` downloads model weights only; it never runs inference. Model data persists in `ollama-models`. Check readiness with `docker compose logs ollama-init` or `docker compose exec ollama ollama list`. Local model weight digests are recorded with successful model attempts and each case result when available.

Override `LLM_FALLBACK_MODELS` with a comma-separated `provider/model` chain, or set it empty to disable fallbacks for a strict single-model benchmark. Each case stores every attempted provider/model, error category, latency, token usage, winning model, and fallback flag. Reports aggregate actual model counts, so mixed-model runs are visible. Fallback is triggered by transport/response errors, including invalid JSON; an incorrect mapping does not silently switch models. Small local models are recovery options; their quality must be measured with your benchmark.

Optional `LLM_HTTP_ATTEMPTS` controls bounded retries (default one); `LLM_TIMEOUT_SECONDS` limits each attempt (default 45 seconds). Slow CPU inference may need 120 seconds. Cost estimates price only the winning primary-model response; local Ollama has no hosted API charge. Failed attempts, retries, and local compute costs are excluded explicitly.

The UI API base is `http://127.0.0.1:8000`. On this machine `localhost:8000` also resolves to a different Node listener over IPv6; the explicit IPv4 URL avoids that routing conflict. `GET /api/evaluations` reads PostgreSQL only and does not call Gemma. `GET /api/diagnostics` reports the API service, storage connectivity, model chain, and its trace link without making an inference call.

This is a local, single-node observability deployment with production tracing conventions. Jaeger Badger storage cannot scale horizontally. Before a shared production deployment, add authentication/TLS, external scalable storage, secrets management, backup/restore, alerting, and sampling. The local dashboard binds only to loopback.

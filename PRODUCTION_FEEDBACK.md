# Safety and reviewed production feedback

Analysis now requires PostgreSQL. Source rows, the uploaded catalog, predictions and version metadata are persisted. Configure a retention policy before uploading confidential production data. Model providers still receive allowed release content.

## Local setup

Set a strong `POSTGRES_PASSWORD` and `FEEDBACK_REVIEWER_KEY` in `.env`. Never put the reviewer key in a `NEXT_PUBLIC_*` variable. The review UI holds it in memory and sends it in the `X-Reviewer-Key` header; use HTTPS outside localhost. The API and worker need matching safety configuration.

```powershell
docker compose up --build -d postgres api frontend regression-test
```

Open http://localhost:3000. Analyze files, open **Give feedback** for each row, enter your name, mark correct/incorrect, choose the complete expected test set and missed tests, and leave a comment. Feedback is append-only. Catalog gaps belong in comments, not invented test names.

In **Feedback review**, enter a reviewer name and the reviewer key, load feedback, inspect the source/prediction, and approve or reject with a rationale. Approval and golden insertion share one transaction. Only one golden case per analyzed row can be approved. Repeated approval returns a conflict. Names are self-reported, not verified identities.

Approved cases are held in `golden_cases`; the original benchmark file is unchanged. Export returns a content-versioned, scorer-compatible dataset. Multiple expected tests are a required set, not interchangeable alternatives. The current model returns one test, so multi-test corrections expose its coverage limitation. Catalog conflicts prevent a combined export rather than silently mixing versions.

In **Evaluation**, select **Approved production feedback** and the development/all split to run against a snapshot of reviewed cases. New approvals never change queued runs. Exported data can also be evaluated with `backend/evaluate.py --dataset <export.json> --split development`. Do not describe these reviewed development cases as an unseen held-out test set.

## Safety pipeline

`backend/safety.py` classifies Unicode-normalized text using deterministic rules for instruction override, role spoofing, forced predictions, credential disclosure, suspicious execution requests and selected harmful instructions. It examines release data and candidate metadata before inference; the complete catalog is screened before analysis. Suspicious input returns a review-required blocked result without calling the inference model. Text size and workbook size/expanded archive limits are enforced.

These rules are a transparent baseline, not a trained universal safety classifier. False positives and false negatives remain possible. Encoded or novel attacks can bypass pattern rules. No detector guarantees prompt-injection prevention.

For managed classification, create and publish an Amazon Bedrock Guardrail with prompt-attack, harmful-content and sensitive-information policies appropriate to this workload. Set:

```dotenv
SAFETY_REQUIRE_MANAGED=true
BEDROCK_GUARDRAIL_ID=<guardrail identifier>
BEDROCK_GUARDRAIL_VERSION=<published version>
```

The application calls `ApplyGuardrail` separately for INPUT and OUTPUT, including when the inference provider is Ollama or Google. This sends screened content to AWS and incurs guardrail usage; consider data residency and policy requirements. Grant the service identity `bedrock:ApplyGuardrail` on the intended guardrail. Missing configuration or managed service failure blocks processing. Cloud guardrail resources are not automatically provisioned or configured by this code.

Output is screened for safety, complete schema, exact candidate membership and source-quoted evidence. Blocked output is replaced with a safe review-required result. Raw output remains in protected review storage for audit/evaluation and is omitted from analysis responses. Blocked input cannot be promoted into the mapping benchmark; curate separate adversarial security datasets.

Source: https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-independent-api.html

## Faster uploaded analysis and recovery

For hosted Gemma 4, `GEMMA_THINKING_LEVEL=minimal` explicitly disables thinking; use `high` to enable it. This changes generation behavior and must be evaluated for mapping quality. The application also strips Gemma's documented thought-channel envelope before parsing JSON and reports token-limit truncation explicitly. Thinking settings are recorded in analysis/evaluation versions. See [Google's Gemma API thinking documentation](https://ai.google.dev/gemma/docs/core/gemma_on_gemini_api#thinking).

The local deployment uses a 120-second HTTP timeout and at most two HTTP attempts for retryable status codes. Five-row batches can generate more output than one row: the output allowance is now 1024 tokens per row, capped by the existing global 4096-token setting. Provider outages, malformed JSON or truncation still produce explicit row errors; batch requests cannot guarantee a particular completion time.

`/api/analyze` now uses the versioned `regression-v2-batch` prompt and groups up to five release rows into one inference request. Two batches can be in flight per request. Configure `ANALYSIS_BATCH_SIZE` (1–10), `ANALYSIS_BATCH_CONCURRENCY` (1–4), `ANALYSIS_BATCH_MAX_CHARS` and `ANALYSIS_OUTPUT_TOKENS_PER_ROW` in `.env`, then recreate the API container. For a CPU-only Ollama server configured for one parallel request, use concurrency 1.

Each case has an integer source-row identifier and its own shortlist. Blocked inputs are excluded before inference, and each returned prediction is independently checked against that row's candidates and source evidence. Duplicate/unknown output identifiers invalidate the batch. Missing outputs receive explicit row errors; the system does not silently retry each row individually. Provider fallback routes still apply to batch transport/envelope failures. Input-size limits can split a group into smaller batches or block an individually oversized row. Output budgets scale with the number of allowed rows and remain capped by `LLM_MAX_OUTPUT_TOKENS`.

The preferred response envelope is `{"results":[...]}`. A provider's valid top-level JSON array is normalized to that envelope before the same row-ID and output checks. It is not parsed by discarding array brackets. The live sample in `outputs/batch_smoke_summary.json` analyzed five sample release notes in one hosted Gemma call in about 19.7 seconds with no provider/output guard errors; it does not establish mapping accuracy or whole-workbook completion time.

Completed batches are checkpointed in PostgreSQL as they finish; final results are sorted by source row. The frontend supplies an `analysis_run_id` for new requests and polls saved results every three seconds, showing rows and progress as batches finish. Token usage is labeled as a batch total with a shared batch ID: do not sum the same usage once for every row. Five rows per call reduces request count, but output generation still scales with row count; a fivefold time/cost improvement is not guaranteed. Concurrency is bounded per analysis request, not globally across users. Production quota control still requires a shared queue/limiter.

Use **Refresh saved analyses**, choose a run and **Load saved rows** to recover results. Upload the original two workbooks, keep that run selected and click **Analyze release** to process only unsaved rows. The API accepts the optional multipart `resume_run_id` field. Reused rows keep their original feedback IDs and prediction versions; resumed rows record the new batch version. Saved rows and catalog content must match, and new runs also verify a complete dataset hash. Legacy single-row checkpoints cannot verify source rows they never stored. A database advisory lease rejects concurrent resumes of the same run.

This remains a synchronous HTTP endpoint: a browser/proxy timeout can still interrupt the response. Closing the browser is not a reliable server-side cancellation mechanism. Resume does not regenerate saved failure/blocked rows. `GET /api/analyses` and `GET /api/analyses/{run_id}` expose local recovery/history; protect them with authenticated tenant authorization before public deployment.

The fixed-dataset evaluation worker currently retains its single-row inference path. Its scores are not a validation of the new multi-row prompt. Benchmark the batch prompt on held-out labeled cases before adopting it for production release decisions.

## Production launch work still required

- Replace self-reported names/shared reviewer secrets with SSO, verified identities and reviewer roles. Authenticate analysis and feedback submission, and enforce tenant ownership on every row/result. Current Compose ports are localhost only; this is not a public multi-tenant service.
- Add idempotent feedback submission, row-level correction history and reviewer conflict resolution. Current golden approvals are immutable; superseding an approved case needs an explicit audited workflow.
- Add paginated review/history with filtering, sampling, and a dashboard segmented by model/prompt/catalog version. `/api/feedback/metrics` exposes submission counts, not unbiased model accuracy.
- Add managed database migrations, encrypted backups, recovery drills, deletion/retention procedures and access audit logs. Auto-created tables are a development convenience.
- Move uploaded analysis into background jobs with checkpoints, progress, cancellation, bounded model concurrency, rate limits, task idempotency and dead-letter handling.
- Maintain SME-approved representative labels and an untouched test set. Review data leakage, duplicate release notes, sensitive content and catalog-version compatibility before dataset publication.
- Validate classifier attack-detection/false-positive rates, measure added guardrail latency/cost, test evasions and outages, and track safety blocks separately from mapping accuracy.
- Define quality/cost/latency launch gates, staged rollouts, drift checks, monitoring ownership and rollback.

## Verification

Unit tests: `backend/.venv/Scripts/python.exe -m pytest backend -q`.
PostgreSQL integration (mock inference, scoped cleanup): `docker compose run --rm -e RUN_DB_TESTS=1 api pytest test_feedback_integration.py -q`.
Frontend production build: `cd frontend; npm run build`.

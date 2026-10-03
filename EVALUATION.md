# Run locally and evaluate the regression agent

The local backend uses Gemma through your existing Google API key in `.env`. This means the app runs on your computer while model inference uses Google's API and needs internet. A fully disconnected alternative is provided through Ollama. AWS uses Bedrock through the same agent and validation path.

The Google model-list check on this machine returned `gemma-4-26b-a4b-it` and `gemma-4-31b-it`. The local `.env` is configured with the first. Model availability can change; use a model your key can access. Existing `GEMINI_MODELS` values are preserved but not used for Gemma selection. API keys are not copied into reports.

## Start the local app

Run from the repository root in PowerShell:

```powershell
python -m venv backend/.venv
backend/.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
backend/.venv/Scripts/python.exe -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 8001
```

In another terminal:

```powershell
Set-Location frontend
npm install
# frontend/.env.local should contain NEXT_PUBLIC_API_BASE=http://localhost:8001
npm run dev
```

Open <http://localhost:3000>. Port 8001 is used because another application was already listening on 8000. The 2025 and original 2026 workbook formats are both accepted. Preview validates inputs without calling a model; Analyze calls the configured model for each release row. The health endpoint identifies the provider/model but does not verify model connectivity.

## Generate predictions and score them

```powershell
# Recommended initial development run
backend/.venv/Scripts/python.exe backend/evaluate.py --split development --output outputs/eval_development

# Held-out evaluation after prompt/configuration changes are finished
backend/.venv/Scripts/python.exe backend/evaluate.py --split test --output outputs/eval_test

# All 63 primary 2025 mapping cases for a diagnostic baseline
backend/.venv/Scripts/python.exe backend/evaluate.py --split all --output outputs/eval_all
```

Default K is 8. Use `--k 5` to evaluate a different actual shortlist size. Only case inputs and catalog candidates are sent to the model; golden answers are held back for scoring. The 22 knowledge-QA cases are not scored by this runner because the agent currently performs E2E mapping, not question answering. Two 2024 source records are excluded unless `--include-controls` is supplied.

The runner writes:

- `predictions.json`: raw model predictions, guarded agent outputs, actual shortlists, model and dataset/prompt fingerprints.
- `metrics.json`: all nine metrics, counts, confidence bins, decision confusion matrix and per-case outcomes.
- `case_results.csv`: missed and unnecessary tests, error details and retrieval/model failure stages.
- `report.html`: a readable local report with results, formulas and explanations. Open it in any browser.

Predictions are saved after every completed call. A provider/response failure stops the run by default and exits with code 2. The report is marked partial and shows the model error. It does not claim the error was a correct no-match. `--continue-on-error` explicitly allows measuring operational failures across the remaining cases. Do not compare a partial run with a full benchmark.

The runner uses two concurrent model calls by default and spaces request starts by four seconds. Use `--concurrency 1` for sequential execution or reduce concurrency if your model quota is limited. In-flight responses are saved before a stop. Compatible checkpoints can be resumed without repeating successful calls:

```powershell
backend/.venv/Scripts/python.exe backend/evaluate.py --split all --resume outputs/eval_all/predictions.json --output outputs/eval_all
```

Resume checks the dataset, provider, model, K and prompt fingerprints. Failed calls are retried; successful calls are reused. A resumed run therefore reflects completed model responses after retry, not first-attempt service reliability. The separate failure-run reports and provider error counts should be retained when studying operational reliability.

## Rescore with no network

```powershell
backend/.venv/Scripts/python.exe backend/evaluate.py --split all --predictions outputs/eval_all/predictions.json --output outputs/eval_replay
```

Use the same split, K, controls and limit as the generation run. Replay verifies the dataset fingerprint and required case IDs. This is completely offline: it does not call Gemma, Ollama, Bedrock or AWS. Generating new predictions with Google-hosted Gemma is not disconnected inference.

## Understand the nine metrics

Each `(release note, required primary test)` is a selection opportunity. A correct selected test is a true positive (TP). A required test that was not selected is a false negative (FN). A selected test outside the accepted gold answer is a false positive (FP). Selecting a wrong catalog test when another test was required creates **both an FP and an FN**.

The current gold dataset allows one primary test per note. `acceptable_e2e_names` contains alternative correct single answers. If either HR or ESS is acceptable, selecting either counts as one TP; it does not count the other alternative as a missed test. Supporting tests are useful suggestions, not mandatory primary selections. Future datasets can supply `acceptable_e2e_sets` for genuine multiple-required-test answers; the scorer supports them, but the current agent still returns only one test.

| Metric | Calculation | Why it matters |
| --- | --- | --- |
| Recall | TP / (TP + FN) | Coverage of required tests. Low recall means release risks may be missed. |
| Precision | TP / (TP + FP) | Proportion of recommended tests that are useful. Low precision wastes regression effort. |
| False negatives | Count of required gold selections missed | Inspect these first, especially payroll, time-to-pay and security scenarios. The report identifies the specific missed test and stage. |
| False positives | Count of selected tests outside the accepted gold answer | Helps reduce unnecessary execution and review. On ambiguous gold cases, a forced match is scored as an FP under the conservative selection policy. |
| Exact match rate | Cases where the full selected set equals an accepted gold set / all cases | Complete set agreement, including empty sets. Also inspect decision-and-set accuracy, because an incorrect abstention reason can still return the correct empty set. |
| Top-K retrieval recall | Mean fraction of an accepted required set appearing in the actual K-item shortlist, across gold-match cases | Diagnoses retrieval. With this single-primary gold, it is the number of positive cases where any accepted alternative was shortlisted / positive cases. |
| No-match accuracy | Correct `no_matching_e2e` decisions / gold `no_matching_e2e` cases | Checks recognition of catalog gaps. `unable_to_identify` is different and does not receive credit. |
| Hallucination rate | Successful raw responses naming an out-of-catalog test / successful raw responses | Detects invention before validation removes it. A real catalog test outside the shortlist is not a catalog hallucination. Model errors are reported separately. |
| Confidence calibration | Compare stated confidence with observed decision-and-selection accuracy | Shows whether certainty is trustworthy. Inspect reliability bins, high-confidence accuracy, ECE and Brier score. |

For example, if gold requires A, B and C over three notes, and the agent selects A, D and nothing, TP=1, FP=1 and FN=2. Recall is 1/3=33.3%; precision is 1/2=50%. If two other notes have no valid test and the agent returns no-match on both, set exact-match rate is 3/5=60%. Those easy empty-set cases improve exact agreement but do not fix the missed tests.

Confidence bins group responses by 0-10%, 10-20%, and so on (100% belongs to the final bin). For each bin, compare mean confidence with actual correctness. **Expected calibration error (ECE)** is `sum(bin_count / N * abs(mean_confidence - accuracy))`. **Brier score** is `mean((confidence_as_fraction - correct_as_0_or_1)^2)`. Lower is better for both. Two answers with 80% confidence, one right and one wrong, yield accuracy 50%, ECE 0.30 and Brier `(0.2² + 0.8²)/2 = 0.34`.

Raw model confidence is evaluated against raw decision/selection correctness. The agent's displayed confidence is a blend of model confidence, lexical relevance, first-rank agreement and evidence grounding. It is separately evaluated against the final guarded output and labeled a **heuristic**, not a calibrated probability. Model failures and invalid confidence values are excluded from calibration; sample counts are shown. A bin with one or two examples provides weak evidence, even if its accuracy is 100%.

Undefined ratios are `null` in JSON and N/A in the report, never silently 0% or 100%. Model errors fail exact/decision accuracy and generate an FN on a positive case. Hallucination and confidence metrics use successful responses only, so always inspect the separate model-error count.

## Limits of this gold set

This is an AI-authored, source-grounded **candidate** gold set, not an SME-approved benchmark. The 2025 mapping set has 63 notes but only seven positive matches, 45 catalog gaps and 11 unresolved mappings. Overall accuracy alone is easy to inflate by abstaining on everything. Prioritize recall, inspect every false negative and obtain tenant/test-script SME validation before treating these labels as production truth. No business-severity weights were fabricated. The runner does not use model confidence as a substitute for observed correctness.

The `--split all` baseline mixes development and held-out cases and is for diagnosis. Use `--split test` for final comparisons after prompt tuning. Do not tune prompts by reading held-out predictions, then call the same cases an unseen test.

## Fully local Gemma and later AWS

For disconnected inference, install Ollama and pull a Gemma model while internet is available:

```powershell
ollama pull gemma3:4b
```

Set `.env` to `LLM_PROVIDER=ollama`, `OLLAMA_MODEL=gemma3:4b` and `OLLAMA_BASE_URL=http://localhost:11434`, then restart the backend. The app and evaluation runner use the local `/api/chat` endpoint. A model download/server is required; having a Google API key alone does not install a local model. See [Ollama API documentation](https://docs.ollama.com/api) and [Google text-generation documentation](https://ai.google.dev/gemini-api/docs/text-generation).

For AWS, the SAM template explicitly sets `LLM_PROVIDER=bedrock`, avoiding accidental reliance on your laptop `.env` or Google key. Configure `BEDROCK_MODEL_ID` and AWS IAM access as described in README. Provider routing is tested; no AWS deployment or live Bedrock call was performed in this task.

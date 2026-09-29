# Workday Release Regression Agent

An explainable regression-test triage tool for Workday releases. Upload a release-notes workbook and an E2E test catalog; the app ranks likely candidates, asks Amazon Bedrock to make a grounded selection, calculates confidence signals, and flags uncertain matches for human review.

![Workflow](https://img.shields.io/badge/workflow-upload%20%E2%86%92%20shortlist%20%E2%86%92%20review-1f6feb)
![Backend](https://img.shields.io/badge/backend-FastAPI-009688)
![Frontend](https://img.shields.io/badge/frontend-Next.js-black)

## What it does

- Validates both `.xlsx` uploads and reports their sheet/row counts before analysis.
- Reads the first non-empty worksheet and maps release-note fields to the E2E catalog.
- Uses deterministic lexical, fuzzy-title, product-area, and work-stream signals to shortlist candidates.
- Uses Amazon Bedrock only to choose from the shortlist; it cannot invent an E2E catalog value.
- Returns evidence, reasoning, confidence bands, evaluation signals, and an explicit review flag for every release row.
- Exports the result table as CSV. Uploaded files and results are kept in memory for the current session only.

## Architecture

```text
Next.js UI (localhost:3000)
        │ multipart upload / JSON results
        ▼
FastAPI API (localhost:8000)
        │ parse + shortlist + grounded Bedrock review
        ▼
Release workbook + E2E catalog workbook
```

## Quick start

### 1. Backend

```powershell
cd backend
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cd ..
Copy-Item .env.example .env
# Edit .env if you need to override the Bedrock region/model. Keep this file private.
cd backend
uvicorn app:app --reload --port 8000
```

### 2. Frontend

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The frontend defaults to `http://localhost:8000`; set `NEXT_PUBLIC_API_BASE` if the API runs elsewhere.

## Workbook schemas

The release workbook must include: `Functional Area (Domain)`, `Product Line`, `Product Area`, `Using Workday`, `Title`, `Description`, `Business Benefits`, `Changes`, `Impact`, `Release Number`, and `Release Note URL`.

The E2E catalog workbook must include: `Item`, `Work-Stream`, and `E2E`.

Column matching is case- and punctuation-insensitive. The repository includes `WhatsNew2026R2.xlsx` and `e2e list.xlsx` as local sample inputs.

## API endpoints

- `GET /api/health` — service status and active model configuration.
- `POST /api/preview` — validate both workbooks and return metadata.
- `POST /api/analyze` — analyze every release-note row. Send both files as multipart fields named `release_file` and `e2e_file`.

## Configuration

Copy `.env.example` to `.env` and configure:

| Variable | Purpose |
| --- | --- |
| `AWS_REGION` | AWS region used for Bedrock, defaulting to `ap-south-1`. |
| `BEDROCK_MODEL_ID` | Bedrock model or inference-profile ID, defaulting to Amazon Nova Lite. |
| `REVIEW_THRESHOLD` | Minimum confidence before a match can be marked ready. |
| `FRONTEND_ORIGIN` | Allowed browser origin for CORS. |

The deployed Lambda uses its IAM execution role to call Bedrock; no model API key is stored in the application. If Bedrock is unavailable, the app still parses and shortlists files but returns a safe, review-required fallback.

## Quality checks

```powershell
cd backend
pytest
cd ..\frontend
npm run build
```

Continuous integration runs both checks on pushes and pull requests. See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Security notes

Do not commit `.env`, API keys, customer workbooks, or other sensitive release data. The checked-in `.env.example` contains placeholders only. Rotate any credential that may previously have been exposed.

## License

MIT — see [LICENSE](LICENSE).

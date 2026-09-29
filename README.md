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
| `AWS_REGION` | AWS region used for Bedrock, defaulting to `ap-southeast-2`. |
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

## AWS deployment with Amazon Bedrock

The deployment target is `ap-southeast-2` (Sydney), matching the AWS console URL. The backend is prepared for AWS Lambda behind an API Gateway HTTP API. Lambda calls Amazon Bedrock using its IAM role, so no Bedrock API key is stored in the application.

### Prerequisites

- AWS account with Amazon Bedrock model access enabled for Amazon Nova Lite in Sydney.
- AWS CLI v2 and AWS SAM CLI installed and authenticated with an IAM identity.
- An AWS Budgets zero-spend alert configured before deploying.

Check the tools:

```powershell
aws --version
sam --version
aws sts get-caller-identity
```

### Deploy the backend

Run from the repository root:

```powershell
$env:AWS_DEFAULT_REGION = "ap-southeast-2"
sam build --template-file infra/template.yaml
sam deploy --guided --template-file .aws-sam\build\template.yaml
```

During the guided deployment, use a stack name such as `workday-regression-agent`, keep the region as `ap-southeast-2`, and use `http://localhost:3000` as the initial `FrontendOrigin`. Save the generated API URL from the stack Outputs.

### Deploy the frontend

The Next.js app is configured as a static export. Create an S3 bucket in `ap-southeast-2`, then build with the API URL produced by the backend stack:

```powershell
cd frontend
npm ci
$env:NEXT_PUBLIC_API_BASE = "https://YOUR_API_ID.execute-api.ap-southeast-2.amazonaws.com"
npm run build
aws s3 sync .\out s3://YOUR_BUCKET_NAME --delete
```

For a public HTTPS website, put the bucket behind an Amazon CloudFront distribution. For a learning deployment, use the CloudFront-provided domain and then redeploy the backend with that domain as `FrontendOrigin`:

```powershell
sam deploy --template-file .aws-sam\build\template.yaml --stack-name workday-regression-agent --capabilities CAPABILITY_IAM --parameter-overrides FrontendOrigin=https://YOUR_CLOUDFRONT_DOMAIN
```

The first deployment is intentionally small: no database, VPC, NAT Gateway, EC2 instance, or always-on container. Delete the CloudFormation stack and S3 bucket when you finish practicing so resources do not remain active.

### Bedrock configuration

The default model is `amazon.nova-lite-v1:0`. Bedrock model availability and inference-profile IDs can change, so confirm the exact model shown in the Bedrock console for `ap-southeast-2` before deployment. The Lambda role needs `bedrock:InvokeModel`; the SAM template grants the minimum Bedrock actions needed by the Converse call, with the resource left broad for the first learning deployment.

## Security notes

Do not commit `.env`, API keys, customer workbooks, or other sensitive release data. The checked-in `.env.example` contains placeholders only. Rotate any credential that may previously have been exposed.

## License

MIT — see [LICENSE](LICENSE).

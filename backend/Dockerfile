FROM python:3.12-slim
WORKDIR /workspace
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend backend
COPY prompts prompts
COPY outputs/workday_2025_eval/workday_2025_golden_eval.json outputs/workday_2025_eval/workday_2025_golden_eval.json
WORKDIR /workspace/backend
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

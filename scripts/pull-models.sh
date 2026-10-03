#!/bin/sh
# Download weights only. This script never runs inference or an evaluation.
set -eu
attempt=0
until ollama list >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "Ollama did not become ready" >&2
    exit 1
  fi
  sleep 2
done
pull_model() {
  if ollama show "$1" >/dev/null 2>&1; then
    echo "Model weights already installed: $1"
    return 0
  fi
  retries=0
  until ollama pull "$1"; do
    retries=$((retries + 1))
    if [ "$retries" -ge 3 ]; then
      echo "Weight download failed after three attempts: $1" >&2
      return 1
    fi
    echo "Retrying weight download: $1" >&2
    sleep 5
  done
}
pull_model qwen3:0.6b
pull_model qwen2.5:0.5b
echo "Local fallback model weights are ready; no inference was run."

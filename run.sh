#!/usr/bin/env bash
# Launch the Sheet → Dashboard app. Open http://localhost:8077 afterwards.
cd "$(dirname "$0")" || exit 1
echo "Sheet → Dashboard  ·  http://localhost:8077"
echo "(needs Ollama running with qwen2.5:14b — falls back to smaller models automatically)"
exec python3 server.py

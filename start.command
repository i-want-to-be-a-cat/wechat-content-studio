#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -q -r backend/requirements.txt
fi
exec .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 4173

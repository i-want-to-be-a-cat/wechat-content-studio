#!/bin/bash
set -e
cd "$(dirname "$0")"
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 4173 --reload

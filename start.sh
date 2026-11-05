#!/bin/sh
set -eu

: "${github_repo:?Missing required env var: github_repo}"
: "${github_token:?Missing required env var: github_token}"

mkdir -p /workspace/app/tmp
mkdir -p /workspace/app/.local/pregrade

cd /workspace/app

python github_folder_sync.py ./.local/pregrade "$github_repo" --token "$github_token" &

exec uvicorn app:app --host 0.0.0.0 --port 5000

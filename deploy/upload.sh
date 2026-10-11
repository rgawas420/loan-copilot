#!/usr/bin/env bash
# Package the project and copy it to the server, keeping the server's .env, kb/, storage/ and policies/.
# Usage (Git Bash on Windows, or any Linux/macOS shell):  ./deploy/upload.sh root@YOUR_DROPLET_IP
# (Windows' own tar.exe crashes on this folder; Git Bash's GNU tar does not.)
set -euo pipefail
SERVER="${1:?Usage: ./deploy/upload.sh root@YOUR_DROPLET_IP}"
cd "$(dirname "$0")/.."

# Release gate: the answer configuration being shipped must do at least as well as the approved baseline on the
# golden tests (see evals/gate.py). SKIP_GATE=1 ships anyway (e.g. a change that does not touch answers).
if [ "${SKIP_GATE:-0}" != "1" ]; then
    PY="${PYTHON:-$( [ -x venv/Scripts/python.exe ] && echo venv/Scripts/python.exe || echo venv/bin/python )}"
    if ! "$PY" -m evals.gate; then
        echo "Not uploading: the release gate did not pass. Fix the regressions (Admin > RAG quality > Golden tests),"
        echo "or set SKIP_GATE=1 if this upload does not change how answers are produced."
        exit 1
    fi
fi

ARCHIVE="$(mktemp -u)-urban-money.tgz"
tar --exclude=./venv --exclude=node_modules --exclude=./frontend/dist --exclude=./var --exclude=./uploads \
    --exclude=./mobile --exclude=__pycache__ --exclude=.pytest_cache --exclude=./.env --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    -czf "$ARCHIVE" .
echo "Packaged $(du -h "$ARCHIVE" | cut -f1); uploading..."

scp "$ARCHIVE" "$SERVER:/tmp/urban-money-app.tgz"
ssh "$SERVER" "mkdir -p /opt/urban-money && tar -xzf /tmp/urban-money-app.tgz -C /opt/urban-money && rm /tmp/urban-money-app.tgz && echo Uploaded to /opt/urban-money"
rm -f "$ARCHIVE"

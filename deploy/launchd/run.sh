#!/bin/bash
# launchd wrapper: load .env, then exec a pmev module under the repo venv.
# launchd has no EnvironmentFile equivalent (systemd's), so the units call this
# instead of python directly. Use when adding a new launchd service.
#
# Usage: run.sh pmev.pollers.pinnacle
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

exec "$REPO/.venv/bin/python" -m "$1"

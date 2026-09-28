#!/usr/bin/env bash
# Start the deployer on the installer node.
set -euo pipefail
cd "$(dirname "$0")"

HOST="${DEPLOYER_HOST:-127.0.0.1}"
PORT="${DEPLOYER_PORT:-8800}"

if [[ ! -d .venv ]]; then
  echo "==> Creating virtualenv"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

command -v oc >/dev/null || { echo "ERROR: 'oc' not found in PATH"; exit 1; }

if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" ]]; then
  echo
  echo "  !! Binding to $HOST. This app accepts cluster-admin credentials."
  echo "  !! Put it behind a firewall, or use an SSH tunnel instead:"
  echo "  !!   ssh -L 8800:127.0.0.1:8800 $(whoami)@\$(hostname)"
  echo
fi

echo "==> http://${HOST}:${PORT}"
exec ./.venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" --no-access-log

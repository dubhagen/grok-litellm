#!/bin/sh
# Start the local LiteLLM gateway and the Grok development adapter.
set -eu
cd "$(dirname "$0")"
export PATH="$(pwd)/.venv/bin:${PATH}"
if [ ! -f .env ]; then
  echo "missing .env" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a
mkdir -p var media
./ensure-postgres.sh
set -a
# shellcheck disable=SC1091
. ./.env
set +a
if [ ! -x .venv/bin/python ] || [ ! -x .venv/bin/litellm ]; then
  echo "missing .venv; install requirements.txt into .venv first" >&2
  exit 1
fi
.venv/bin/python patch_prisma.py

stop_pid() {
  file="$1"
  if [ -f "$file" ]; then
    pid="$(cat "$file")"
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
    rm -f "$file"
  fi
}

stop_pid var/adapter.pid
stop_pid var/litellm.pid

PYTHONPATH=. .venv/bin/python -m adapter.server >> var/adapter.log 2>&1 &
echo $! > var/adapter.pid

.venv/bin/litellm --config config.yaml --host 127.0.0.1 --port "${LITELLM_PORT:-4000}" >> var/litellm.log 2>&1 &
echo $! > var/litellm.pid

echo "adapter pid $(cat var/adapter.pid) on ${ADAPTER_PORT:-4010}"
echo "litellm pid $(cat var/litellm.pid) on ${LITELLM_PORT:-4000}"

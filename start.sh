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

refuse_placeholder() {
  name="$1"
  value="$2"
  placeholder="$3"
  if [ -z "$value" ] || [ "$value" = "$placeholder" ]; then
    echo "Replace ${name} in .env. The example value is public and will not start." >&2
    exit 1
  fi
}

refuse_placeholder LITELLM_MASTER_KEY "${LITELLM_MASTER_KEY:-}" sk-local-change-me
refuse_placeholder ADAPTER_API_KEY "${ADAPTER_API_KEY:-}" sk-adapter-change-me

mkdir -p var media
./ensure-postgres.sh
set -a
# shellcheck disable=SC1091
. ./.env
set +a
# LiteLLM calls the adapter here. Image URLs use ADAPTER_PUBLIC_BASE when set.
ADAPTER_HOST="${ADAPTER_HOST:-127.0.0.1}"
ADAPTER_PORT="${ADAPTER_PORT:-4010}"
export ADAPTER_HOST ADAPTER_PORT
export ADAPTER_API_BASE="http://${ADAPTER_HOST}:${ADAPTER_PORT}/v1"
if [ -z "${ADAPTER_PUBLIC_BASE:-}" ]; then
  export ADAPTER_PUBLIC_BASE="http://${ADAPTER_HOST}:${ADAPTER_PORT}"
fi
if [ ! -x .venv/bin/python ] || [ ! -x .venv/bin/litellm ]; then
  echo "missing .venv; install requirements.txt into .venv first" >&2
  exit 1
fi
.venv/bin/python patch_prisma.py
schema="$(.venv/bin/python -c 'import pathlib, litellm; print(pathlib.Path(litellm.__file__).resolve().parent / "proxy" / "schema.prisma")')"
if [ ! -f "$schema" ]; then
  echo "LiteLLM schema not found at ${schema}" >&2
  exit 1
fi
.venv/bin/python -m prisma generate --schema "$schema"

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

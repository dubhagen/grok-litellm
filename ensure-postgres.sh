#!/bin/sh
# Start the project-local Postgres that the LiteLLM admin UI requires.
# Listens on 127.0.0.1 only. Data and binaries stay under var/.
set -eu
cd "$(dirname "$0")"

PG_PREFIX="$(pwd)/var/pgsql"
PG_DATA="$(pwd)/var/pgdata"
PG_PORT="${POSTGRES_PORT:-55432}"
ARCHIVE_URL="https://github.com/theseus-rs/postgresql-binaries/releases/download/17.5.1/postgresql-17.5.1-x86_64-unknown-linux-gnu.tar.gz"

mkdir -p var

# Create DATABASE_URL once. Prints only port, user, and database name.
eval "$(
  python3 - <<'PY'
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

root = Path(".").resolve()
env_path = root / ".env"
text = env_path.read_text() if env_path.exists() else ""
port = os.environ.get("POSTGRES_PORT", "55432")
user = "litellm"
database = "litellm"
password = None
for line in text.splitlines():
    if line.startswith("DATABASE_URL="):
        parsed = urlparse(line.split("=", 1)[1].strip().strip('"').strip("'"))
        if parsed.hostname:
            port = str(parsed.port or port)
        if parsed.username:
            user = parsed.username
        if parsed.password:
            password = parsed.password
        if parsed.path and parsed.path != "/":
            database = parsed.path.lstrip("/")
        break
else:
    password = secrets.token_hex(24)
    with env_path.open("a") as handle:
        handle.write(
            f"\nPOSTGRES_PORT={port}\n"
            f"DATABASE_URL=postgresql://{user}:{password}@127.0.0.1:{port}/{database}\n"
            "LITELLM_PRISMA_COMMAND_TIMEOUT=300\n"
            "LITELLM_PRISMA_MIGRATE_DEPLOY_TIMEOUT=600\n"
        )
    os.chmod(env_path, 0o600)
if not password:
    raise SystemExit("DATABASE_URL is set but has no password")
pw_path = root / "var" / "pg.pw"
pw_path.write_text(password + "\n")
os.chmod(pw_path, 0o600)
print(f"PG_PORT={port}")
print(f"PG_USER={user}")
print(f"PG_DB={database}")
PY
)"

if [ ! -x "${PG_PREFIX}/bin/postgres" ]; then
  archive="$(mktemp)"
  curl -fL --retry 3 --retry-delay 2 -o "$archive" "$ARCHIVE_URL"
  unpack="$(mktemp -d)"
  tar -xzf "$archive" -C "$unpack"
  rm -f "$archive"
  found="$(find "$unpack" -type f -path '*/bin/postgres' | head -n 1)"
  if [ -z "$found" ]; then
    echo "postgres binary missing from archive" >&2
    exit 1
  fi
  rm -rf "$PG_PREFIX"
  mv "$(dirname "$(dirname "$found")")" "$PG_PREFIX"
  rm -rf "$unpack"
fi

if [ ! -f "${PG_DATA}/PG_VERSION" ]; then
  "${PG_PREFIX}/bin/initdb" \
    -D "$PG_DATA" \
    -U "$PG_USER" \
    --pwfile="$(pwd)/var/pg.pw" \
    --auth-local=trust \
    --auth-host=scram-sha-256 \
    --no-locale \
    --encoding=UTF8 \
    >/dev/null
fi

if ! "${PG_PREFIX}/bin/pg_isready" -h 127.0.0.1 -p "$PG_PORT" >/dev/null 2>&1; then
  "${PG_PREFIX}/bin/pg_ctl" -D "$PG_DATA" -l "$(pwd)/var/postgres.log" -w start \
    -o "-c listen_addresses=127.0.0.1 -c port=${PG_PORT}"
fi

export PGPASSWORD
PGPASSWORD="$(cat var/pg.pw)"
if ! "${PG_PREFIX}/bin/psql" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='${PG_DB}'" | grep -q 1; then
  "${PG_PREFIX}/bin/createdb" -h 127.0.0.1 -p "$PG_PORT" -U "$PG_USER" "$PG_DB"
fi
unset PGPASSWORD

echo "postgres ready on 127.0.0.1:${PG_PORT}"

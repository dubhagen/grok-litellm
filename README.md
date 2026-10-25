# grok-litellm

Local LiteLLM gateway for developing against a Grok subscription. Chat and responses go to the Grok CLI subscription proxy. Images are produced by a headless `grok` process. Speech and transcription are development stubs.

This is a localhost development proxy. It is not a production gateway, and it is not an official xAI or LiteLLM product.

## What it runs

`start.sh` starts two processes on loopback:

| Process | Default address | Role |
| --- | --- | --- |
| `adapter.server` | `ADAPTER_HOST`:`ADAPTER_PORT` (`127.0.0.1:4010`) | OpenAI-compatible backend. Forwards chat and responses. Shells out to `grok` for images. |
| `litellm` | `127.0.0.1:4000` | Proxy your client talks to. `config.yaml` sends it to `http://$ADAPTER_HOST:$ADAPTER_PORT/v1`. |

Postgres lives under `var/` and listens on `127.0.0.1:55432` only. LiteLLM's admin UI needs it. `ensure-postgres.sh` downloads the server binaries on first run and writes a password into `.env`. `start.sh` then generates the Prisma client from the installed LiteLLM schema.

## Models

| Name | Mode | Backend |
| --- | --- | --- |
| `grok-4.7` | chat | Grok subscription proxy |
| `grok-4.7-build-fast` | chat | Grok subscription proxy |
| `grok-4.6` | chat | Grok subscription proxy |
| `grok-4.5` | chat | Grok subscription proxy |
| `grok-image` | image generation | headless `grok` image tool |
| `grok-tts` | speech | short beep stub |
| `grok-asr` | transcription | fixed text stub |

Chat models advertise vision support so clients can send images. Token limits in `config.yaml` are local declarations for the proxy, not a claim about upstream capacity.

## Requirements

- Python 3.12
- A logged-in [Grok CLI](https://x.ai) (`grok login`). The adapter reads `~/.grok/auth.json` and does not store that token.
- `ffmpeg`, used once to create the speech stub
- `curl`, used once to fetch the project-local Postgres binaries
- Outbound HTTPS to `cli-chat-proxy.grok.com` and, on first run, to the Postgres binary release and the Prisma engine download

## Setup

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Replace LITELLM_MASTER_KEY and ADAPTER_API_KEY before starting.
chmod 600 .env
./start.sh
```

Generate both keys yourself. `start.sh` exits while either one is still the public placeholder from `.env.example`.

`start.sh` is idempotent for the database. It stops a previous adapter and LiteLLM pid if those files exist, then starts both again. Logs go to `var/adapter.log` and `var/litellm.log`.

The admin UI is `http://127.0.0.1:4000/ui`. The username is `admin` unless `UI_USERNAME` is set. The password is `LITELLM_MASTER_KEY` unless `UI_PASSWORD` is set. The API bearer token is `LITELLM_MASTER_KEY`. Clients call `http://127.0.0.1:${LITELLM_PORT:-4000}/v1`.

`ADAPTER_HOST` and `ADAPTER_PORT` are where the adapter listens and where LiteLLM sends requests. Set `ADAPTER_PUBLIC_BASE` only when image URLs must use a different address than that bind address. Keep the host on loopback.

`patch_prisma.py` rewrites one assignment in the installed LiteLLM package so Prisma Client Python 0.13+ does not drop the database connection. Reinstalling LiteLLM restores the upstream file, and the next `start.sh` patches it again. The patch lives inside `.venv`, which is not part of this repo.

## Check

With both processes running:

```sh
.venv/bin/python verify.py --skip-images
```

Drop `--skip-images` to also exercise image generation and edit. That path calls `grok` and can take a few minutes. `verify.py` reads `.env` and does not print secrets.

Point a client at `http://127.0.0.1:4000/v1` and use `LITELLM_MASTER_KEY` as the bearer token. The same value is the admin UI password.

## Layout

```
adapter/            OpenAI-compatible development backend
config.yaml         LiteLLM model list and proxy settings
ensure-postgres.sh  Project-local Postgres, loopback only
patch_prisma.py     Prisma engine-hook workaround
start.sh            Start Postgres, adapter, and LiteLLM
verify.py           Smoke checks against the local proxy
```

`var/` holds Postgres data, binaries, logs, pids, and image-job scratch. `media/` holds generated images the adapter serves. Both are gitignored.

## Security

- Bind addresses are loopback. Do not expose this proxy past localhost.
- `.env` holds the LiteLLM master key, the adapter key, and the Postgres URL. It is gitignored.
- The Grok session token stays in `~/.grok/auth.json`. It is read at request time and is never written into this tree.
- Speech and transcription responses are stubs. Do not treat them as model output.

## License

[MIT](LICENSE)

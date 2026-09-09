# Open Executive

[![CI](https://github.com/SenteLabsAI/OpenExecutive/actions/workflows/ci.yml/badge.svg)](https://github.com/SenteLabsAI/OpenExecutive/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Next.js 15](https://img.shields.io/badge/Next.js-15-black.svg)](https://nextjs.org/)

An AI system that acts as your company's virtual executive team — a senior advisor with Harvard MBA-level knowledge, customized for your specific business.

## Demo

[![Open Executive demo video](https://img.youtube.com/vi/O_g97xxVTMk/maxresdefault.jpg)](https://youtu.be/O_g97xxVTMk)

A walkthrough of Open Executive in action — [watch on YouTube](https://youtu.be/O_g97xxVTMk).

## What It Does

Developed by [sentelabs.ai](https://sentelabs.ai) Open Executive provides a single coherent executive voice backed by eight specialist AI agents:

- **Chief Strategy Officer** — competitive analysis, M&A, market positioning, OKRs
- **Chief Financial Officer** — financial modeling, fundraising, unit economics, cash flow
- **Chief HR/People Officer** — hiring, compensation, performance, culture
- **General Counsel** — contracts, IP, employment law basics, compliance
- **Chief Operating Officer** — process design, vendor management, operational scaling
- **Chief Marketing Officer** — GTM strategy, brand, communications, PR
- **Chief Product Officer** — roadmap, prioritization, product strategy
- **Board Communications Director** — board decks, investor relations, governance

All responses come from one consistent executive voice. The internal agent architecture is never exposed to the user. Beyond Q&A, the system maintains episodic memory of past decisions and initiatives across sessions, and a built-in scheduler can proactively surface follow-ups and time-sensitive actions.

## Architecture

```
User message
    ↓
Executive Orchestrator (claude-sonnet-4-6)
    ↓ tool use → parallel specialist calls
CSO / CFO / CHRO / GC / COO / CMO / CPO / Board
    ↓ each specialist retrieves relevant context from ChromaDB
Built-in MBA knowledge + Your company documents
    ↓
Synthesized executive response
```

**Knowledge** — Two retrieval layers per specialist call: (1) built-in MBA-level Markdown (`knowledge/builtin/`, git-tracked) seeded into ChromaDB at startup, and (2) your uploaded company documents chunked and stored in a separate `company_docs` collection. RAG context is injected into the user turn, never the cached system prompt.

**Episodic memory** — After every response, a background `claude-haiku-4-5` pass extracts key decisions, initiatives, and advice into SQLite. The next session opens with a `<past_decisions>` block so the Executive remembers what it recommended last month.

**Scheduler** — A built-in job runner claims due actions via `UPDATE … RETURNING` to prevent double-firing. The API must run as a single instance; do not horizontally scale it without gating the scheduler first.

**Prompt caching** — The system prompt is structured so the Executive persona, company profile, and knowledge index are cached separately (up to 85% cache hit rate after the first few turns). No dynamic content ever goes in a cached block.

See [docs/architecture.md](docs/architecture.md) for the full design.

## Tech Stack

| Layer | Choice |
|---|---|
| LLM backbone | Anthropic Claude API |
| Default model | `claude-sonnet-4-6` (Executive + most specialists) |
| Deep reasoning | `claude-opus-4-7` (CSO, CFO, GC, Board — with extended thinking) |
| Backend | Python 3.11 + FastAPI |
| Package manager | `uv` |
| Vector store | ChromaDB (local, embedded) |
| Episodic memory | SQLite |
| Web UI | Next.js 15 (App Router) + Tailwind |
| License | Apache 2.0 |

## Repo Layout

```
openexecutive/
├── packages/
│   ├── core/
│   │   └── openexecutive/
│   │       ├── orchestrator/     # Executive persona + routing loop
│   │       ├── agents/           # 8 specialist agents
│   │       ├── knowledge/        # ChromaDB store + RAG pipeline
│   │       ├── memory/           # Company profile + episodic memory
│   │       ├── onboarding/       # Wizard state machine + profile builder
│   │       ├── prompts/          # Persona + domain prompts + cache manager
│   │       ├── api/              # FastAPI app + routes
│   │       ├── integrations/     # Slack, Email, Telegram, Google Chat, Discord
│   │       ├── scheduler/        # Background job runner (single-instance)
│   │       ├── alerts/           # Proactive alert system
│   │       ├── audit/            # Audit logging
│   │       ├── architecture/     # Internal architecture utilities
│   │       ├── workflows/        # Multi-step workflow definitions
│   │       └── cli.py            # Click CLI
│   └── ui/                       # Next.js 15 web UI
├── evals/                        # Eval scenarios + LLM-as-judge runner
├── fixtures/                     # Demo company fixtures (profiles, docs, rosters)
├── scripts/                      # Operator scripts (Fly secrets, Google auth)
├── docker/                       # Dockerfile(s) + docker-compose.yml
├── fly.api.toml / fly.ui.toml    # Fly.io configs — dev API + UI apps
├── fly.api.qa.toml / fly.ui.qa.toml  # Fly.io configs — QA API + UI apps
├── fly.honcho.toml               # Fly.io config — Honcho memory app (optional)
└── docs/                         # Architecture + deployment docs
```

## Quick Start

```bash
# Clone the repo
git clone https://github.com/SenteLabsAI/OpenExecutive.git
cd OpenExecutive

# Set your Anthropic API key
cp .env.example .env
# Edit .env and add ANTHROPIC_API_KEY=sk-ant-...

# Start everything
make dev
```

Open http://localhost:3000 to start chatting with your executive. The API runs on port 8000 and the UI on 3000.

> **The UI needs the repo-root `.env` linked in.** Next.js only reads `.env`
> files from its own project root, so `packages/ui` never sees the root `.env`
> where `AUTH_SECRET`, `AUTH_GOOGLE_ID`, `AUTH_GOOGLE_SECRET`, `ALLOWED_EMAILS`
> and `BACKEND_BASE_URL` live. `make dev` and `make install` link it for you
> (`packages/ui/.env.local` → `../../.env`, gitignored, never clobbering an
> existing file); `make link-env` does it on its own.
>
> Without that link the failure is quiet: the app redirects to `/signin` and the
> page renders normally, but signing in fails and `/api/auth/providers` returns
> `There was a problem with the server configuration` — NextAuth has neither a
> secret nor Google credentials.

> **Signing in for the first time.** The UI gates every page behind Google
> sign-in, so a fresh checkout needs three values in the repo-root `.env`:
>
> ```bash
> AUTH_SECRET=$(openssl rand -base64 32)   # NextAuth refuses to start without it
> AUTH_GOOGLE_ID=...                       # Google Cloud -> APIs & Services ->
> AUTH_GOOGLE_SECRET=...                   #   Credentials -> OAuth client ID (Web)
> ALLOWED_EMAILS=you@example.com           # replace the alice/bob placeholders
> ```
>
> In the Google OAuth client, add the exact callback as an **Authorized redirect
> URI** — `http://localhost:3000/api/auth/callback/google` (path and port must
> match, and Google accepts `http` only for `localhost`). `ALLOWED_EMAILS` is
> the bootstrap allowlist: it applies only until the People table has an email
> row, after which the roster is authoritative.

> **`Bus error` on `next dev` / `next build`?** The native `@next/swc` binary
> Next ships is incompatible with some newer glibc versions (Ubuntu 25.10 /
> glibc 2.42). SIGBUS kills the process, so you get `Bus error` and nothing
> else — no stack, no app error. Check with `ldd --version`. Two ways round it:
>
> ```bash
> # A. run the UI on the WASM SWC build (slower compiles, stays on the host)
> cd packages/ui && npm install --no-save @next/swc-wasm-nodejs
> cd ../.. && make dev-wasm
>
> # B. run in Docker, which uses node:22-alpine (musl, unaffected)
> make docker
> ```
>
> `make dev-wasm` exists because the recipe is not guessable: Turbopack needs
> native bindings, so WASM implies `--webpack`; and deleting the native binary
> is not enough on its own, since Next re-downloads it into `~/.cache/next-swc`
> unless `NEXT_TEST_WASM=1` forces the WASM loader.

> **Reaching a dev server from another machine?** Set `ALLOWED_DEV_ORIGINS` to
> the host you browse to:
>
> ```bash
> ALLOWED_DEV_ORIGINS=192.168.1.50
> ```
>
> `next dev` blocks cross-origin requests to its `/_next/*` resources by
> default. Without this the HTML loads but the client bundle does not, so the
> page never hydrates and freezes on whatever the server rendered — a component
> showing "Loading…" stays there, with only a console warning to explain it.
> Dev-only; `next start` serves no dev resources and ignores it.
>
> For a machine you leave running, prefer production mode instead — it skips
> dev-mode entirely and compiles once rather than per request:
>
> ```bash
> cd packages/ui && NEXT_TEST_WASM=1 npm run build -- --webpack   # drop NEXT_TEST_WASM if native SWC works
> BACKEND_BASE_URL=http://localhost:8000 npx next start --port 3000
> ```

> **Ports already in use?** Override either:
>
> ```bash
> make dev API_PORT=8001            # API on 8001, UI still on 3000
> make dev API_PORT=8001 UI_PORT=3001
> ```
>
> `make dev` points the UI's `BACKEND_BASE_URL` at `API_PORT` for you — the
> browser talks to the Next server, which proxies `/api/backend` to the API,
> so both sides have to agree. `make stop` takes the same variables.
>
> If a stale server is holding a port, `make stop` frees both. It backgrounds
> uvicorn with `&`, so an API started by an earlier `make dev` outlives a
> failed UI start and even a closed terminal.

> **Reaching it from another device.** `next dev` already binds every
> interface, so the UI is on your LAN at `http://<your-ip>:3000` with no
> change — and its server-side proxy reaches the API over loopback, so the API
> does not need exposing. What blocks a second device is sign-in: every page is
> gated by Google OAuth, and Google will not accept a private IP as a redirect
> URI. An SSH tunnel from the other machine keeps you on `localhost`, which
> Google does allow:
>
> ```bash
> ssh -N -L 3000:localhost:3000 <user>@<host-ip>   # then browse localhost:3000
> ```
>
> For a durable setup, put the UI on a hostname with real HTTPS (Tailscale
> Funnel, cloudflared, ngrok), set `AUTH_URL` to that origin with
> `AUTH_TRUST_HOST=true`, and register `<origin>/api/auth/callback/google` in
> the Google OAuth client.
>
> `API_HOST=0.0.0.0` exposes the API itself, for when something other than the
> UI must call it directly. The API has **no authentication** unless
> `BACKEND_SHARED_SECRET` is set, so set that first — otherwise anyone on the
> network can read your company data and spend your model quota.

> **First run:** requires Python 3.11+ and Node 22+. The initial `uv sync` pulls heavy
> ML dependencies (ChromaDB + sentence-transformers/PyTorch), and the first boot
> downloads a small embedding model (~90 MB) to build the local vector index — so the
> first `make dev` takes a few minutes before the app is ready. Subsequent starts are fast.

**For contributors not using `make`:**

```bash
cd packages/core
uv sync
source .venv/bin/activate
uvicorn openexecutive.api.main:app --reload --port 8000

# In a second terminal
cd packages/ui && npm install && npm run dev
```

## Run the Discord Bot

1. Create a Discord application at https://discord.com/developers/applications
2. Enable the **Message Content** privileged intent (Bot → Privileged Gateway Intents)
3. Invite the bot with `bot` + `applications.commands` scopes
4. Set env vars in `.env`: `DISCORD_BOT_TOKEN`, `DISCORD_APP_ID`, `DISCORD_GUILD_IDS`
5. Run the API normally — the bot starts as part of the FastAPI lifespan when `DISCORD_BOT_TOKEN` is set:

```bash
make dev
```

The bot is embedded in the API process (alongside the email poller, scheduler, and resumer) so it shares the same SQLite database and ChromaDB vector store under `/data` in production. Skip the token to disable.

For iterating on bot-only code without restarting the API, `make discord` runs the bot as a standalone process against the same local DB.

Users can DM the bot, `@mention` it in a channel (replies in a thread), or use `/ask` and `/today` slash commands. Slash commands sync to `DISCORD_GUILD_IDS` instantly on startup; leave blank for global registration (up to 1-hour propagation delay).

### Deploying to production

Just set the secrets on the existing API app — no new Fly app required:

```bash
flyctl secrets set -a openexec-api-dev \
  DISCORD_BOT_TOKEN=... \
  DISCORD_APP_ID=... \
  DISCORD_GUILD_IDS=...
```

Discord user access is managed via the /people UI — add a Person row with `discord_user_id` set.

The machine restarts and the bot starts on the next lifespan boot. To disable in prod: `flyctl secrets unset -a openexec-api-dev DISCORD_BOT_TOKEN`.

## Onboarding Your Company

The first time you visit the app, you'll be guided through a wizard to set up your company profile:
- Company basics (name, industry, stage, team size)
- Business model and revenue
- Competitive landscape
- Strategic priorities
- Culture and values
- Optional: financial position, document upload

After onboarding, the Executive will reference your specific company context in every response.

## Interfaces

| Interface | How to Use |
|-----------|-----------|
| **Web UI** | `http://localhost:3000` |
| **Slack** | Mention `@OpenExecutive` or DM the app |
| **Email** | CC or email the configured address (IMAP/SMTP poller) |
| **Telegram** | Message the configured bot |
| **Google Chat** | Mention the app in a space |
| **Discord** | DM the bot, `@mention` it in a channel, or use `/ask` / `/today` slash commands |
| **CLI** | `openexecutive chat` |

## Document Upload

Upload your pitch deck, financial model, strategy docs, or any company documents via the web UI or API. The Executive will reference them when relevant.

```bash
# Via CLI
openexecutive upload deck.pdf model.xlsx strategy.md

# Via API
curl -X POST http://localhost:8000/documents \
  -F "file=@deck.pdf" \
  -F "domain=strategy"
```

## Deployment (Fly.io)

Two environments, each a separate set of Fly apps, driven by branch:

| Environment | Trigger | Workflow | Apps |
|---|---|---|---|
| **dev** | push/merge to `main` (continuous) | `.github/workflows/deploy.yml` | `openexec-api-dev`, `openexec-ui-dev` |
| **qa** | push/merge to `qa` (deliberate promotion) | `.github/workflows/deploy-qa.yml` | `openexec-api-qa`, `openexec-ui-qa` |

Both workflows use `dorny/paths-filter` to deploy only the changed app (API, UI, or both). QA is a stable twin of dev — same image and runtime, only the app name differs (`fly.api.qa.toml` / `fly.ui.qa.toml`) — so it lags `main` and stays vetted. An optional Honcho memory app (`fly.honcho.toml`) deploys independently.

### Topology

| App | Purpose | State |
|-----|---------|-------|
| `openexec-api-{dev,qa}` | FastAPI + scheduler | Persistent volume `executive_data` at `/data` |
| `openexec-ui-{dev,qa}` | Next.js 15 | Stateless |
| `openexec-honcho-dev` | Honcho per-person memory (optional) | Postgres-backed |

> **⚠️ Single-instance only**: The scheduler claims rows via `UPDATE … RETURNING`. Running two API machines would double-fire scheduled actions. `max_machines_running = 1` is set in `fly.api.toml` / `fly.api.qa.toml` — do not override it.

### Required GitHub Actions secrets

Deploys authenticate with per-app Fly deploy tokens stored as repo (or org) Actions secrets. Generate each with `flyctl tokens create deploy -a <app> -x 999999h`:

| Secret | App | Used by |
|---|---|---|
| `FLY_API_TOKEN_API` | `openexec-api-dev` | dev |
| `FLY_API_TOKEN_UI` | `openexec-ui-dev` | dev |
| `FLY_API_TOKEN_HONCHO` | `openexec-honcho-dev` | dev (honcho job) |
| `FLY_API_TOKEN_API_QA` | `openexec-api-qa` | qa |
| `FLY_API_TOKEN_UI_QA` | `openexec-ui-qa` | qa |

Per-app runtime secrets (`ANTHROPIC_API_KEY`, `BACKEND_SHARED_SECRET`, the `AUTH_*` set, integration tokens) are set directly on each Fly app — see `scripts/fly-secrets.sh.example`.

### One-time bootstrap (dev)

```bash
# 1. Create apps and volume
flyctl apps create openexec-api-dev
flyctl apps create openexec-ui-dev
flyctl volumes create executive_data --region iad --size 1 -a openexec-api-dev

# 2. Set the required secret
flyctl secrets set -a openexec-api-dev ANTHROPIC_API_KEY=sk-ant-...

# 3. Create deploy tokens and add as GitHub secrets FLY_API_TOKEN_API and FLY_API_TOKEN_UI
flyctl tokens create deploy -a openexec-api-dev -x 999999h
flyctl tokens create deploy -a openexec-ui-dev  -x 999999h

# 4. First deploy
gh workflow run "Deploy (dev)" -f target=both
```

QA bootstraps the same way against the `-qa` app names (push to the `qa` branch, or `gh workflow run "Deploy (qa)"`). See [docs/deployment.md](docs/deployment.md) for the full runbook (operations, rollback, common failure modes, why `.flycast` isn't used).

### Access control

The deployed UI is gated behind Google sign-in with an email allow-list, and the public API is protected by a shared-secret header between the UI proxy and the FastAPI backend. See [docs/auth.md](docs/auth.md) for the full setup (Google Cloud Console steps, required Fly secrets, adding/removing users, rotating secrets, and a debugging table).

## Configuration

All settings via environment variables. Minimum required: `ANTHROPIC_API_KEY` —
*unless* you configure a local or OpenRouter backend instead (see [Running on
Local Models](#running-on-local-models)). At least one provider must be set or
the app refuses to start.

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes¹ | — | Anthropic API key |
| `DEFAULT_MODEL` | No | `claude-sonnet-4-6` | Executive + most specialists |
| `DEEP_REASONING_MODEL` | No | `claude-opus-4-7` | CSO, CFO, GC, Board |
| `VECTOR_STORE_PATH` | No | `./chroma_db` | ChromaDB directory |
| `EPISODIC_DB_PATH` | No | `./episodic_memory.db` | SQLite for episodic memory |
| `COMPANY_PROFILE_PATH` | No | `./company/profile.yaml` | Company profile |
| `ENABLE_CACHING` | No | `true` | Anthropic prompt caching |
| `ROUTING_MODEL` | No | `claude-haiku-4-5-20251001` | Model for intent routing |
| `SLACK_BOT_TOKEN` | No | — | Slack bot OAuth token |
| `SLACK_APP_TOKEN` | No | — | Slack socket mode token |
| `EXEC_EMAIL_ADDRESS` | No | — | Executive Gmail address (Gmail MCP OAuth) |
| `EMAIL_POLL_INTERVAL_SECONDS` | No | `60` | How often to poll for new email |
| `TELEGRAM_BOT_TOKEN` | No | — | Telegram bot token (from @BotFather) |
| `TELEGRAM_WEBHOOK_SECRET` | No | — | Random string for webhook validation |
| `DISCORD_BOT_TOKEN` | No | — | Discord bot token (Developer Portal → Bot tab) |
| `DISCORD_APP_ID` | No | — | Discord application ID (General Information tab) |
| `DISCORD_GUILD_IDS` | No | — | Comma-separated guild IDs for dev slash-command registration |
| `DISCORD_NOTIFY_CHANNEL_ID` | No | — | Default channel ID for outbound notifications |
| `GOOGLE_CHAT_PROJECT_NUMBER` | No | — | GCP project number for Google Chat |
| `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` | No | — | Path to service account JSON key |
| `GOOGLE_OAUTH_CLIENT_ID` | No | — | Google OAuth client ID (Gmail MCP) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | No | — | Google OAuth client secret (Gmail MCP) |
| `OPENROUTER_ENABLED` | No | `false` | Route Claude calls through OpenRouter and unlock non-Anthropic models per-agent in the Council UI |
| `OPENROUTER_API_KEY` | No | — | Required when `OPENROUTER_ENABLED=true` |
| `LOCAL_MODELS_ENABLED` | No | `false` | Route selected slugs to a local OpenAI-compatible server (Ollama, LM Studio, vLLM, llama.cpp) |
| `LOCAL_BASE_URL` | No | — | Local server URL incl. version path, e.g. `http://localhost:11434/v1`. Required when `LOCAL_MODELS_ENABLED=true` |
| `LOCAL_API_KEY` | No | — | Optional bearer token (vLLM / gateways); Ollama & LM Studio need none |
| `LOCAL_MODELS` | No | — | Comma-separated local model slugs to surface in the Council UI and route locally, e.g. `llama3.3,qwen2.5` |
| `LOCAL_TIMEOUT_S` | No | `300` | Per-call timeout for local generation, in seconds |
| `AGENT_SDK_ENABLED` | No | `false` | Route Claude calls through the Claude Code CLI so they run on a Claude Pro/Max **subscription** instead of a metered API key |
| `AGENT_SDK_CLI_PATH` | No | — | Explicit path to the `claude` executable; defaults to the CLI bundled with `claude-agent-sdk` |
| `AGENT_SDK_TIMEOUT_S` | No | `300` | Per-call timeout for the Agent SDK backend, in seconds |
| `HONCHO_ENABLED` | No | `false` | Per-person memory layer ([honcho.dev](https://honcho.dev)) — a peer card shared across all channels |
| `HONCHO_API_KEY` | No | — | Required when `HONCHO_ENABLED=true` |
| `HONCHO_BASE_URL` | No | — | Self-hosted Honcho endpoint |

See [.env.example](.env.example) for the full list.

> ¹ `ANTHROPIC_API_KEY` is required only when you serve Claude models directly.
> It can be omitted entirely if you run on local models (`LOCAL_MODELS_ENABLED`),
> route through OpenRouter (`OPENROUTER_ENABLED`), or use a Claude subscription
> (`AGENT_SDK_ENABLED`).

## Running on a Claude Subscription

If you have a **Claude Pro or Max** plan, Open Executive can serve Claude calls
through the Claude Code CLI — authenticating with the credentials from
`claude auth login` — instead of a metered `ANTHROPIC_API_KEY`. Usage then draws
on your subscription allowance.

```bash
# 1. Install the optional extra (bundles the CLI, ~95 MB)
cd packages/core && uv sync --extra agent-sdk

# 2. Log in once, interactively.
#    The SDK bundles the CLI inside site-packages but does NOT put `claude`
#    on your PATH, so resolve the bundled binary first:
CLAUDE_BIN=$(uv run python -c "import claude_agent_sdk, pathlib; print(pathlib.Path(claude_agent_sdk.__file__).parent / '_bundled' / 'claude')")
"$CLAUDE_BIN" auth login
"$CLAUDE_BIN" auth status      # expect: "loggedIn": true

# 3. In .env
AGENT_SDK_ENABLED=true
# ...and you can leave ANTHROPIC_API_KEY unset entirely
```

> **Two gotchas worth knowing up front:**
>
> - Use `uv sync --extra agent-sdk`, **not** `uv pip install` — `sync` creates
>   the virtualenv if it doesn't exist yet, whereas `pip install` fails with
>   `No virtual environment found` on a fresh checkout.
> - The command is `claude auth login`, **not** `claude login` (there is no
>   `login` subcommand). And a bare `claude` will be *command not found* unless
>   you have Claude Code installed separately — installing the Python SDK does
>   not add it to your PATH. If you do already have Claude Code on your PATH,
>   just run `claude auth login` and skip the `CLAUDE_BIN` dance.
>
> At runtime the provider finds the bundled CLI on its own, so
> `AGENT_SDK_CLI_PATH` only needs setting if you want to point at a *different*
> `claude` binary.
>
> - A plain `make install` (or `uv sync`) **prunes** the optional extra, since
>   `uv sync` makes the environment match exactly the selected dependency set.
>   Re-add it with `make install-agent-sdk`, or install both at once with
>   `make install UV_EXTRAS="--extra agent-sdk"`.

First-time setup also needs the UI dependencies — `make install` covers both
packages, and `make dev` only *starts* the servers. Next.js 16 requires
Node.js >= 20.9.

**Know the trade-offs before you switch.** The CLI runs its own agent loop, so
this backend is not a byte-identical match for the Anthropic API:

- **Rate limits are the real budget.** One cross-domain chat turn can fan out to
  8 specialists, against your plan's 5-hour and weekly caps.
- `max_tokens` and per-block `cache_control` are not expressible — the CLI owns
  its own output budget and prompt cache. System blocks are flattened in order,
  so the CLI's prefix caching still applies.
- Prior assistant/tool-result turns are replayed as a transcript rather than as
  structured history.
- Anthropic server-side tools (`web_search`) are unavailable on this path.

`OPENROUTER_ENABLED` takes precedence when both are on, so enabling this cannot
silently redirect an existing OpenRouter deployment. Best suited to local
development — a deployed, shared instance should keep using an API key.

## Running from prebuilt images (GHCR)

`.github/workflows/publish-images.yml` builds both images on every push to
`main` and publishes them to GitHub Container Registry:

```
ghcr.io/beardywalrus/openexecutive-api:latest
ghcr.io/beardywalrus/openexecutive-ui:latest
```

Each build also publishes `sha-<commit>` (and `v*` on a tag), so a host can pin
an exact build with `IMAGE_TAG=sha-<commit>`.

On the Docker host — no clone, no build, just the compose file and a `.env`:

```bash
docker compose --env-file .env -f docker/docker-compose.ghcr.yml up -d
```

This is the recommended way to run on a host where `next dev`/`next build` die
with `Bus error`: the UI image is built on `node:22-alpine` (musl), so it never
loads the glibc-linked `@next/swc` binary.

### Running on Unraid

Use a **bind mount to appdata** rather than the managed volume. Set
`DATA_PATH` and the state becomes ordinary files under
`/mnt/user/appdata/openexec/data` — browsable over SMB, backed up with the rest
of appdata, and loadable by copying files in rather than reaching into a volume
through a container.

The API image runs as root and the UI image never touches the mount, so no
`PUID`/`PGID` juggling is needed.

**Compose Manager plugin** (recommended — the API stays unpublished, and
container-name DNS works inside the stack). Install *Compose Manager* from
Community Applications, add a stack, and paste this as the compose file, filling
in the marked values:

```yaml
services:
  api:
    image: ghcr.io/beardywalrus/openexecutive-api:latest
    container_name: openexec-api
    restart: unless-stopped
    expose: ["8000"]
    environment:
      AGENT_SDK_ENABLED: "true"
      CLAUDE_CODE_OAUTH_TOKEN: "PASTE-claude-setup-token-OUTPUT"
      ANTHROPIC_API_KEY: ""
      # The only setting with no default. It is the address the Executive
      # sends and polls as, so it is never guessed — but "" is accepted when
      # the email integration is unused. Omitting the key entirely is what
      # fails, with `EXEC_EMAIL_ADDRESS Field required` at startup.
      EXEC_EMAIL_ADDRESS: ""
      VECTOR_STORE_PATH: /data/chroma_db
      COMPANY_PROFILE_PATH: /data/company/profile.yaml
      EPISODIC_DB_PATH: /data/episodic_memory.db
      ENABLE_CACHING: "true"
    volumes:
      - /mnt/user/appdata/openexec/data:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  ui:
    image: ghcr.io/beardywalrus/openexecutive-ui:latest
    container_name: openexec-ui
    restart: unless-stopped
    ports:
      - "3000:3000"
    environment:
      NODE_ENV: production
      BACKEND_BASE_URL: http://api:8000
      DISABLE_AUTH: "true"
      AUTH_TRUST_HOST: "true"
    depends_on:
      - api
```

Then `http://<unraid-ip>:3000`. Values are inlined deliberately: Compose Manager
stores each stack in its own project directory, and where it looks for a `.env`
varies by version, so a self-contained file removes a step that silently
produces empty variables.

**Or two containers via Add Container.** Unraid's default `bridge` gives no
container-name DNS, so create a user-defined network first (once, over SSH or
the terminal):

```bash
docker network create openexec
```

It then appears in each template's *Network Type* dropdown.

| Field | `openexec-api` | `openexec-ui` |
|---|---|---|
| Repository | `ghcr.io/beardywalrus/openexecutive-api:latest` | `ghcr.io/beardywalrus/openexecutive-ui:latest` |
| Network Type | `openexec` | `openexec` |
| Port | *(none — keep it off the LAN)* | `3000` → `3000` |
| WebUI | — | `http://[IP]:[PORT:3000]` |
| Path | `/data` → `/mnt/user/appdata/openexec/data`, RW | *(none)* |
| Variables | `AGENT_SDK_ENABLED=true`, `CLAUDE_CODE_OAUTH_TOKEN=…`, `EXEC_EMAIL_ADDRESS=` (required — see below), `VECTOR_STORE_PATH=/data/chroma_db`, `COMPANY_PROFILE_PATH=/data/company/profile.yaml`, `EPISODIC_DB_PATH=/data/episodic_memory.db` | `BACKEND_BASE_URL=http://openexec-api:8000`, `DISABLE_AUTH=true`, `AUTH_TRUST_HOST=true`, `NODE_ENV=production` |

`EXEC_EMAIL_ADDRESS` must be **present** even if empty. It is the one setting
with no default — the Executive's own address, never guessed — and leaving the
variable out entirely (rather than setting it to `""`) fails startup with
`EXEC_EMAIL_ADDRESS Field required`. `docker-compose.ghcr.yml` passes
`${EXEC_EMAIL_ADDRESS:-}`, so the Compose route is covered automatically; a
hand-built container template is not.

Note `BACKEND_BASE_URL` differs between the two routes: Compose addresses the
service (`api`), the template UI addresses the container name
(`openexec-api`).

Skipping the custom network means publishing the API and pointing the UI at
`http://<unraid-ip>:8000` — which puts an API with **no authentication** on the
LAN. If you do that, set `BACKEND_SHARED_SECRET` to the same value on both
containers first; the UI stamps it as `x-api-key` and the API rejects requests
without it.

`DISABLE_AUTH=true` means anyone who can reach port 3000 is the executive. That
is the intended mode for a single user on a trusted LAN — see "Running with no
sign-in at all" below before exposing it further.

Updating is *Check for Updates* → *Apply*, or `docker compose pull && up -d`.
State lives in the mount, which neither touches, and the API migrates its own
schema on boot.

### Moving an existing install into Docker

A native install accumulates state in three places, and they do not share a
parent directory:

| What | Native location | In the container |
|---|---|---|
| Company profile, uploaded docs, MCP config, client slots | `company/` beside your `.env` | `/data/company/` |
| Vector store the knowledge search reads | `chroma_db/` beside your `.env` | `/data/chroma_db/` |
| People, decisions, initiatives, sessions, audit | `packages/core/episodic_memory.db` | `/data/episodic_memory.db` |

The database is the one that catches people out: its path is **cwd-relative**
(`EPISODIC_DB_PATH`, defaulting to `./episodic_memory.db`), and `make dev` runs
uvicorn after `cd packages/core` — so it lands there rather than beside the
other two. Copying "the data directory" leaves it behind, and the container
comes up with an empty company.

Stop the app, then package all three:

```bash
make stop                    # add API_PORT=… UI_PORT=… if you overrode them
make docker-export           # -> openexec-state.tar.gz
```

SQLite files are copied through the backup API rather than read off disk, so a
write-ahead log that has not been checkpointed still exports as one consistent
file. The vector store's binary index segments have no such guarantee, which is
why the export refuses to run while the API is up (`--force` overrides).

Copy the tarball to the Docker host, then load it **with the stack down** — the
API writes to the same files, and swapping a database under a running process is
how you get a half-imported install:

```bash
make docker-import STATE=openexec-state.tar.gz
```

Importing into a volume that already holds state is refused rather than silently
merged, because the result of merging two installs is an old database beside a
newer vector store. Pick explicitly:

```bash
make docker-import IMPORT_ARGS=--replace   # move existing state aside, recoverably
make docker-import IMPORT_ARGS=--merge     # extract over it
make docker-import IMPORT_ARGS=--dry-run   # show the command, touch nothing
```

`--replace` moves only the three entries the export manages into
`/data/.superseded-<timestamp>/`, so it is undoable and — importantly — leaves
the rest of the volume alone. `WORKSPACE_MCP_CREDENTIALS_DIR` puts Google
Workspace credentials at `/data/google_credentials`, which the export
deliberately does not contain; a "wipe /data and extract" import would destroy
them. Delete the `.superseded-` directory once the import looks right.

The archive is checked before it reaches the container: the image sets no
`USER`, so `tar` there runs as root, and an archive with absolute or `..` paths
is rejected on the host rather than trusted to the far side of that boundary.
The extract also passes `--no-same-owner --no-same-permissions`, declining to
restore archived ownership and modes — setuid bits included — from a file that
has crossed hosts.

Without a clone on the Docker host, the same thing by hand:

```bash
docker compose --env-file .env -f docker/docker-compose.ghcr.yml run --rm \
  --no-deps -v "$(pwd)/openexec-state.tar.gz:/state.tar.gz:ro" \
  api tar --no-same-owner --no-same-permissions -xzf /state.tar.gz -C /data
```

Then start it:

```bash
docker compose --env-file .env -f docker/docker-compose.ghcr.yml up -d
```

### Updating

New images are published on every push to `main`, so an update is:

```bash
make docker-update      # compose pull, then up -d
```

State lives in the named volume, which neither `pull` nor recreating the
containers touches — so this is the whole update. A database from an older build
upgrades itself on first boot: `initialize_db` runs from the API's lifespan and
its migrations are idempotent and additive (`CREATE TABLE IF NOT EXISTS`, guarded
`ALTER TABLE`). Re-importing is for moving state between machines, not for
updating; you do not need to re-export after an upgrade.

Pin a specific build with `IMAGE_TAG=sha-<commit>` if you want to hold or roll
back.

`run --rm --no-deps` borrows the `api` service purely for its volume mount, so
Compose resolves the volume name itself — worth knowing, because the volume is
named after the Compose project (`docker_executive_data` when the project name
comes from the `docker/` directory), not `executive_data`.

Three things do **not** come across in the tarball, by design:

- **Your `.env`.** Rewrite it for the host rather than copying it: the compose
  file sets `BACKEND_BASE_URL` itself, and a stale `localhost:8001` from a
  native run would be wrong inside the network.
- **Your Claude login.** `claude auth login` writes credentials to your home
  directory, which the container does not share. Generate a token instead —
  see the next section.
- **Google Workspace credentials.** `scripts/mint-google-token.py` writes them
  to `.gworkspace-credentials/`, while the container reads
  `WORKSPACE_MCP_CREDENTIALS_DIR=/data/google_credentials`. Re-mint them against
  the container rather than copying, and note the converse: if you ever point
  `WORKSPACE_MCP_CREDENTIALS_DIR` *inside* `company/`, a live refresh token gets
  swept into the tarball.

Symlinks under `company/` are skipped rather than followed, and the export says
so — otherwise a link into a docs folder elsewhere on the machine would archive
that folder's contents into a file destined for another host. Do check
`company/mcp_servers.json`, which *does* travel: the example uses `$VAR`
references, but nothing stops a literal token being pasted in.

### Running the container on your Claude subscription

The API image ships the Agent SDK, so the container can serve Claude calls from
a Claude Pro/Max subscription instead of a metered key. It authenticates with a
long-lived token rather than an interactive login — generate it on any machine
that can complete the browser flow:

```bash
claude setup-token          # prints a long-lived subscription token
```

Then in `.env` on the Docker host:

```bash
AGENT_SDK_ENABLED=true
CLAUDE_CODE_OAUTH_TOKEN=<the token>
# ANTHROPIC_API_KEY can be left empty
```

The same trade-offs apply as locally — see "Running on a Claude Subscription"
below. Rate limits are the real budget, and one cross-domain turn can fan out to
eight specialists.

> Treat that token like a password: it is your subscription. Keep it in `.env`
> (gitignored), not in the compose file, and only on a host you control.

> The API is deliberately **not** published to the host — the UI reaches it over
> the compose network, and the API has no authentication unless
> `BACKEND_SHARED_SECRET` is set. Set that secret on **both** services if you
> expose it.

### Running with no sign-in at all

Google's redirect-URI rules (HTTPS required, raw IPs rejected) make a plain LAN
install awkward. For a **single-user install on a network you trust**, you can
turn sign-in off entirely:

```bash
DISABLE_AUTH=true
```

Then `http://<host-ip>:3000` just works — no tunnel, no HTTPS, no Google client,
and `AUTH_SECRET` / `AUTH_GOOGLE_*` are unused.

Requests then carry no verified identity, so the backend resolves the caller to
the **principal Person** — the same path the CLI and direct `curl` already use
(`api/routes/chat.py::_resolve_caller_person_id`). For one user that is exactly
right; per-person features (Honcho peer memory, per-user `/today` filtering)
collapse to the principal rather than breaking.

> **There is no second gate behind this.** Anyone who can reach the port is the
> executive: your company documents, memory, audit log and model spend. Do not
> expose that port to the internet, and remember a VPN or Tailscale guest counts
> as "can reach the port". The server logs a warning on every start while it is
> on. For multi-user, keep Google sign-in.

### Google sign-in on a Docker host

Every page is gated by Google OAuth, and Google's redirect-URI rules decide what
is possible here: redirect URIs must use **HTTPS**, and the host **cannot be a
raw IP address** — with a single exception for `localhost` (and `127.0.0.1`),
which may use plain HTTP.

So `http://192.168.1.50:3000` can never be registered, no matter how the
container is configured. Two options that do work:

**1. Reach it over a localhost tunnel** (quickest; nothing to buy or configure)

```bash
ssh -N -L 3000:localhost:3000 user@docker-host   # then browse localhost:3000
```

- Google redirect URI: `http://localhost:3000/api/auth/callback/google`
- Leave `AUTH_URL` **unset** — NextAuth then infers the origin from the request.

**2. Give it a real hostname with HTTPS** (proper multi-user setup)

Put a reverse proxy in front (Caddy, Traefik, Tailscale Funnel, cloudflared) so
the UI is served at e.g. `https://openexec.example.com`, then:

- Google redirect URI: `https://openexec.example.com/api/auth/callback/google`
- `AUTH_URL=https://openexec.example.com`
- `AUTH_TRUST_HOST=true` (already the default here)

Either way the `.env` also needs:

```bash
AUTH_SECRET=$(openssl rand -base64 32)   # without it: "problem with the server configuration"
AUTH_GOOGLE_ID=...apps.googleusercontent.com
AUTH_GOOGLE_SECRET=GOCSPX-...
ALLOWED_EMAILS=you@example.com           # a valid login not listed here is still rejected
```

## Running on Local Models

Open Executive can run against any **OpenAI-compatible** local server — Ollama,
LM Studio, vLLM, or llama.cpp — instead of (or alongside) the Anthropic API.
Local model slugs route to your server through the same provider abstraction the
hosted models use; no agent or orchestrator code changes.

```bash
# 1. Pull a capable, tool-use-friendly model (example: Ollama)
ollama pull llama3.3

# 2. In .env — point at the local server and list the slugs to expose
LOCAL_MODELS_ENABLED=true
LOCAL_BASE_URL=http://localhost:11434/v1   # Ollama default
LOCAL_MODELS=llama3.3

# 3. (Optional) run with NO Anthropic key — make local the default everywhere
DEFAULT_MODEL=llama3.3
DEEP_REASONING_MODEL=llama3.3
ROUTING_MODEL=llama3.3
# ...and leave ANTHROPIC_API_KEY unset
```

The listed slugs appear in the **Council UI** model dropdown, so you can also run
a hybrid setup — keep the Executive on Claude while flipping individual
specialists to a local model per-agent.

**Caveats.** Server-side web search (`ENABLE_WEB_SEARCH`) and Anthropic prompt
caching / extended thinking have no local equivalent and are automatically
disabled for local models. Multi-agent routing leans heavily on tool use, so
pick a model that's strong at it (e.g. Llama 3.3 70B, Qwen2.5) — small models
may route poorly. `LOCAL_API_KEY` is only needed if your server (vLLM, or a
gateway) requires a bearer token; Ollama and LM Studio need none.

## Adding a New Specialist Agent

1. Create `packages/core/openexecutive/agents/your_agent.py` extending `BaseAgent`
2. Add a system prompt constant in `packages/core/openexecutive/prompts/domain_prompts.py`
3. Register in `packages/core/openexecutive/orchestrator/router.py` — add to `SPECIALIST_REGISTRY` and the `specialist` enum in `SPECIALIST_TOOLS`
4. Add domain alias to `DOMAIN_ALIASES` in `packages/core/openexecutive/knowledge/retriever.py`
5. Add knowledge docs to `knowledge/builtin/your_domain/`
6. Add at least 2 eval scenarios to `evals/scenarios/`
7. Submit a PR — CI requires all of the above

## Development

```bash
make dev          # Start FastAPI + Next.js
make test         # Run Python tests
make eval         # Run eval suite
make lint         # Run ruff + mypy
make docker       # Build and run Docker stack

# Unit tests only (no API calls required)
pytest packages/core/tests/unit/ -v
```

## Evaluation System

`evals/` contains 29 scenarios covering all 8 domains, scored by `claude-opus-4-7` as an LLM-as-judge. Each scenario defines a query, simulated company context, expected topics, required specialist routing, and a domain-specific rubric. Five scoring dimensions (persona coherence, domain accuracy, company context utilization, routing quality, actionability) are each rated 1–5. The CI gate requires ≥ 3.5/5 average; any dimension dropping > 10% vs `main` fails the PR.

## Privacy

Everything in `company/` is gitignored — the profile YAML, uploaded documents, and the ChromaDB vector store. None of this leaves your local machine (or your own Fly volume in cloud deployments) except as part of prompts sent to the Anthropic API. Anthropic does not train on API data.

## Contributing

See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md). All PRs must include:
- Working implementation (no stubs)
- Tests for new behavior
- Eval scenarios for new agents or prompt changes

## License

Apache 2.0 — free to use commercially, requires attribution.

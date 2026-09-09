.PHONY: dev dev-wasm stop test lint eval docker docker-export docker-import docker-update clean install install-agent-sdk link-env discord

# UV_EXTRAS lets you pull in optional Python extras, e.g.
#   make install UV_EXTRAS="--extra agent-sdk"
# Without it, `uv sync` installs exactly the base dependency set and PRUNES
# anything else — including a previously installed `agent-sdk` extra.
UV_EXTRAS ?=

# Dev server ports. Override when something already holds the defaults, e.g.
#   make dev API_PORT=8001
#   make dev API_PORT=8001 UI_PORT=3001
# BACKEND_BASE_URL is how the Next server finds the API (it proxies
# /api/backend server-side), so it must follow API_PORT or the UI will keep
# calling :8000 and every request will fail.
API_PORT ?= 8000
UI_PORT  ?= 3000

# Bind address for the API. Default 127.0.0.1 — reachable only from this
# machine. `next dev` already binds all interfaces, so the UI is on your LAN
# at http://<your-ip>:$(UI_PORT) without changing anything here, and its
# server-side proxy reaches the API over loopback either way.
#
# Only set API_HOST=0.0.0.0 if something other than the UI must reach the API
# directly (a phone hitting /chat, another host running the eval suite). The
# API has NO auth unless BACKEND_SHARED_SECRET is set, so exposing it hands
# anyone on the network your company data and your model spend. Set that
# secret first.
API_HOST ?= 127.0.0.1

# Extra env and flags for the UI dev server; `dev-wasm` below sets both.
UI_DEV_ENV ?=
UI_DEV_FLAGS ?=

install: link-env
	cd packages/core && uv sync $(UV_EXTRAS)
	cd packages/ui && npm install

# Next.js only reads .env files from its OWN project root, so the repo-root
# .env — which is where .env.example puts AUTH_SECRET, AUTH_GOOGLE_ID,
# AUTH_GOOGLE_SECRET, ALLOWED_EMAILS and BACKEND_BASE_URL — is invisible to the
# UI. Symptom: the app redirects to /signin as designed, the page renders, and
# then /api/auth/providers returns "There was a problem with the server
# configuration" because NextAuth has no secret and no Google credentials.
#
# Linking it in as .env.local keeps the repo-root .env the single source of
# truth (which .gitignore already assumes) and needs no duplication. .env.local
# is gitignored at any depth and takes precedence over other .env files.
#
# Never clobbers an existing file — the -e/-L pair also catches a broken
# symlink, which -e alone would miss and then fail on.
link-env:
	@if [ -f .env ] && [ ! -e packages/ui/.env.local ] && [ ! -L packages/ui/.env.local ]; then \
		ln -s ../../.env packages/ui/.env.local && \
		echo "Linked packages/ui/.env.local -> ../../.env (UI reads the repo-root .env)"; \
	fi

# Claude subscription backend (AGENT_SDK_ENABLED=true) — see README,
# "Running on a Claude Subscription". Run this after a plain `make install`,
# which prunes the extra.
install-agent-sdk:
	cd packages/core && uv sync --extra agent-sdk

# The API is backgrounded with `&`, so make cannot see it fail: a port clash
# kills uvicorn with "[Errno 98] Address already in use" while the UI starts
# anyway, and you get a working web app whose every request fails against a
# backend that never came up. Check both ports first and refuse to start,
# naming the process in the way — the error message is otherwise the only
# clue, and it names neither the port nor the owner.
#
# Both lines source the repo-root .env into the process env (upstream #44):
# link-env covers the UI, because Next auto-loads only packages/ui/.env*, but
# it does nothing for the API — BACKEND_SHARED_SECRET and
# BACKEND_ALLOWED_ORIGINS are read from os.environ rather than pydantic
# Settings, so dotenv alone left the API's gate silently open. Exported values
# win over .env.local for duplicate keys. .env values must be shell-safe:
# quote anything containing spaces or `$$`.
dev: link-env
	@python3 -c "$$PORT_SCAN" preflight "$(API_PORT)" "$(UI_PORT)"
	@echo "Starting Open Executive (API $(API_HOST):$(API_PORT), UI :$(UI_PORT))..."
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; cd packages/core && uv run uvicorn openexecutive.api.main:app --reload --host $(API_HOST) --port $(API_PORT) &
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; cd packages/ui && $(UI_DEV_ENV) BACKEND_BASE_URL=http://localhost:$(API_PORT) npm run dev -- $(UI_DEV_FLAGS) --port $(UI_PORT)

# Run the UI on the WASM SWC build instead of the native @next/swc binary.
# Needed where that binary is incompatible with the system glibc (Ubuntu 25.10
# / glibc 2.42), which kills `next dev`, `next build` and `next start` outright
# with "Bus error" and no further output.
#
# Two non-obvious requirements, both established by running it:
#   * Turbopack requires native bindings, so WASM implies --webpack. Without
#     it Next loads WASM and then refuses: "Turbopack is not supported on this
#     platform ... Only WebAssembly (WASM) bindings were loaded".
#   * Deleting the native binary is NOT enough on its own — Next re-downloads
#     it into ~/.cache/next-swc. NEXT_TEST_WASM=1 is what actually forces the
#     WASM path and blocks the native loader.
#
# Install the WASM build once (--no-save keeps this host-specific workaround
# out of package.json and the lockfile):
#   cd packages/ui && npm install --no-save @next/swc-wasm-nodejs
#
# Expect noticeably slower compiles than Turbopack.
dev-wasm:
	@$(MAKE) dev UI_DEV_ENV="NEXT_TEST_WASM=1" UI_DEV_FLAGS="--webpack"

# Verify the dev ports are actually free, and fail loudly if they are not.
# The previous `stop` piped lsof's output into xargs and echoed "Stopped."
# unconditionally, so on a machine without lsof (most minimal Ubuntu installs)
# it killed nothing and still reported success — the next `make dev` then died
# with "Address already in use" and no clue why.
define PORT_SCAN
import os, sys

LISTEN = "0A"

def listening(ports):
    """Which of `ports` are in LISTEN state, and the socket inodes holding them.

    Read from /proc rather than shelling out: lsof is absent on most minimal
    Ubuntu installs and ss is not guaranteed either, and this has to work on
    the machine that is already broken.
    """
    busy = {}
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = open(path).read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            f = row.split()
            if len(f) < 10 or f[3] != LISTEN:
                continue
            port = int(f[1].split(":")[1], 16)
            if port in ports:
                busy.setdefault(port, set()).add(f[9])
    return busy

def owner(inodes):
    """The pid and command line holding one of these socket inodes."""
    want = {"socket:[%s]" % i for i in inodes}
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            fds = os.listdir("/proc/%s/fd" % pid)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink("/proc/%s/fd/%s" % (pid, fd)) in want:
                    raw = open("/proc/%s/cmdline" % pid).read().replace(chr(0), " ")
                    cmd = " ".join(raw.split())
                    return pid, (cmd or "unknown")[:120]
            except OSError:
                continue
    return None, None

def describe(busy):
    lines = []
    for port in sorted(busy):
        pid, cmd = owner(busy[port])
        if pid:
            lines.append("  port %d is held by pid %s: %s" % (port, pid, cmd))
        else:
            lines.append("  port %d is held by a process this user cannot inspect" % port)
    return lines

def usage(problem):
    """Refuse rather than guess.

    The ports arrive positionally through the shell, so a Make variable that
    expands to nothing (`make dev API_PORT=`) silently shifts everything left:
    the surviving number gets read as API_PORT when it was UI_PORT, and with
    both blank the scan runs against an empty port set and reports success no
    matter what is actually listening. That is precisely the "killed nothing
    and still said Stopped." failure this check exists to end, so validate
    before trusting the arguments.
    """
    sys.exit(
        "PORT_SCAN: %s\nUsage: PORT_SCAN preflight|stop <api_port> <ui_port>\n"
        "Got: %r\nCheck that API_PORT and UI_PORT are set to real port numbers."
        % (problem, sys.argv[1:])
    )

mode, raw = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
if mode not in ("preflight", "stop"):
    usage("unknown mode %r" % mode)
if len(raw) != 2:
    usage("expected exactly 2 ports, got %d" % len(raw))
try:
    ports = [int(a) for a in raw]
except ValueError:
    usage("ports must be integers")
if not all(0 < port < 65536 for port in ports):
    usage("ports must be in 1-65535")

busy = listening(set(ports))

if mode == "preflight":
    if busy:
        stop = " ".join(
            "%s=%d" % (name, port)
            for name, port in (("API_PORT", ports[0]), ("UI_PORT", ports[1]))
            if port in busy
        )
        sys.exit(
            "Refusing to start — something already holds:\n"
            + "\n".join(describe(busy))
            + "\n\nFree it with:  make stop " + stop
            + "\nOr pick other ports:  make dev API_PORT=... UI_PORT=..."
        )
elif busy:
    sys.exit(
        "Could not free:\n"
        + "\n".join(describe(busy))
        + "\n\nThat process is not ours to kill by name; stop it directly."
    )
else:
    print("Stopped.")
endef
export PORT_SCAN

# Kill by process name first (procps is always present), then by port with
# whichever tool exists. `make dev` backgrounds uvicorn with `&`, so it
# outlives a failed UI start and a closed terminal.
#
# The [u]/[n] bracket trick stops pkill matching the shell running this very
# recipe: the regex "[u]vicorn" matches the string "uvicorn", but this command
# line contains a literal "[u]vicorn", which it does not match. Without it,
# `make stop` kills its own shell ("Terminated") on every run.
#
# lsof and fuser are both attempted rather than either/or — lsof is missing on
# most minimal Ubuntu installs, and a present-but-failing one must not stop us
# reaching the fallback.
stop:
	@-pkill -f "[u]vicorn openexecutive.api.main:app" 2>/dev/null || true
	@-pkill -f "[n]ext dev" 2>/dev/null || true
	@-command -v lsof >/dev/null 2>&1 && lsof -ti:$(API_PORT) -ti:$(UI_PORT) 2>/dev/null | xargs -r kill -9 2>/dev/null || true
	@-command -v fuser >/dev/null 2>&1 && fuser -k $(API_PORT)/tcp $(UI_PORT)/tcp >/dev/null 2>&1 || true
	@sleep 1
	@python3 -c "$$PORT_SCAN" stop "$(API_PORT)" "$(UI_PORT)"

# Package this install's accumulated state — company profile and documents,
# vector store, database — for the Docker volume. The three do not share a
# parent directory and the database path is cwd-relative, so the script
# resolves each the way the app does rather than copying one tree. Stop the
# app first; it refuses to run against a live API unless you pass --force.
#
#   make docker-export                     -> openexec-state.tar.gz
#   make docker-export STATE=/tmp/oe.tgz
#
# Then on the Docker host, see README "Moving an existing install into Docker".
STATE ?= openexec-state.tar.gz
docker-export:
	@python3 scripts/export-state.py -o "$(STATE)"

# Load an exported tarball into the Compose volume, with the stack DOWN — the
# API writes to the same files, and swapping a database under a running
# process is how you get a half-imported install.
#
#   make docker-import                             # refuses if state is already there
#   make docker-import IMPORT_ARGS=--replace       # move it aside first, recoverably
#   make docker-import IMPORT_ARGS=--merge         # extract over it
#   make docker-import IMPORT_ARGS=--dry-run
IMPORT_ARGS ?=
docker-import:
	@scripts/import-state.sh --state "$(STATE)" $(IMPORT_ARGS)

# Pull the latest published images and restart. State lives in the named
# volume, which neither pull nor recreate touches — so this is the whole
# update, and the API migrates its own schema on boot.
docker-update:
	@docker compose --env-file .env -f docker/docker-compose.ghcr.yml pull
	@docker compose --env-file .env -f docker/docker-compose.ghcr.yml up -d

test:
	cd packages/core && uv run pytest tests/ -v --tb=short

lint:
	cd packages/core && uv run ruff check openexecutive/ && uv run mypy openexecutive/

eval:
	cd packages/core && uv run python ../../evals/run_evals.py \
		--scenarios ../../evals/scenarios/ \
		--output ../../evals/results/

# --env-file makes ${VAR} interpolation in docker-compose.yml read the
# repo-root .env (compose only auto-reads docker/.env otherwise). The
# containers additionally load the full .env via each service's env_file.
COMPOSE_ENV_FILE := $(if $(wildcard .env),--env-file .env,)

docker:
	docker compose $(COMPOSE_ENV_FILE) -f docker/docker-compose.yml up --build

docker-down:
	docker compose $(COMPOSE_ENV_FILE) -f docker/docker-compose.yml down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf packages/core/.venv packages/core/.mypy_cache packages/core/.ruff_cache
	rm -rf packages/ui/node_modules packages/ui/.next

discord:
	cd packages/core && uv run python -m openexecutive.integrations.discord_bot

seed-knowledge:
	cd packages/core && uv run python -c "from openexecutive.knowledge.loader import seed_builtin_knowledge; import asyncio; asyncio.run(seed_builtin_knowledge())"

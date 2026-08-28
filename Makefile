.PHONY: dev stop test lint eval docker clean install install-agent-sdk discord

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

install:
	cd packages/core && uv sync $(UV_EXTRAS)
	cd packages/ui && npm install

# Claude subscription backend (AGENT_SDK_ENABLED=true) — see README,
# "Running on a Claude Subscription". Run this after a plain `make install`,
# which prunes the extra.
install-agent-sdk:
	cd packages/core && uv sync --extra agent-sdk

dev:
	@echo "Starting Open Executive (API :$(API_PORT), UI :$(UI_PORT))..."
	@cd packages/core && uv run uvicorn openexecutive.api.main:app --reload --port $(API_PORT) &
	@cd packages/ui && BACKEND_BASE_URL=http://localhost:$(API_PORT) npm run dev -- --port $(UI_PORT)

# Verify the dev ports are actually free, and fail loudly if they are not.
# The previous `stop` piped lsof's output into xargs and echoed "Stopped."
# unconditionally, so on a machine without lsof (most minimal Ubuntu installs)
# it killed nothing and still reported success — the next `make dev` then died
# with "Address already in use" and no clue why.
define STOP_CHECK
import socket, sys
busy = []
for port in ($(API_PORT), $(UI_PORT)):
    sock = socket.socket()
    sock.settimeout(0.3)
    if sock.connect_ex(("127.0.0.1", port)) == 0:
        busy.append(port)
    sock.close()
if busy:
    sys.exit(
        "Could not free port(s): "
        + ", ".join(str(p) for p in busy)
        + " - find the owner with: ss -ltnp | grep -E ':($(API_PORT)|$(UI_PORT))'"
    )
print("Stopped.")
endef
export STOP_CHECK

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
	@python3 -c "$$STOP_CHECK"

test:
	cd packages/core && uv run pytest tests/ -v --tb=short

lint:
	cd packages/core && uv run ruff check openexecutive/ && uv run mypy openexecutive/

eval:
	cd packages/core && uv run python ../../evals/run_evals.py \
		--scenarios ../../evals/scenarios/ \
		--output ../../evals/results/

docker:
	docker compose -f docker/docker-compose.yml up --build

docker-down:
	docker compose -f docker/docker-compose.yml down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf packages/core/.venv packages/core/.mypy_cache packages/core/.ruff_cache
	rm -rf packages/ui/node_modules packages/ui/.next

discord:
	cd packages/core && uv run python -m openexecutive.integrations.discord_bot

seed-knowledge:
	cd packages/core && uv run python -c "from openexecutive.knowledge.loader import seed_builtin_knowledge; import asyncio; asyncio.run(seed_builtin_knowledge())"

.PHONY: help doctor setup up down reset migrate test

help:  ## List available commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

doctor:  ## Check prerequisites (uv, .venv, .env, Docker)
	@bash scripts/doctor.sh

setup:  ## Install Python dependencies and create .env if missing
	uv sync
	@test -f .env || cp .env.example .env
	@$(MAKE) --no-print-directory doctor

up:  ## Start Postgres in Docker (background)
	docker-compose up -d

down:  ## Stop Postgres (data is preserved in the Docker volume)
	docker-compose down

reset:  ## ⚠️  Wipe the database volume and restart fresh
	docker-compose down -v
	docker-compose up -d
	uv run alembic upgrade head

migrate:  ## Apply any pending Alembic migrations
	uv run alembic upgrade head

test:  ## Run all tests (SQLite in-memory, no Docker required)
	uv run pytest

mcp:  ## Start the MCP tool server (stdio transport, for Claude Desktop / agents)
	uv run python -m tuition.tools.mcp_server

agents:  ## Run the tuition agent graph (interactive — prompts for approval)
	uv run python -m tuition.agents.run $(PERIOD) $(PAYPAL_CSV) $(BANK_CSV)

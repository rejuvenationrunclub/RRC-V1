.PHONY: install db-up db-down migrate test test-unit lint fmt typecheck run check

install:        ## Install runtime + dev dependencies into .venv
	uv sync

db-up:          ## Start local PostgreSQL (docker compose)
	docker compose up -d db

db-down:        ## Stop local PostgreSQL (data volume is kept)
	docker compose down

migrate:        ## Apply migrations to DATABASE_URL
	uv run alembic upgrade head

test:           ## Full suite (needs TEST_DATABASE_URL for integration tests)
	uv run pytest -q

test-unit:      ## Unit tests only (no database)
	uv run pytest -q -m "not integration"

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

check: lint typecheck test   ## Everything CI runs (except the secret scan)

run:            ## Run the bot locally (reads .env)
	uv run python -m rrc_bot

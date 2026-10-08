.PHONY: install migrate run test lint format

install:
	pip install -c requirements.lock -e ".[dev]"

migrate:
	alembic upgrade head

run:
	uvicorn app.main:app --reload --no-access-log

test:
	pytest

lint:
	ruff check . && ruff format --check .

format:
	ruff check . --fix && ruff format .

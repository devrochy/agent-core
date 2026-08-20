.PHONY: up down dev migrate seed install install-plugins lint test evals

up:            ## Levanta todo el stack (postgres, redis, api, celery)
	docker-compose up -d --build

down:          ## Detiene el stack
	docker-compose down

dev:           ## API en modo desarrollo (recarga automática)
	uvicorn core.main:app --reload --port 8000

migrate:       ## Corre migraciones del core
	alembic upgrade head

seed:          ## Crea tablas base + usuario admin por defecto
	python scripts/init_db.py

install:       ## Instala el core en modo editable
	pip install -e ".[dev]"

install-plugins: ## Instala todos los plugins locales
	bash scripts/install_plugins.sh

lint:          ## Linters
	ruff check core cli
	mypy core cli || true

test:          ## Tests
	pytest tests -q

evals:         ## Corre los golden scenarios
	python evals/runner.py

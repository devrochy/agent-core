.PHONY: up down dev migrate seed install install-plugins lint test evals

up:            ## Levanta todo el stack (postgres, redis, api, celery)
	docker-compose up -d --build

down:          ## Detiene el stack
	docker-compose down

dev:           ## API en modo desarrollo (recarga automática)
	uvicorn core.main:app --reload --port 8000

migrate:       ## Corre migraciones del core + plugins
	agent-core db migrate

seed:          ## Crea tablas base + usuario admin por defecto
	python scripts/init_db.py

install:       ## Instala el core en modo editable
	pip install -e ".[dev]"

install-plugins: ## Instala todos los plugins locales
	bash scripts/install_plugins.sh

lint:          ## Linters
	ruff check core cli tests evals scripts
	mypy core cli || true

test:          ## Tests unitarios (sin BD/red)
	pytest -q

test-integration: ## Tests de integración (requiere stack local: Postgres+Redis)
	AGENT_CORE_INTEGRATION=1 pytest tests/integration -q

evals:         ## Corre los golden scenarios
	python evals/runner.py

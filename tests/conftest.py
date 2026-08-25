"""Configuración común de los tests.

Los tests de esta suite son deliberadamente puros: no tocan la base de datos, ni
Redis, ni la red, ni requieren API key. Cubren la lógica de bootstrap que hacía
fallar la instalación desde cero.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# core.config exige varias variables; se fijan antes de importar nada del core.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "3sJ8kZ0mQ1nR5tV7wY9bC2eF4gH6jL8mN0pQ2rS4tU6=")
os.environ.setdefault("APP_SECRET_KEY", "test-secret")

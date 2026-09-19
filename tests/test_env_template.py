"""Generación del .env: parseo de URLs, puertos y defaults por proveedor."""

from __future__ import annotations

from cli.preflight import (
    llm_defaults,
    parse_db_url,
    parse_redis_url,
    slugify,
    update_url_port,
)


def test_parse_db_url_completo() -> None:
    info = parse_db_url("postgresql+asyncpg://app:s3cr3t@db.local:5440/midb")
    assert info == {
        "user": "app", "password": "s3cr3t", "host": "db.local",
        "port": "5440", "db": "midb",
    }


def test_parse_db_url_usa_defaults_si_falta_todo() -> None:
    info = parse_db_url("postgresql+asyncpg://localhost")
    assert info["user"] == "agent_core" and info["port"] == "5432"


def test_parse_redis_url_sin_puerto() -> None:
    assert parse_redis_url("redis://localhost/0")["port"] == "6379"


def test_update_url_port_preserva_credenciales() -> None:
    salida = update_url_port("postgresql+asyncpg://u:p@localhost:5432/db", 5440)
    assert salida == "postgresql+asyncpg://u:p@localhost:5440/db"


def test_update_url_port_con_password_que_contiene_dos_puntos_y_digitos() -> None:
    """El regex `(://[^/]+):(\\d+)` era ambiguo con estas contraseñas."""
    salida = update_url_port("postgresql+asyncpg://app:secret:9999@localhost:5432/db", 5441)
    assert salida.endswith("@localhost:5441/db")
    assert "app:secret:9999@" in salida


def test_update_url_port_anade_puerto_si_no_habia() -> None:
    """Sin puerto explícito el regex no casaba y la URL se quedaba sin cambiar."""
    assert update_url_port("redis://localhost/0", 6390) == "redis://localhost:6390/0"


def test_update_url_port_preserva_la_base_de_datos() -> None:
    salida = update_url_port("postgresql+asyncpg://u:p@localhost:5432/produccion", 5500)
    assert salida.endswith("/produccion")


def test_slugify_apto_para_compose() -> None:
    assert slugify("Barbería El Corte") == "barberia-el-corte"
    assert slugify("Mi Asistente!!") == "mi-asistente"


def test_slugify_cae_al_fallback_si_queda_vacio() -> None:
    assert slugify("   ") == "agent-core"
    assert slugify("###") == "agent-core"


def test_llm_defaults_por_proveedor() -> None:
    """El modelo de razonamiento debe ser del proveedor elegido.

    Regresión: la plantilla escribía `deepseek-reasoner` siempre, así que con
    Anthropic el difficulty routing pedía un modelo de DeepSeek a Anthropic.
    """
    assert llm_defaults("anthropic")["reasoning_model"].startswith("claude")
    assert llm_defaults("openai")["reasoning_model"].startswith("gpt")
    assert llm_defaults("deepseek")["reasoning_model"] == "deepseek-reasoner"


def test_llm_defaults_proveedor_desconocido_cae_a_deepseek() -> None:
    assert llm_defaults("inexistente") == llm_defaults("deepseek")


def test_env_template_declara_las_variables_criticas() -> None:
    from cli.commands.init import ENV_TEMPLATE

    for clave in (
        "COMPOSE_PROJECT_NAME", "DATABASE_URL", "REDIS_URL",
        "LLM_MODEL_REASONING", "LLM_BASE_URL", "MASTER_ENCRYPTION_KEY",
        "TOKEN_MASTER_KEYS", "TOKEN_ACTIVE_KEY_ID",
    ):
        assert f"{clave}=" in ENV_TEMPLATE

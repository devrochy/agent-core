"""Configuración central del core (Pydantic Settings).

Toda variable de entorno documentada en .env.example tiene su campo aquí.
Regla: el core NUNCA lee os.environ directamente; siempre a través de `settings`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DebounceMode = Literal["inmediato", "rapido", "natural", "pausado"]
SplitStyle = Literal["un_mensaje", "pocos_cortos", "varios_cortos"]

DEBOUNCE_DELAY_MS: dict[str, int] = {
    "inmediato": 0,
    "corto": 3_000,  # ventana alineada con ADR-0006 (2–4 s)
    "rapido": 5_000,
    "natural": 15_000,
    "pausado": 30_000,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Aplicación ---
    app_name: str = Field(default="Agent Core", alias="APP_NAME")
    app_env: Literal["development", "production"] = Field(default="development", alias="APP_ENV")
    app_secret_key: str = Field(default="change-me", alias="APP_SECRET_KEY")
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")

    # --- Tenants (datos por cliente, fuera del código del framework) ---
    # Carpeta con los artefactos de cada tenant: catalog.json, docs/, evals/.
    # Gitignored por defecto; cada despliegue tiene la suya (ADR-0001).
    tenants_root: str = Field(default="tenants", alias="TENANTS_ROOT")

    # --- Infraestructura ---
    # Default = app_runtime (NO dueño) para que RLS se aplique de verdad aunque
    # alguien arranque sin .env. El dueño solo se usa en MIGRATIONS_DATABASE_URL.
    database_url: str = Field(
        default="postgresql+asyncpg://app_runtime:app_runtime@localhost:5432/agent_core",
        alias="DATABASE_URL",
    )
    # Conexión de MIGRACIONES (dueño de las tablas). El runtime conecta con
    # app_runtime (no dueño) para que RLS se aplique; solo las migraciones usan
    # el dueño. Vacío = usa DATABASE_URL.
    migrations_database_url: str = Field(default="", alias="MIGRATIONS_DATABASE_URL")
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # --- Cifrado ---
    # MASTER_ENCRYPTION_KEY: clave Fernet legada -- SecretBox ya no la usa para
    # cifrar (todo lo nuevo es AES-256-GCM), solo para poder seguir
    # DESCIFRANDO valores viejos que quedaron en ese formato. No borrar hasta
    # confirmar que no queda ningún dato en formato Fernet en producción.
    master_encryption_key: str = Field(default="", alias="MASTER_ENCRYPTION_KEY")
    # TOKEN_MASTER_KEYS: JSON {"<key_id>": "<32 bytes en base64>", ...} -- el
    # set de claves AES-256-GCM disponibles para DESCIFRAR (rotación: nunca se
    # borra una clave vieja mientras haya datos cifrados con ella). Vacío por
    # default: SecretBox.encrypt() falla recién al usarse sin esto configurado
    # (igual criterio que MASTER_ENCRYPTION_KEY hoy), no bloquea el arranque.
    token_master_keys: str = Field(default="{}", alias="TOKEN_MASTER_KEYS")
    # Con qué key_id se CIFRA contenido nuevo (debe existir en TOKEN_MASTER_KEYS).
    token_active_key_id: str = Field(default="", alias="TOKEN_ACTIVE_KEY_ID")

    # --- LLM ---
    llm_provider: Literal["deepseek", "openai", "anthropic", "openai_compatible"] = Field(
        default="deepseek", alias="LLM_PROVIDER"
    )
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_model: str = Field(default="deepseek-chat", alias="LLM_MODEL")
    llm_model_reasoning: str = Field(default="deepseek-reasoner", alias="LLM_MODEL_REASONING")
    llm_base_url: str = Field(default="https://api.deepseek.com", alias="LLM_BASE_URL")
    llm_max_tokens: int = Field(default=4096, alias="LLM_MAX_TOKENS")
    difficulty_routing: bool = Field(default=True, alias="DIFFICULTY_ROUTING")

    # --- Embeddings ---
    embedding_provider: Literal["local", "openai", "none"] = Field(
        default="none", alias="EMBEDDING_PROVIDER"
    )
    embedding_model: str = Field(default="all-MiniLM-L6-v2", alias="EMBEDDING_MODEL")
    embedding_dimensions: int = Field(default=384, alias="EMBEDDING_DIMENSIONS")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")

    # --- WhatsApp (Meta Cloud API) ---
    whatsapp_verify_token: str = Field(default="", alias="WHATSAPP_VERIFY_TOKEN")
    whatsapp_app_secret: str = Field(default="", alias="WHATSAPP_APP_SECRET")
    whatsapp_access_token: str = Field(default="", alias="WHATSAPP_ACCESS_TOKEN")
    whatsapp_phone_number_id: str = Field(default="", alias="WHATSAPP_PHONE_NUMBER_ID")

    # --- Meta App (preflight, ADR pendiente) ---
    # BYOK por tenant (AppSettings.meta_app_config_encrypted); estos 3 son SOLO
    # el fallback opcional de dev/single-tenant, igual criterio que whatsapp_*
    # arriba -- nunca bloquean el arranque si faltan.
    meta_app_id: str = Field(default="", alias="META_APP_ID")
    meta_app_secret: str = Field(default="", alias="META_APP_SECRET")
    meta_config_id: str = Field(default="", alias="META_CONFIG_ID")
    # Versión de Graph API usada SOLO por el módulo de preflight (core/gateway/
    # meta_preflight.py) -- no toca los v21.0 hardcodeados en outbound.py ni en
    # connections.py::verify_meta_credentials (envío de mensajes en producción).
    graph_api_version: str = Field(default="v21.0", alias="META_GRAPH_VERSION")

    # Instagram Login (ruta b): app de Meta DISTINTA (App ID numérico propio,
    # host graph.instagram.com/api.instagram.com) -- nunca reusar
    # meta_app_id/meta_app_secret acá, son credenciales de apps diferentes.
    instagram_app_id: str = Field(default="", alias="INSTAGRAM_APP_ID")
    instagram_app_secret: str = Field(default="", alias="INSTAGRAM_APP_SECRET")
    # Login Configuration DEDICADA para Instagram vía Facebook Login (ruta a):
    # nunca reusar meta_config_id (Messenger/WhatsApp) -- mezclar los dos
    # juegos de permisos en el mismo config_id rompe el consentimiento.
    meta_config_id_instagram: str = Field(default="", alias="META_CONFIG_ID_INSTAGRAM")
    # Feature flag: el envío directo de la ruta (b) (graph.instagram.com/
    # {ig_user_id}/messages) no está verificado contra doc primaria de 2026.
    instagram_direct_send_enabled: bool = Field(default=False, alias="INSTAGRAM_DIRECT_SEND_ENABLED")

    # --- Comportamiento ---
    debounce_mode: DebounceMode = Field(default="natural", alias="DEBOUNCE_MODE")
    max_agent_iterations: int = Field(default=6, alias="MAX_AGENT_ITERATIONS")
    message_split_style: SplitStyle = Field(default="pocos_cortos", alias="MESSAGE_SPLIT_STYLE")

    # --- Modo demo (SOLO desarrollo) ---
    # Activa un LLM fake determinístico y un envío de canal no-op para probar el
    # pipeline sin API key ni credenciales reales. Prohibido en producción.
    demo_mode: bool = Field(default=False, alias="DEMO_MODE")

    # --- Concurrencia (Sprint 1, ADR-0006) ---
    # Turnos concurrentes globales y por tenant. La cuota por tenant impide que un
    # tenant ruidoso ahogue a los demás en el mismo worker.
    max_concurrent_turns: int = Field(default=8, alias="MAX_CONCURRENT_TURNS")
    max_turns_per_tenant: int = Field(default=2, alias="MAX_TURNS_PER_TENANT")

    # --- Observabilidad ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    otel_enabled: bool = Field(default=False, alias="OTEL_ENABLED")
    otel_exporter_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_service_name: str = Field(default="agent-core", alias="OTEL_SERVICE_NAME")


@lru_cache
def get_settings() -> Settings:
    return Settings()

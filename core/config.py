"""Configuración central del core (Pydantic Settings).

Toda variable de entorno documentada en .env.example tiene su campo aquí.
Regla: el core NUNCA lee os.environ directamente; siempre a través de `settings`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, field_validator
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


# Campos cuyo valor nunca se muestra en `repr` (ni en tracebacks con variables locales de rich
# o typer, que imprimen el objeto entero) y contraseñas dentro de URLs.
_CAMPOS_SECRETOS = ("key", "secret", "token", "password", "dsn")
_CLAVE_EN_URL = re.compile(r"(://[^/\s:@]+:)[^@\s/]+@")
OCULTO = "**********"


def _ocultar(nombre: str | None, valor: Any) -> Any:
    if not isinstance(valor, str) or not valor:
        return valor
    if nombre and any(p in nombre.lower() for p in _CAMPOS_SECRETOS):
        return OCULTO
    return _CLAVE_EN_URL.sub(rf"\g<1>{OCULTO}@", valor)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:  # type: ignore[override]
        """`repr`/`str`/rich nunca muestran secretos: un traceback con variables locales que
        pase por un frame con `settings` (el loop, los runners) imprimía la API key."""
        for nombre, valor in super().__repr_args__():
            yield nombre, _ocultar(nombre, valor)

    # --- Aplicación ---
    app_name: str = Field(default="Agent Core", alias="APP_NAME")
    app_env: Literal["development", "production"] = Field(default="development", alias="APP_ENV")
    app_secret_key: str = Field(default="change-me", alias="APP_SECRET_KEY")
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")
    # URL del panel nuevo: a donde vuelve el navegador tras el OAuth de Google.
    # En producción suele coincidir con el origen público; en dev apunta al :3200.
    panel_base_url: str = Field(default="", alias="PANEL_BASE_URL")
    # Landing pública: de donde se sirve el widget embebible del chat web
    # (/widget.js) y el formulario alojado. Se usa para armar el fragmento
    # que el negocio pega en su sitio (HU-CHAN-02).
    landing_base_url: str = Field(default="https://www.solvershub.site", alias="LANDING_BASE_URL")
    # Prefijo de ruta bajo el que se sirve la app detrás de un proxy inverso
    # (p. ej. "/agent-core" en Coolify/Tailscale). Vacío = servida en la raíz.
    # El proxy QUITA el prefijo al reenviar; para las URLs que genera la app
    # (redirects, links, estáticos) lo vuelve a agregar `PathPrefixMiddleware`
    # (ADR-0073).
    root_path: str = Field(default="", alias="ROOT_PATH")

    @field_validator("root_path", mode="after")
    @classmethod
    def _normalize_root_path(cls, value: str) -> str:
        stripped = (value or "").strip().strip("/")
        return f"/{stripped}" if stripped else ""

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
    # Proxies de confianza (CIDR, separados por coma): solo si la conexión llega
    # desde uno de estos se leen `X-Forwarded-For`/`CF-Connecting-IP` para saber la
    # IP del cliente (bloqueo de login, HU-ARCH-04). Por defecto, las redes privadas
    # y loopback: Traefik de Coolify, cloudflared y el BFF del panel viven ahí.
    trusted_proxy_cidrs: str = Field(
        default="127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,::1/128,fc00::/7",
        alias="TRUSTED_PROXY_CIDRS",
    )

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
    # Modelos vigentes de DeepSeek (V4). `deepseek-chat` y `deepseek-reasoner` se
    # discontinuaron el 2026-07-24: no usarlos, ni como respaldo (P-05/D-22).
    llm_model: str = Field(default="deepseek-flash", alias="LLM_MODEL")
    llm_model_reasoning: str = Field(default="deepseek-flash", alias="LLM_MODEL_REASONING")
    # Modelo de respaldo ante error del proveedor o fallas de tools > 5 %/h.
    llm_model_fallback: str = Field(default="gpt-4o-mini", alias="LLM_MODEL_FALLBACK")
    llm_base_url: str = Field(default="https://api.deepseek.com", alias="LLM_BASE_URL")
    # Modo de razonamiento para modelos V4: los IDs explícitos lo activan por
    # defecto, así que el chat normal debe apagarlo explícitamente.
    llm_thinking_default: bool = Field(default=False, alias="LLM_THINKING_DEFAULT")
    llm_thinking_reasoning: bool = Field(default=True, alias="LLM_THINKING_REASONING")
    llm_max_tokens: int = Field(default=4096, alias="LLM_MAX_TOKENS")
    difficulty_routing: bool = Field(default=True, alias="DIFFICULTY_ROUTING")

    # --- Embeddings ---
    embedding_provider: Literal["local", "openai", "none"] = Field(
        default="none", alias="EMBEDDING_PROVIDER"
    )
    embedding_model: str = Field(default="all-MiniLM-L6-v2", alias="EMBEDDING_MODEL")
    embedding_dimensions: int = Field(default=384, alias="EMBEDDING_DIMENSIONS")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    # Retención de la memoria semántica en días (ADR-0053). 0 = sin purga automática.
    memory_retention_days: int = Field(default=0, alias="MEMORY_RETENTION_DAYS")

    # --- WhatsApp (Meta Cloud API) ---
    whatsapp_verify_token: str = Field(default="", alias="WHATSAPP_VERIFY_TOKEN")
    whatsapp_app_secret: str = Field(default="", alias="WHATSAPP_APP_SECRET")
    # Webhook fail-closed (HU-ARCH-05): sin `app_secret` resoluble, un POST se
    # rechaza con 401. En test se puede permitir explícitamente sin firma, con
    # aviso en el log. Nunca activarlo en producción.
    webhook_allow_unsigned: bool = Field(default=False, alias="WEBHOOK_ALLOW_UNSIGNED")
    whatsapp_access_token: str = Field(default="", alias="WHATSAPP_ACCESS_TOKEN")
    whatsapp_phone_number_id: str = Field(default="", alias="WHATSAPP_PHONE_NUMBER_ID")

    # --- Canal WhatsApp Web no oficial (waweb, ADR-0074) ---
    # Canal NO oficial con riesgo de suspensión. El connector está apagado por
    # defecto: en producción NO debe correr salvo habilitación explícita.
    waweb_enabled: bool = Field(default=False, alias="WAWEB_ENABLED")
    # Directorio de sesiones efímeras (idealmente un volumen tmpfs). La copia
    # durable y cifrada vive en `waweb_sessions.creds_encrypted`.
    waweb_session_dir: str = Field(default="/tmp/waweb-sessions", alias="WAWEB_SESSION_DIR")
    waweb_sweep_interval_s: int = Field(default=15, alias="WAWEB_SWEEP_INTERVAL_S")
    waweb_reconnect_max_s: int = Field(default=60, alias="WAWEB_RECONNECT_MAX_S")

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

    # --- Checklist de promoción a producción ---
    # Hash SHA-256 (hex) del META_APP_SECRET de DESARROLLO, copiado UNA VEZ a
    # mano a las env vars de producción (ver docs/despliegue-coolify.md) --
    # nunca se comparan los secretos en texto plano, solo hashes. Vacío =
    # check no configurado (`ok=None`, nunca `True` por default).
    dev_app_secret_hash: str = Field(default="", alias="DEV_APP_SECRET_HASH")

    # --- Comportamiento ---
    debounce_mode: DebounceMode = Field(default="natural", alias="DEBOUNCE_MODE")
    max_agent_iterations: int = Field(default=6, alias="MAX_AGENT_ITERATIONS")
    message_split_style: SplitStyle = Field(default="un_mensaje", alias="MESSAGE_SPLIT_STYLE")

    # --- Modo demo (SOLO desarrollo) ---
    # Activa un LLM fake determinístico y un envío de canal no-op para probar el
    # pipeline sin API key ni credenciales reales. Prohibido en producción.
    demo_mode: bool = Field(default=False, alias="DEMO_MODE")

    # --- Concurrencia (Sprint 1, ADR-0006) ---
    # Turnos concurrentes globales y por tenant. La cuota por tenant impide que un
    # tenant ruidoso ahogue a los demás en el mismo worker.
    max_concurrent_turns: int = Field(default=8, alias="MAX_CONCURRENT_TURNS")
    max_turns_per_tenant: int = Field(default=2, alias="MAX_TURNS_PER_TENANT")

    # --- Reclamo de pendientes del consumer (HU-ARCH-06, ADR-0090) ---
    # Un mensaje que quedó `pending` (crash, deploy, turno colgado) se reclama con
    # XAUTOCLAIM tras este idle. Un claim de idempotencia incompleto más viejo que
    # `claim_stale_seconds` se considera abandonado y se revoca para reprocesar.
    # Invariante: turn_timeout_s < inbound_reclaim_idle_ms < claim_stale_seconds,
    # así un turno acotado nunca se confunde con un abandono.
    inbound_reclaim_idle_ms: int = Field(default=300_000, alias="INBOUND_RECLAIM_IDLE_MS")
    claim_stale_seconds: int = Field(default=600, alias="CLAIM_STALE_SECONDS")

    # --- Presupuesto de un turno (HU-ARCH-27) ---
    # `turn_timeout_s` es el presupuesto TOTAL del turno (todas las llamadas al
    # LLM y tools): al agotarse se corta y el mensaje no queda colgado. La espera
    # por el cupo del tenant tiene tope (`turn_queue_timeout_s`); si se agota, el
    # turno se reprograma/reclama en vez de esperar para siempre. `turn_slow_warn_s`
    # registra los turnos lentos para ver el problema antes de que sea incidente.
    turn_timeout_s: int = Field(default=180, alias="TURN_TIMEOUT_S")
    turn_queue_timeout_s: int = Field(default=30, alias="TURN_QUEUE_TIMEOUT_S")
    turn_slow_warn_s: int = Field(default=30, alias="TURN_SLOW_WARN_S")

    # --- Costos de Meta ---
    # Franquicia de 1000 mensajes de servicio gratis/mes por número: cifra
    # de FUENTE SECUNDARIA, la página oficial de Meta no la menciona. Se
    # muestra en el panel como HIPÓTESIS A VALIDAR contra la primera
    # factura real, nunca se resta del `cost` autoritativo. Default
    # apagado -- se prende explícitamente para probarla.
    meta_service_message_allowance_hypothesis_enabled: bool = Field(
        default=False, alias="META_SERVICE_MESSAGE_ALLOWANCE_HYPOTHESIS_ENABLED"
    )

    # --- Planes y facturación de tenants (ADR-0045 / ADR-0046) ---
    # Cobro de la SUSCRIPCIÓN de plataforma (el tenant nos paga a nosotros), no
    # el cobro del negocio del tenant (eso es pack-payments). Credenciales
    # propias de la plataforma; sin ellas, la pasarela queda deshabilitada.
    billing_provider: Literal["none", "wompi"] = Field(default="none", alias="BILLING_PROVIDER")
    billing_currency: str = Field(default="COP", alias="BILLING_CURRENCY")
    billing_trial_days: int = Field(default=30, alias="BILLING_TRIAL_DAYS")
    wompi_billing_public_key: str = Field(default="", alias="WOMPI_BILLING_PUBLIC_KEY")
    wompi_billing_private_key: str = Field(default="", alias="WOMPI_BILLING_PRIVATE_KEY")
    wompi_billing_events_secret: str = Field(default="", alias="WOMPI_BILLING_EVENTS_SECRET")
    # Sandbox por default (mismo criterio que pack-payments): producción se
    # habilita explícitamente.
    wompi_billing_sandbox: bool = Field(default=True, alias="WOMPI_BILLING_SANDBOX")
    # R-05 (HU-SH-04, ADR-0123): Wompi admite UNA URL de eventos por cuenta y ambiente. Si la
    # cuenta de la plataforma es la misma que usa el pack de pagos de un negocio (el de
    # `solvershub`, que vende los planes), su URL apunta a `/webhooks/billing/wompi` y los
    # eventos que no son de un pago de la plataforma se reenvían a ESE negocio (UUID). Vacío =
    # cuentas separadas: no se reenvía nada (fail-closed).
    billing_wompi_eventos_tenant: str = Field(default="", alias="BILLING_WOMPI_EVENTOS_TENANT")
    # Días antes del fin del período en que el job de renovación genera el cobro del
    # siguiente y avisa por el canal preferido del negocio (HU-SH-04).
    billing_dias_renovacion: int = Field(default=5, ge=1, le=28, alias="BILLING_DIAS_RENOVACION")

    # --- Aprovisionamiento automático de negocios (HU-SH-03, ADR-0121) ---
    # Negocios VENDEDORES por UUID, separados por coma (p. ej. el de `solvershub`): solo
    # un `subscription.activated` de uno de ellos crea un negocio nuevo. Vacío = apagado.
    # Va en el worker (ahí corre el despachador del outbox). No hay token de servicio.
    # Desde HU-SH-04 (ADR-0123) también es el canal asistente del cobro de la plataforma.
    provisioning_seller_tenants: str = Field(default="", alias="PROVISIONING_SELLER_TENANTS")
    # Antiabuso del alta (HU-SH-07, ADR-0125). Pedidos de planes de la plataforma por hora
    # desde un mismo origen (el cliente del chat): pasado el cupo, se frenan. Va en la app,
    # donde corren las tools.
    alta_pedidos_por_hora: int = Field(default=3, ge=1, le=100, alias="ALTA_PEDIDOS_POR_HORA")
    # Pedidos de alta de la misma persona (cliente, teléfono o documento) en 24 h a partir de
    # los cuales el alta va a revisión humana («muchos intentos»). Va en el worker.
    alta_intentos_revision: int = Field(default=3, ge=2, le=100, alias="ALTA_INTENTOS_REVISION")

    # --- Observabilidad ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    otel_enabled: bool = Field(default=False, alias="OTEL_ENABLED")
    otel_exporter_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    otel_service_name: str = Field(default="agent-core", alias="OTEL_SERVICE_NAME")

    # --- Correo de sistema (recuperación de contraseña, ADR-0098) ---
    # SMTP de la plataforma, distinto del canal SMTP por tenant (que exige
    # contexto de negocio). Sin host configurado, el envío es log-only
    # (mismo criterio que las alertas de Slack).
    system_smtp_host: str = Field(default="", alias="SYSTEM_SMTP_HOST")
    system_smtp_port: int = Field(default=587, alias="SYSTEM_SMTP_PORT")
    system_smtp_username: str = Field(default="", alias="SYSTEM_SMTP_USERNAME")
    system_smtp_password: str = Field(default="", alias="SYSTEM_SMTP_PASSWORD")
    system_smtp_from: str = Field(default="", alias="SYSTEM_SMTP_FROM")

    # --- Notificaciones push de la app oficial (ADR-0099) ---
    # Expo Push Service con el projectId de EAS existente. Sin
    # EXPO_PUSH_ENABLED, el envío es log-only (mismo criterio que el correo de
    # sistema): un canal sin configurar no rompe el flujo.
    expo_push_enabled: bool = Field(default=False, alias="EXPO_PUSH_ENABLED")
    expo_push_url: str = Field(
        default="https://exp.host/--/api/v2/push/send", alias="EXPO_PUSH_URL"
    )
    expo_push_access_token: str = Field(default="", alias="EXPO_PUSH_ACCESS_TOKEN")

    # --- Alertas (salud de conexiones Meta) ---
    # Sin webhook configurado, send_alert() solo loguea (nunca fue un canal
    # real hasta este ticket -- antes todo esto era log.error a secas).
    slack_alerts_webhook_url: str = Field(default="", alias="SLACK_ALERTS_WEBHOOK_URL")
    # Sentry es OPCIONAL, mismo patrón que OTEL: sin DSN, configure_sentry()
    # es no-op. before_send redacta tokens de Meta como red de última
    # instancia, no como primera línea de defensa.
    sentry_enabled: bool = Field(default=False, alias="SENTRY_ENABLED")
    sentry_dsn: str = Field(default="", alias="SENTRY_DSN")


@lru_cache
def get_settings() -> Settings:
    return Settings()

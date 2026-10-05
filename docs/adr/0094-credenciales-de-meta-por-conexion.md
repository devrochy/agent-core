# ADR-0094 — Credenciales de Meta por conexión, con el origen guardado

- **Estado:** aceptado
- **Fecha:** 2026-10-05
- **Contexto:** HU-CONN-01 (SPEC-08, `solvershub-docs`); relacionado con ADR-0030 (custodia de tokens) y ADR-0031 (plantillas por WABA)

## Contexto

Una conexión de Meta se crea por una de tres vías y cada una guarda sus credenciales distinto:

| Alta | Token de acceso | App y App Secret |
|---|---|---|
| BYOK (`save_byok_connection`) | `cloud_api_access_token`, `subject_id = meta_app_id` | los del negocio: `app_access`, `subject_id = meta_app_id` |
| Embedded Signup (`meta_onboarding`) | `business_integration_system_user`, `subject_id = business_id` (o el id de la conexión) | los de la plataforma (`META_APP_ID` / `META_APP_SECRET`) |
| Carga manual (`POST /admin/meta/manual`) | `business_integration_system_user`, igual que ES | los de la plataforma |

Cada servicio decidía por su cuenta qué leer, y casi todos suponían Embedded Signup: leían siempre el token
`business_integration_system_user` (que una conexión BYOK no tiene) y firmaban el `appsecret_proof` con
`META_APP_SECRET` (que no es el secreto de la app del negocio). Resultado verificado: Números, Plantillas,
salud, créditos y precios fallaban con BYOK, que es hoy el único alta que funciona de punta a punta. Los crons
de plantillas y de precios exigían los dos tokens a la vez (`TOKEN_KIND` y `app_access`) y ninguna alta los
guarda juntos (una conexión BYOK no tiene token de system user; una de ES no tiene `app_access`), así que
**no sincronizaban ninguna conexión real**. Además `run_health_checks` y los endpoints de
token tomaban el secreto de "la" conexión del tenant (`resolve_app_credentials`, `resolve_effective_app_secret`),
que con varias conexiones podía no ser la que se estaba revisando.

## Decisión

1. **`meta_connections.origin`** (`byok` | `embedded_signup` | `manual`, `NOT NULL`, default `manual`, CHECK
   `ck_meta_connections_origin`; migración `0051`). Lo fija cada alta al crear la conexión; el wizard de
   Embedded Signup lo fija también al guardar el token si reutilizó una conexión viva de otro origen.
   Se **guarda**: el código ya no lo infiere mirando qué tokens existen.
2. **Resolver único** `core/gateway/meta_credentials.py`:
   `credentials_for(connection, *, waba_id=None) -> MetaCredentials`
   (`origin`, `app_id`, `access_token`, `app_secret`, `graph_api_version`, `waba_id`, `business_id`).

   | origin | token | app_id / app_secret |
   |---|---|---|
   | `byok` | `cloud_api_access_token` de la conexión | `meta_app_id` y `app_access` de la conexión |
   | `embedded_signup`, `manual` | `business_integration_system_user` de la conexión | los de la plataforma |

   Sin credenciales utilizables lanza `CredentialsUnavailable(reason=…)`; el `reason` es un slug estable y el
   mensaje nunca lleva secretos. `MetaCredentials.__repr__` oculta token y secreto.
3. **Todo llamado a Graph sobre una conexión recibe `MetaCredentials`**, y el `appsecret_proof` se calcula con
   el secreto de esa conexión. Migrados: Números, Plantillas (servicios, panel SSR y API), salud
   (`run_health_checks`, `resubscribe_waba`), costos y créditos, los endpoints de token BYOK, y los crons de
   plantillas y de precios (por conexión, con el error registrado por conexión y sin cortar a las demás).
   `resolve_waba_and_token` se reemplaza por `resolve_waba_and_credentials`; la copia duplicada del panel SSR se elimina.
4. **Aislamiento en tres capas** (no una sola): RLS del token store bajo el `tenant_scope` activo; el AAD de
   AES-GCM, que ata cada fila a `tenant:conexión:kind` (el resolver pasa el `tenant_id` de la propia conexión);
   y un chequeo explícito `connection.tenant_id == tenant actual`. Una conexión eliminada (`deleted_at`) no
   presta credenciales (invariante 8).
5. **Backfill de la migración**, con la evidencia que dejó cada alta (el origen es un hecho histórico, no se
   filtra por `revoked_at`): token `cloud_api_access_token` → `byok`; token de system user o wizard asociado →
   `embedded_signup`; `app_access` o `webhook_verify_token` → `byok`; sin evidencia → `manual`. Se recorre
   **tenant por tenant fijando `app.tenant_id`**: con `FORCE ROW LEVEL SECURITY` un `UPDATE` de un rol sin
   bypass no falla, afecta 0 filas y dejaría todo en `manual` en silencio. Se verifica por tenant que no quede
   ninguna fila sin origen, y `SET NOT NULL` aborta la migración si algo escapó.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Seguir infiriendo el origen por los tokens que existen | Es el origen del defecto: una conexión a medio guardar o con un token revocado se clasifica mal, y cada servicio lo reimplementa |
| Un `kind` de token único para BYOK y ES | Cambia el CHECK de `meta_tokens`, exige re-cifrar filas existentes (el `kind` entra en el AAD) y no resuelve de dónde sale el secreto |
| Que cada servicio reciba un `app_secret` y lo busque el llamador | Mantiene N lugares donde elegir mal el secreto; no hay un punto donde auditar el aislamiento |
| Backfill con un único `UPDATE` global | Silenciosamente no actualiza nada si el rol de migraciones no tiene `BYPASSRLS` |

## Consecuencias

**Positivas:** BYOK funciona en Números, Plantillas, salud, costos, créditos y token; hay un solo punto donde
se decide de dónde sale cada credencial y donde se prueba el aislamiento entre negocios; los crons procesan
las conexiones (antes no procesaban ninguna).

**Negativas / lo que aceptamos pagar:**
- Los crons de plantillas (cada hora) y de precios (cada día) ahora llaman de verdad a Graph para las
  conexiones de Embedded Signup y BYOK: más tráfico a Meta que antes, que era cero.
- La verificación de salud ahora corre completa para las conexiones BYOK (antes devolvía solo dos checks): una
  conexión con el token vencido pasará a `degraded`/`error` en el próximo ciclo. Es lo correcto, pero visible.
- Conexiones `manual` cuyo token se validó con las credenciales BYOK del negocio se resuelven con la app de la
  plataforma, como pide el diseño; ver "Fuera de alcance".

**Qué haría que revisáramos esto:** un cuarto tipo de alta (p. ej. conexiones de Instagram/Messenger con su
propia app) o credenciales por conexión que roten por otro mecanismo que el token store.

## Fuera de alcance (queda anotado en `docs/BACKLOG.md`)

- Suscripciones y webhook (`meta_subscriptions*.py`, `resolve_app_credentials`, `resolve_effective_app_secret`
  en el webhook) siguen eligiendo "la" conexión del tenant: es HU-CONN-05 (webhook y secreto por conexión).
- Descarga de media: el core no la hace hoy (`url: ""` en `inbound.py`); HU-MEDIA-02 deberá usar `credentials_for`.
- `POST /admin/meta/manual` valida el token con las credenciales del tenant (o la plataforma) y la conexión se
  resuelve siempre con la plataforma.
- `meta_private.meta_tokens` tiene RLS habilitada pero no forzada (la migración `0029` solo cubrió `public`).

## Impacto en el código

- `alembic/versions/0051_meta_connections_origin.py`, `core/db/models.py` (`MetaConnection.origin`).
- `core/gateway/meta_credentials.py` (nuevo); altas: `meta_byok.py`, `meta_onboarding.py`, `api/routers/meta_wizard.py`.
- Consumidores: `admin/meta_numbers_service.py`, `meta_templates_service.py`, `meta_health_service.py`,
  `meta_costs_service.py`, `admin/views.py`, `api/routers/meta_ops.py`, `gateway/meta_health.py`,
  `tasks/message_template_sync.py`, `tasks/meta_pricing_sync.py`.
- Tests: `tests/test_meta_credentials.py`, `tests/integration/test_meta_credentials_origin.py`.

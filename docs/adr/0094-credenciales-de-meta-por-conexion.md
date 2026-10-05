# ADR-0094 — Credenciales de Meta por conexión, con el origen guardado

- **Estado:** aceptado
- **Fecha:** 2026-10-05
- **Supersede (parcial):** ADR-0040 §3 (el sender elegía el token probando "BYOK primero") y ADR-0041 §5
  (el aviso de vencimiento y el cron diario también elegían el token por inferencia). El resto de ambos sigue vigente.
- **Enmienda:** ADR-0034, "Hallazgo del auditor-multitenant (2026-09-20) corregido": la carga manual vuelve a
  validar el token con la app de la plataforma (ver Decisión 6).
- **Contexto:** HU-CONN-01 (SPEC-08, `solvershub-docs`); relacionado con ADR-0027 (cifrado AES-GCM: el AAD ata cada
  token a `tenant:conexión:kind`), ADR-0030 (wizard de Embedded Signup) y ADR-0031 (plantillas por WABA)

## Contexto

Una conexión de Meta se crea por una de tres vías y cada una guarda sus credenciales distinto:

| Alta | Token de acceso | App y App Secret |
|---|---|---|
| BYOK (`save_byok_connection`) | `cloud_api_access_token`, `subject_id = meta_app_id` | los del negocio: `app_access`, `subject_id = meta_app_id` |
| Embedded Signup (`meta_onboarding`) | `business_integration_system_user`, `subject_id = business_id` (o el id de la conexión) | los de la plataforma (`META_APP_ID` / `META_APP_SECRET`) |
| Carga manual (`POST /api/v1/admin/meta/manual`) | `business_integration_system_user`, igual que ES | los de la plataforma |

Cada servicio decidía por su cuenta qué leer, y casi todos suponían Embedded Signup: leían siempre el token
`business_integration_system_user` (que una conexión BYOK no tiene) y firmaban el `appsecret_proof` con
`META_APP_SECRET` (que no es el secreto de la app del negocio). Resultado verificado: Números, Plantillas,
salud, créditos y precios fallaban con BYOK, que es hoy el único alta que funciona de punta a punta. Los crons
de plantillas y de precios exigían los dos tokens a la vez (`TOKEN_KIND` y `app_access`) y ninguna alta los
guarda juntos (una conexión BYOK no tiene token de system user; una de ES no tiene `app_access`), así que
**no sincronizaban ninguna conexión real**. `run_health_checks` y los endpoints de token tomaban el secreto
de "la" conexión del tenant (`resolve_app_credentials`, `resolve_effective_app_secret`), que con varias
conexiones podía no ser la que se estaba revisando.

Además, el sender (ADR-0040 §3) y los avisos de vencimiento (ADR-0041 §5) probaban "BYOK primero". Una conexión
BYOK que el wizard de Embedded Signup reutiliza conserva su token BYOK (temporal, de 24 h) junto al de ES: la
salud validaba el de ES mientras el sender enviaba con el viejo (panel en verde, agente mudo) y el cron alertaba
cada día por un token que ya no se usaba.

## Decisión

1. **`meta_connections.origin`** (`byok` | `embedded_signup` | `manual`, `NOT NULL` **sin valor por defecto**,
   CHECK `ck_meta_connections_origin`; migración `0051`). Es el **modo de credenciales vigente** de la conexión,
   no un dato histórico:
   - lo fija cada alta al crear la conexión;
   - cambia solo cuando otra alta guarda sus credenciales sobre la misma conexión: el wizard de Embedded Signup
     que reutiliza una conexión viva la pasa a `embedded_signup` al guardar el token del system user, y BYOK que
     reconecta la misma app la deja en `byok`;
   - sin default: un alta que lo olvide falla con `NOT NULL`, en vez de quedar en silencio con las credenciales
     de la plataforma (el secreto más sensible no puede ser el valor por defecto).
   Tras una toma por ES, `meta_app_id` sigue siendo el de la app BYOK anterior; alinearlo (y su índice único
   `uq_meta_connections_tenant_app`) queda para HU-CONN-05. Ningún consumidor deduce el origen de `meta_app_id`.
2. **Una sola regla de qué token usa cada origen**, en `core/gateway/meta_credentials.py`:
   - `access_token_refs(connection)`: `(kind, subject_id)` del token de acceso según `origin` (BYOK →
     `cloud_api_access_token`; ES y manual → token del system user por `business_id` y, si no, por el id de la
     conexión);
   - `access_token_for(connection)`: solo el token, sin App Secret (lo usa el sender, que no firma con
     `appsecret_proof`: exigirle el secreto rompería envíos que no lo necesitan);
   - `access_token_expires_at(connection)`: el vencimiento del token que de verdad se usa;
   - `credentials_for(connection) -> MetaCredentials` (`origin`, `app_id`, `access_token`, `app_secret`,
     `graph_api_version`, `waba_id`, `business_id`): BYOK → `meta_app_id` y `app_access` de la conexión; ES y
     manual → la app de la plataforma. Sin credenciales lanza `CredentialsUnavailable(reason=…)`; el `reason` es
     un slug estable y el mensaje nunca lleva secretos. `MetaCredentials.__repr__` oculta token y secreto.
   - `origin_label(origin)`: la etiqueta de la columna "Origen" del panel ("BYOK (app propia)", "Embedded
     Signup", "Carga manual").
3. **Todo consumidor usa esa regla.** Con `credentials_for`: Números, Plantillas (servicios, panel SSR y API),
   salud (`run_health_checks`, `resubscribe_waba`), costos/créditos, endpoints de token BYOK y los crons de
   plantillas y de precios (por conexión, con el error registrado por conexión y sin cortar a las demás).
   Con `access_token_for`: el sender. Con `access_token_expires_at`: el cron de vencimiento, el resumen de salud,
   la página de salud del panel SSR e `inspect`. Las etiquetas de origen salen de la columna, no de comparar
   `meta_app_id` con la app de la plataforma. `resolve_waba_and_token` se reemplaza por
   `resolve_waba_and_credentials`; la copia del panel SSR se elimina.
4. **Aislamiento.** `credentials_for` exige un `tenant_scope` activo y que sea el de la conexión
   (`no_tenant_scope` / `tenant_mismatch`, antes de tocar el token store); el token store lee bajo RLS y con el
   AAD de ADR-0027 armado con el `tenant_id` de la propia conexión. Una conexión eliminada no presta
   credenciales (invariante 8). El lock horario de sincronización de plantillas lleva el tenant en la llave
   (`waba_id` no es único entre negocios).
5. **Backfill de la migración**, con la evidencia de cada alta (subconsultas correlacionadas también por
   `tenant_id`): token `cloud_api_access_token` → `byok`; token de system user o wizard asociado →
   `embedded_signup`; `app_access`/`webhook_verify_token` → `byok`; sin evidencia → `manual`. Con tokens de los
   dos orígenes gana `byok`: es el token con el que el sender enviaba hasta esta migración, así el despliegue no
   cambia con qué token sale cada mensaje; esas conexiones se listan en el log de la migración para revisarlas a
   mano (`debug_token`). Se recorre **tenant por tenant fijando `app.tenant_id`**: con `FORCE ROW LEVEL
   SECURITY`, un `UPDATE` de un rol sin bypass no falla, afecta 0 filas y dejaría todo sin origen. Se verifica
   por tenant que no quede ninguna fila sin origen, y `SET NOT NULL` aborta la migración si algo escapó.
6. **Carga manual = app de la plataforma.** `POST /api/v1/admin/meta/manual` valida el token con la app de la
   plataforma (si no está configurada, 400 `platform_app_not_configured`) y guarda `meta_app_id` de la
   plataforma, porque una conexión `manual` se resuelve siempre con ella. Enmienda la corrección de ADR-0034,
   que validaba con la app del tenant si tenía una: ese token pasaba la validación y fallaba en cada llamada
   posterior por `appsecret_proof`. Un negocio con su propia app de Meta conecta por el wizard BYOK.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Seguir infiriendo el origen por los tokens que existen | Es el origen del defecto: una conexión tomada por ES o con un token revocado se clasifica mal, y cada servicio lo reimplementa |
| `origin` como hecho histórico (no cambia) | Una conexión BYOK tomada por ES usaría el token BYOK viejo en todo el sistema; habría que guardar aparte el modo vigente y volveríamos a dos fuentes de verdad |
| `origin` con default `manual` | Un alta nueva que lo olvide usaría en silencio la app de la plataforma; ya ocultaba datos falsos en los fixtures |
| Un `kind` de token único para BYOK y ES | Cambia el CHECK de `meta_tokens`, exige re-cifrar filas existentes (el `kind` entra en el AAD) y no resuelve de dónde sale el secreto |
| Que cada servicio reciba un `app_secret` y lo busque el llamador | Mantiene N lugares donde elegir mal el secreto; no hay un punto donde auditar el aislamiento |
| Backfill con un único `UPDATE` global | Silenciosamente no actualiza nada si el rol de migraciones no tiene `BYPASSRLS` |

## Consecuencias

**Positivas:** BYOK funciona en Números, Plantillas, salud, costos, créditos y token; el sender, la salud y los
avisos de vencimiento hablan del mismo token; hay un solo punto donde se decide de dónde sale cada credencial y
donde se prueba el aislamiento entre negocios; los crons procesan las conexiones (antes no procesaban ninguna).

**Negativas / lo que aceptamos pagar:**
- Los crons de plantillas (cada hora) y de precios (cada día) ahora llaman de verdad a Graph para las
  conexiones de Embedded Signup y BYOK: más tráfico a Meta que antes, que era cero.
- La verificación de salud ahora corre completa para las conexiones BYOK (antes devolvía solo dos checks): una
  conexión con el token vencido pasará a `degraded`/`error` en el próximo ciclo. Es lo correcto, pero visible.
- Una conexión tomada por ES (o con los dos tokens tras el backfill) que se vuelva a tomar envía con el token
  del nuevo origen desde ese momento: si ese token no sirve, el envío falla en vez de caer al otro.
- `POST /api/v1/admin/meta/manual` deja de aceptar tokens de la app propia del negocio.
- Toda creación de `MetaConnection` (código y fixtures de tests) debe fijar `origin`.

**Qué haría que revisáramos esto:** un cuarto tipo de alta (p. ej. conexiones de Instagram/Messenger con su
propia app) o credenciales por conexión que roten por otro mecanismo que el token store.

## Fuera de alcance (queda anotado en `docs/BACKLOG.md`)

- Suscripciones y webhook (`meta_subscriptions*.py`, `resolve_app_credentials`, `resolve_effective_app_secret`
  en el webhook) siguen eligiendo "la" conexión del tenant: es HU-CONN-05 (webhook y secreto por conexión),
  junto con alinear `meta_app_id` tras una toma por ES.
- Descarga de media: el core no la hace hoy (`url: ""` en `inbound.py`); HU-MEDIA-02 deberá usar `credentials_for`.
- `meta_private.meta_tokens` tiene RLS habilitada pero no forzada (la migración `0029` solo cubrió `public`).

## Impacto en el código

- `alembic/versions/0051_meta_connections_origin.py`, `core/db/models.py` (`MetaConnection.origin`).
- `core/gateway/meta_credentials.py` (nuevo); altas: `core/gateway/meta_byok.py`, `core/gateway/meta_onboarding.py`,
  `core/api/routers/meta_wizard.py`.
- Consumidores: `core/gateway/sender.py`, `core/gateway/meta_health.py`, `core/gateway/meta_template_sync.py`,
  `core/admin/meta_numbers_service.py`, `meta_templates_service.py`, `meta_health_service.py`,
  `meta_costs_service.py`, `core/admin/views.py`, `core/api/routers/meta_ops.py`,
  `core/api/routers/connections.py`, `core/tasks/message_template_sync.py`, `core/tasks/meta_pricing_sync.py`,
  `core/tasks/meta_token_expiry_check.py`.
- Tests: `tests/test_meta_credentials.py`, `tests/integration/test_meta_credentials_origin.py`, y `origin`
  explícito en los fixtures que crean `MetaConnection`.

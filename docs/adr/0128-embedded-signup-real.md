# ADR-0128 — Embedded Signup real: canje en el servidor atado a un intento, cadena con el PIN del negocio y la app de la plataforma solo del master

- **Estado:** aceptado
- **Fecha:** 2026-10-09
- **Implementa:** ADR-0080 (el wizard de WhatsApp en el panel nuevo), parte de Embedded Signup.
- **Supersede/enmienda:**
  - enmienda ADR-0094 §1 ("el wizard de ES reutiliza una conexión viva y la toma"): ya no toma una conexión BYOK ni
    una pausada (§6);
  - enmienda ADR-0102 §5: el cron de suscripciones lee también la app de la **plataforma**, con las credenciales del
    entorno y solo lectura; y `next_status` no deja mejorar una conexión de Embedded Signup sin la suscripción de esa
    app confirmada (§5);
  - enmienda ADR-0126, consecuencia "el alta de Embedded Signup sigue re-registrando la suscripción de la app de la
    plataforma": ya no (§4);
  - extiende ADR-0127 §2 a Embedded Signup: PIN obligatorio en producción y `pending` → `connected` al registrar (§3).
- **Contexto:** HU-SH-05 (issue agent-core #340), SPEC-07 · Hito 0A (D-30). Cierra en código el criterio de Embedded
  Signup de HU-CONN-01. Revisado por `arquitecto`, `auditor-multitenant` y `security-reviewer` (2026-10-09); sus
  hallazgos están corregidos (ver "Revisión").

## Contexto

1. El wizard de Embedded Signup existía en el backend (`meta_onboarding`, máquina de estados en `connection_wizards`)
   pero el panel nuevo no abría el popup: el botón creaba una sesión y decía "carga el SDK".
2. El registro del número generaba un PIN que nadie veía: el número real quedaba con una verificación en dos pasos
   que el negocio no conocía (el mismo problema que ADR-0127 resolvió para BYOK de producción).
3. Cada alta re-registraba la suscripción de webhook de la app de la plataforma, compartida por todos los negocios,
   desde el flujo de un negocio (seguimiento de ADR-0126).
4. El último paso marcaba la conexión `connected` mirando solo esa suscripción, sin verificar el token.
5. El wizard reutilizaba cualquier conexión viva del entorno, también una BYOK (desde ADR-0127 las hay en producción)
   o una pausada.

## Decisión

1. **Popup (panel).** SDK de Facebook (`connect.facebook.net/en_US/sdk.js`, `FB.init` con el App ID y la versión de
   Graph del servidor), `FB.login` con el `config_id` de la plataforma, `response_type: "code"`,
   `override_default_response_type: true` y `extras: { setup: {} }`. Es **Embedded Signup v4**, verificado contra la
   documentación de Meta el 2026-10-09: ya no lleva `sessionInfoVersion` ni `featureType`. El evento
   `WA_EMBEDDED_SIGNUP` llega por `postMessage`:
   - `FINISH` (Cloud API), `FINISH_ONLY_WABA` (sin número) o `FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING`
     (coexistencia), con `data.phone_number_id`, `data.waba_id` y `data.business_id`;
   - `CANCEL` con `data.current_step` (abandono) o con `error_message`, `error_code`, `session_id` (error
     reportado). El panel trata igual un `ERROR` de versiones anteriores.

   El listener solo acepta orígenes `https://facebook.com` o subdominios (comparando el hostname: `endsWith` del
   texto aceptaría `evilfacebook.com`). El `code` (TTL de 30 s) va al servidor en cuanto llega, junto con el evento
   si llegó (se espera hasta 3 s). La CSP del panel abre `connect.facebook.net` y `*.facebook.com` solo en
   `/conexiones/whatsapp`.
2. **API** (`core/api/routers/meta_wizard.py`):
   - `GET /wizard`: si la app de la plataforma está configurada (`missing_settings` nombra las variables que faltan)
     y App ID, `config_id` y versión de Graph (públicos). El App Secret nunca sale del servidor.
   - `GET /wizard/session`: el **estado** sin efectos (el alta viva o la última terminada: paso, error, cancelación,
     vencimiento, estado de la conexión).
   - `POST /wizard/session`: arranca o reanuda (`restart` empieza de nuevo sobre la misma conexión) y, si el alta
     espera el popup, da un **intento** aleatorio. Se guarda solo su hash, con el admin que lo pidió y la hora.
   - `POST /wizard/cancel`: registra el `CANCEL` (textos de Meta recortados y sin caracteres de control: son dato) y
     anula el intento. El alta sigue viva en el mismo paso para reintentar.
   - `POST /wizard/exchange`: consume el intento con la fila bloqueada (`consume_attempt`: mismo admin, menos de
     15 min, alta sin vencer; un segundo canje recibe 409) y canjea el `code` en el servidor
     (`GET /oauth/access_token` con `client_id`, `client_secret` y `code`, sin `redirect_uri`). Después **encadena**
     el alta (`complete_signup`).
   - `POST /wizard/resume`, `/wizard/number` (PIN) y `/wizard/pin` (fijar un PIN nuevo con `POST /{phone_number_id}`
     y registrar con él; uno por minuto) retoman desde el paso guardado.
   - En producción, todas las que cambian el alta (también cancelar): propietario del negocio o master, como BYOK de
     producción (ADR-0127).

   En el flujo de popup no hay `redirect_uri` ni `state` de OAuth: el intento cumple ese papel (CSRF, pestañas
   viejas, otro admin del negocio).
3. **Canje seguro y cadena.** Antes de llamar a Meta se rechaza reautorizar una conexión con otra WABA u otro número
   (`different_waba`, `different_number`): quedaría con dos números vivos y el sender respondería por cualquiera
   (ADR-0127, alternativas). Después del canje, primero se verifica con el token **en memoria** que vea la WABA y el
   número y que no sean de otro negocio (ADR-0095); recién entonces se guarda el token (con el sujeto que ya tenía
   la conexión) y el portafolio (`business_id`, solo si la conexión no tenía). La coexistencia la decide Meta
   (`is_on_biz_app` del número), no el evento del popup. La cadena: datos de la WABA y del número, suscribir la WABA
   a la app de la plataforma (`POST /{waba_id}/subscribed_apps`), registrar el número con el PIN
   (`POST /{phone_number_id}/register`), sincronizar plantillas y verificar salud. Un canje rechazado da 400 (repetir
   el popup); un paso posterior que falla da 200 con el paso y el motivo. La cadena tiene un tope de 45 s en el
   request (como "Activar", ADR-0122 §4): pasado el tope no arranca otro paso y el panel ofrece "Retomar".
4. **PIN del negocio.** El PIN lo escribe el negocio en el wizard (el que ya tenga en verificación en dos pasos o uno
   nuevo) y el registro pasa por el mismo servicio que "Números" (`meta_numbers_service.register_number`: cifrado
   antes de llamar a Meta, restaurado si Meta lo rechaza, un intento por minuto). Mientras falta, la conexión queda
   `pending` con `MOTIVO_NUMERO_SIN_REGISTRAR` y registrarlo por cualquiera de los dos caminos la deja `connected`
   (ADR-0127 §2). El wizard pide el PIN en cualquier entorno (Embedded Signup es de producción); "Números" lo exige
   en producción para BYOK y Embedded Signup. Las constantes del PIN viven en `meta_phone_numbers` para que el
   gateway y el servicio no se importen entre sí.
5. **La app de la plataforma.**
   - El alta de un negocio **no** re-registra la suscripción de la app (URL genérica y token global): es compartida
     y la registra el master ("Registrar webhook", ADR-0126 §5).
   - `refrescar_app_de_la_plataforma` la **lee** (`GET /{app_id}/subscriptions`, solo lectura) con
     `META_APP_ID`/`META_APP_SECRET` del entorno: en el cron de 15 min y en el último paso del alta. Guarda el estado
     en `meta_app_subscriptions` y, si Meta no la tiene activa, manda **una** alerta de plataforma. El reparo de
     ADR-0102 §5 era usar un secreto guardado por un negocio que dice ser de la plataforma; este es el del entorno.
   - `check_app_subscription` mira la app que de verdad usa la conexión: con Embedded Signup y manual, la de la
     plataforma (antes, el `meta_app_id` de la fila).
   - `next_status`: una conexión de Embedded Signup solo mejora con el token verificado **y** la suscripción de la app
     de la plataforma confirmada (`None` no alcanza).
   - El último paso corre el diagnóstico de siempre (`diagnose_connection`). Si deja la conexión `pending` o `error`,
     el alta termina igual: el hub muestra el estado y el motivo, y "Diagnosticar" lo retoma.
6. **Qué conexión reutiliza.** Reautoriza la `connected` del entorno o reutiliza el borrador (pasadas 24 h el alta se
   cierra como `wizard_expired` y se empieza otra sobre la misma conexión; dos pedidos simultáneos ya no dan 500).
   No reutiliza (409):
   - una conexión **BYOK** (`byok_connection_exists`): tomarla dejaba su app suscrita a la WABA, su secreto aceptado
     por el webhook y `meta_app_id` desalineado (ADR-0126 §8). El negocio la elimina y conecta con Facebook;
   - una conexión **pausada** (`connection_paused`): el canje la reactivaba sin el diagnóstico de "Activar"
     (ADR-0122).
7. **Final.** El panel lleva al hub de la conexión (`/conexiones/meta/{id}`) con "Enviar mensaje de prueba":
   `GET|POST /api/v1/admin/meta/connections/{id}/test-message` manda una plantilla **aprobada y sin variables**
   (`hello_world` si está) con las credenciales de la conexión; solo `connected` o `degraded`; uno cada 30 s y 10 al
   día por conexión; en producción, propietario o master; queda en la auditoría (`meta.test_message`, con los
   últimos 4 dígitos del destino).
8. **Requisitos de Meta de la plataforma** ("Plataforma → Embedded Signup", solo master):
   `GET /api/v1/admin/platform/meta-app/embedded-signup` y `POST .../step`. Manuales, confirmados por el master con
   quién y cuándo: verificación del negocio, acceso de Tech Provider, App Review de `whatsapp_business_management` y
   `whatsapp_business_messaging`, la configuración de Embedded Signup del `config_id`, el dominio del panel en el SDK
   de JavaScript y la app en modo Live. Más tres datos del servidor (variables de entorno, URL pública en HTTPS y el
   webhook de la app). Tabla de **sistema** `platform_meta_app_requirements` (migración 0066): es de la app de la
   plataforma, no de un negocio; solo la escribe `core/admin/platform_meta_es.py`, que exige master (un test lo
   hace cumplir), y `app_runtime` no tiene `DELETE`. Los chequeos de infraestructura de la app son de HU-CONN-07
   (`/admin/platform/meta-app`, ya en `develop`); esta lista es la parte de Embedded Signup y se enlaza desde allí.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Flujo con redirect y `state` de OAuth | Meta documenta Embedded Signup con popup y `postMessage`; el redirect obliga a registrar URLs por entorno y no da los IDs del evento. El intento cumple el papel del `state` |
| Correr la cadena en Celery | Sin segundo caso de uso (mismo criterio que ADR-0122 §4). Los pasos son reanudables y el request tiene tope; si con Meta real la latencia molesta, el estado ya está en `connection_wizards` |
| Seguir generando el PIN | El número real quedaba con una verificación en dos pasos que el negocio no conoce (ADR-0127) |
| Seguir tomando la conexión BYOK y alinear `meta_app_id` | Hay que mover o dejar de aceptar el secreto viejo y desuscribir su app con su token (ADR-0126 §8). Rechazar la toma es más simple y no deja conexiones con dos apps |
| Esperar el checklist de HU-CONN-07 para los requisitos manuales | #354 guarda corridas automáticas, no pasos manuales confirmados por el master |
| Un ADR-0080 enmendado | Las decisiones cambian ADRs más recientes (0094, 0102, 0126, 0127): van en un ADR nuevo que lo declare |

## Consecuencias

**Positivas:**
- Un negocio conecta su WhatsApp solo, con el popup de Meta, y llega al hub con un mensaje de prueba.
- Un canje que no verifica no reemplaza el token de una conexión que funciona; nadie canjea con un intento ajeno,
  viejo o repetido.
- La app compartida no se toca desde el flujo de un negocio y su suscripción se lee cada 15 min: una caída da una
  alerta de plataforma.

**Negativas / lo que aceptamos pagar:**
- Pasar de BYOK a Embedded Signup exige eliminar la conexión BYOK antes (sus conversaciones quedan en pausa,
  ADR-0122). Conectar otro número exige eliminar la conexión.
- La verificación contra Meta real depende del dueño (app de la plataforma aprobada y configurada en test); los
  tests usan un Graph falso.
- La migración 0066 cuelga de 0063 (la última de la base apilada): al mergear después de 0064 (#354, ya en
  `develop`) y 0065 (#357) hay que re-encadenar su `down_revision`.

**Qué haría que revisáramos esto:** que Meta cambie la forma del evento o del canje (otra versión de Embedded
Signup), que se necesiten varios números por conexión, o que la latencia de la cadena con Meta real pida un job.

## Revisión (2026-10-09)

Corregido tras `security-reviewer`, `auditor-multitenant` y `arquitecto`: token guardado después de verificar;
portafolio y coexistencia después del canje (coexistencia por `is_on_biz_app`); intento consumido con la fila
bloqueada, atado al admin y con vencimiento; cancelar con la guarda de producción; sin toma de BYOK ni de pausadas;
sin segundo número; consultas de los pasos con `tenant_id`, `deleted_at` y orden; refresco de la app de la
plataforma; tope de la cadena; tope diario, auditoría y dueño en producción para el mensaje de prueba; cualquier
error de transporte del canje como `GraphError` (sin frames con secretos) y más claves en el scrubber de Sentry;
logs de `debug_token` solo con la forma; IDs del popup solo numéricos; este ADR en vez de enmendar ADR-0080.

## Impacto en el código

- `core/gateway/meta_onboarding.py`, `core/api/routers/meta_wizard.py`.
- `core/gateway/meta_phone_numbers.py` (`PIN_KIND`, códigos y `generate_pin`), `core/admin/meta_numbers_service.py`
  (PIN obligatorio en producción también para ES, `release_register_rate_limit`).
- `core/gateway/meta_health.py` (`check_app_subscription`, `next_status`),
  `core/tasks/meta_subscriptions_health.py` (`refrescar_app_de_la_plataforma`).
- Nuevos: `core/admin/meta_mensaje_prueba.py`, `core/api/routers/meta_mensaje_prueba.py`,
  `core/admin/platform_meta_es.py`, `core/api/routers/platform_meta_es.py`,
  `alembic/versions/0066_requisitos_app_meta_plataforma.py`.
- `core/observability/sentry_setup.py` (claves del canje en el scrubber).
- Tests: `tests/integration/test_embedded_signup.py` (`aislamiento`), `tests/test_meta_onboarding_register_number.py`,
  `tests/test_platform_meta_app_requirements_solo_desde_su_modulo.py`.

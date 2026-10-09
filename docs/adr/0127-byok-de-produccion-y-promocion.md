# ADR-0127 — BYOK de producción: entorno elegido, registro con PIN y promoción con el alta de siempre

- **Estado:** aceptado
- **Fecha:** 2026-10-09
- **Supersede/enmienda:**
  - enmienda ADR-0094 §1: el wizard BYOK rechaza el `META_APP_ID` de la plataforma (seguimiento de ADR-0126 §7).
    La guarda de `uses_platform_app` por `meta_app_id` se queda para las filas viejas y ahora usa la misma función
    (`meta_byok.es_app_de_la_plataforma`);
  - enmienda ADR-0126 (consecuencia "a una BYOK sin `verify_token` propio se le registra el global"): cada alta
    BYOK guarda un `verify_token` propio, el que escribe el admin o uno aleatorio;
  - enmienda ADR-0102 (mejorar solo con diagnóstico verificado): una conexión `pending` que solo esperaba el
    registro del número pasa a `connected` cuando Meta acepta el registro.
- **Contexto:** HU-CONN-06 (issue agent-core #338), SPEC-08 · Hito 0A (D-30). Depende de HU-CONN-01 (ADR-0094) y
  HU-CONN-05 (ADR-0126). Revisado por `arquitecto`, `auditor-multitenant` y `security-reviewer` (2026-10-09); la
  primera versión promovía en la misma conexión y el arquitecto la rechazó (ver alternativas).

## Contexto

1. **BYOK siempre en sandbox.** `save_byok_connection` aceptaba `environment`, pero el wizard de la API no lo
   mandaba y el valor por defecto era `sandbox` (`meta_byok.py:97`, `meta_wizard.py:164-170`). No había camino de
   producción que no dependiera de que Meta apruebe a SolversHub como Tech Provider (R-01).
2. **Número registrado a ciegas.** El alta marcaba `phone_numbers.registered = True` siempre: vale para el número
   de prueba de Meta (viene registrado), no para un número real recién agregado a la WABA.
3. **Sin promoción.** Pasar de prueba a producción era borrar la conexión y empezar otra a mano.
4. **El checklist de producción miraba la app de la plataforma** (`check_app_subscriptions_complete
   (settings.meta_app_id)`), no la del negocio.

## Decisión

1. **Entorno elegido en el wizard.** `POST /api/v1/admin/meta/wizard/byok` recibe `environment`
   (`sandbox` | `production`, por defecto `sandbox`, lo de antes) y lo guarda en la conexión.
   - En producción se rechaza el número de prueba de Meta (`account_mode = SANDBOX`, que `GET /{phone_number_id}`
     ya informa) y el alta exige ser propietario del negocio o master (`deps.require_owner_or_master`, como
     registrar el webhook): atiende a clientes reales.
   - En cualquier entorno se rechaza el App ID de la plataforma, y App ID, WABA ID y Phone Number ID solo
     aceptan `[A-Za-z0-9_-]` (van en la ruta de Graph).
2. **Registro con PIN.** El alta pide a Meta `platform_type`, `account_mode` y `status` del número (la misma
   llamada que ya verificaba las credenciales) y los guarda en `phone_numbers.raw`. Registrado es
   `platform_type = CLOUD_API`; sin el campo se mira `status = CONNECTED`, y sin ninguno de los dos, registrado
   (respuestas viejas: si fuera falso, `register_number` es idempotente con "already registered"). Si no está
   registrado:
   - la conexión termina `pending` con la razón fija `MOTIVO_NUMERO_SIN_REGISTRAR`
     (`meta_connection_state.esperando_registro`), con credenciales verificadas y la WABA suscrita. **El texto de
     esa razón es un contrato con las filas guardadas**: cambiarlo exige actualizar las `pending` existentes (un
     test lo fija);
   - el PIN va en el mismo alta (`pin`, opcional) o después en la ruta de siempre
     `POST /connections/{id}/numbers/{phone_number_id}/register`. En una BYOK de producción el PIN es obligatorio
     si no hay uno guardado: un PIN generado que nadie ve dejaría al número real con una verificación en dos
     pasos que el negocio no conoce;
   - cuando Meta acepta el registro (o dice que ya estaba registrado) y no quedan números vivos sin registrar, la
     conexión pasa a `connected` (`_conectar_si_solo_faltaba_el_registro`, con `FOR UPDATE`). El alta ya verificó
     token y suscripción; el registro era lo que faltaba;
   - "Diagnosticar" no la mejora (el check del número falla por definición), pero sí la empeora: sin ese check, si
     `next_status` da `error` (un token muerto), queda `error`. El cron de salud no mira `pending`.
   - `_phone_of_connection` ignora los números dados de baja: uno que dejó la conexión pudo tomarlo otro negocio.
3. **Pasar a producción con el alta de siempre** (`core/gateway/meta_byok_promocion.py`, rutas
   `/api/v1/admin/meta/connections/{id}/promotion[/number|/verify|/finish]`). Solo conexiones de WhatsApp
   `origin = byok` con app propia, en `sandbox`, vivas, no `revoked` ni `draft`. Pasos:
   1. **Número real** (`cargar_numero_real`): WABA, número (solo dígitos) y, opcional, el token permanente. Llama
      a `save_byok_connection(environment="production")` con el App ID, el App Secret, el token (si no viene uno
      nuevo) y el `verify_token` guardados de la conexión de prueba: el secreto no sale del servidor y la
      suscripción de la app no cambia. Crea la conexión de producción de esa app o, si ya existe, la reusa (el
      índice único `uq_meta_connections_tenant_app` incluye el entorno: hay a lo sumo una). **La de prueba no se
      toca**: sus testers siguen funcionando;
   2. **Registro** con el PIN en la conexión de producción, la misma ruta del punto 2;
   3. **Plantillas**: al menos una `APPROVED` en la WABA del número real;
   4. **Verificar y terminar** (`terminar_promocion`): corre `checks_de_su_app` sobre la de producción y, si pasan
      (`puede_terminar`), da de baja la de prueba con `soft_delete_connection` (ADR-0122: tokens revocados,
      números y WABAs con `deleted_at`, razón "Reemplazada por la conexión de producción de la misma app"). No
      cambia el estado de la de producción (ADR-0102). Con la fila de producción bloqueada se revisa que su
      estado, números y WABAs sean los verificados; si cambiaron, `connection_changed`.

   **Las conversaciones de los testers se quedan con la conexión de prueba**, que queda eliminada y por eso en
   pausa: nunca se responden desde el número real (ADR-0122 §3). La conversación no guarda el número, y el sender
   toma el primer número vivo de su conexión.

   El estado (`GET .../promotion`) se calcula con lo guardado, sin llamar a Meta: la "pareja" de producción es la
   conexión viva de producción con la misma app. Leer el estado pide la sección `connections`; cargar el número y
   terminar, además, propietario o master. "Verificar" y "Terminar" se limitan a uno cada 10 s por conexión
   (varias llamadas a Graph con la cuota del negocio) y responden 404 antes del límite si la conexión es ajena.
4. **Chequeos con la app del negocio** (`checks_de_su_app`). Todo sale de `credentials_for` (ADR-0094): el token,
   el App ID y el App Secret de la conexión. Revisa que la app sea propia (`own_app`), el token con `debug_token`
   de **su** app, la suscripción guardada de **su** app al objeto `whatsapp_business_account`, cada número
   (`account_mode = LIVE` y registrado), que la WABA esté suscrita a **su** app (no basta "alguna app", ADR-0102)
   y las plantillas aprobadas. Nada usa `META_APP_ID`/`META_APP_SECRET`. Para terminar, ninguno puede dar `False`
   y `own_app`, `token_valid`, `real_number`, `number_registered` y `waba_subscribed_to_own_app` tienen que dar
   `True` (un `None` porque Meta no respondió no alcanza). `/promotion/verify` también acepta una BYOK creada
   directo en producción: es su propia pareja (criterio 4 de la HU).
5. **`verify_token` propio en cada alta BYOK.** Si el admin no escribe uno, se genera con `secrets.token_urlsafe`
   y se guarda (`webhook_verify_token`). Es el que se registra en la app del negocio y el que acepta el handshake
   de esa conexión (ADR-0126 §4). El admin no lo necesita: la suscripción se registra por API.

No hay migración: `environment`, `phone_numbers.raw`/`registered`/`platform_type` y los estados ya existen.

**Cuando entre HU-CONN-07 (#354)**, su checklist por conexión pasa a ser la fuente: `terminar_promocion` debe
consumirlo (sumándole `own_app` y "WABA suscrita a su app" si no los tiene) y `checks_de_su_app` se borra. Queda
en el BACKLOG.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Promover en la misma conexión: cambiar número y WABA y al final `environment = production` (primera versión) | Las conversaciones de los testers quedaban atadas al número real (el sender toma el primer número vivo de la conexión): avisos y seguimientos salían como plantillas cobradas a contactos que nunca le escribieron. Además repetía el alta BYOK sin su rollback. `environment` no cambia ningún comportamiento (es etiqueta y llave del índice), así que conservar el id no compraba nada |
| Agregar el número real a la conexión de prueba y quitar el de prueba al terminar | Con dos números vivos el sender envía por cualquiera de los dos |
| Guardar el avance de la promoción en una tabla o en `connection_wizards` | El estado sale de datos que ya existen (la pareja de producción, su número, su registro, sus plantillas); una tabla duplicaría la verdad. `connection_wizards` es la máquina de Embedded Signup y `start_or_resume_wizard` la retomaría |
| Deducir "registrado" de `status = CONNECTED` | `status` mezcla el registro con la salud del número (`FLAGGED`, `RATE_LIMITED` son números registrados); `platform_type` es el dato del registro |
| Pasar a `connected` con un diagnóstico completo después del PIN | Varias idas a Meta en el request del PIN; el alta ya verificó lo demás. El cron de salud la revisa cada hora |
| Congelar el estado de la conexión `pending` frente a "Diagnosticar" | Empeorar siempre se puede (ADR-0102): un token muerto tiene que verse como `error` |

## Consecuencias

**Positivas:**
- Hay un camino de producción que no depende de que Meta apruebe a SolversHub como Tech Provider.
- Un número real sin registrar ya no aparece como registrado; la conexión dice qué falta.
- La promoción usa el alta BYOK de siempre (un solo camino que mantener) y no rompe la conexión de prueba hasta
  terminar; nada se responde desde el número real a los testers.
- Ninguna BYOK nueva comparte el `verify_token` global.

**Negativas / lo que aceptamos pagar:**
- Al terminar, la conexión de producción tiene otro id que la de prueba: los enlaces del panel a la de prueba dan
  404 (el panel lleva a la de producción) y las conversaciones de los testers quedan en pausa.
- La pantalla de estado no llama a Meta: muestra el último dato guardado. "Verificar" y "Terminar" sí consultan.
- La guarda de `uses_platform_app` por `meta_app_id` sigue: puede haber filas viejas con el App ID de la
  plataforma. Sale cuando la consulta previa al despliegue de ADR-0126 confirme que no hay.
- Hasta que entre #354 conviven dos listas de chequeos por conexión (esta y la de HU-CONN-07).
- El alta BYOK de producción contra Meta real (app en modo Live, System User, PIN) la documenta el dueño en test;
  los tests usan un Graph falso.

**Qué haría que revisáramos esto:** que Meta cambie `account_mode`/`platform_type`, que se necesiten varios
números por conexión de producción o que HU-CONN-08 (hub) reorganice las acciones por conexión.

## Impacto en el código

- `core/gateway/meta_byok.py`: `environment` validado, rechazo del App ID de la plataforma y del número de prueba
  en producción, `numero_registrado`, `pending` con `MOTIVO_NUMERO_SIN_REGISTRAR`, `verify_token` propio.
- `core/gateway/connections.py::verify_meta_credentials`: pide `platform_type`, `account_mode` y `status`.
- `core/gateway/meta_connection_state.py`: `MOTIVO_NUMERO_SIN_REGISTRAR`, `esperando_registro`.
- `core/gateway/meta_credentials.py::uses_platform_app`: usa `es_app_de_la_plataforma`.
- `core/admin/meta_numbers_service.py`: PIN obligatorio en BYOK de producción, `pending` → `connected` al registrar,
  números dados de baja fuera.
- `core/admin/meta_health_service.py`: "Diagnosticar" sobre una conexión que espera el registro; `origin` en la salud.
- `core/api/deps.py`: `es_owner_o_master` y `require_owner_or_master` (antes privados de `meta_ops`).
- `core/gateway/meta_byok_promocion.py` (nuevo) y `core/api/routers/meta_promocion.py` (nuevo).
- `core/api/routers/meta_wizard.py`: `environment`, `pin`, `WizardByokOut` y owner/master en producción.
- Tests: `tests/test_meta_byok.py`, `tests/test_meta_byok_promocion.py` (nuevo),
  `tests/test_meta_numbers_register.py` y `tests/integration/test_byok_produccion.py` (nuevo, `aislamiento`).

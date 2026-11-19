---
name: meta-whatsapp
description: >
  Conocimiento vigente de WhatsApp Business Platform / Meta para el motor: Embedded Signup
  (Tech Provider, config_id), plantillas y categorías, ventana de 24 h, precios por mensaje
  desde 2026, firma del webhook, BYOK y número. Úsala al implementar HU-CONN-*, HU-SH-05 y
  HU-ARCH-13 antes de tocar el gateway o el wizard de conexiones. Cada dato lleva fecha y fuente.
---

Datos verificados el **2026-10-02** (Embedded Signup, el **2026-10-09**) (versión de Graph en el repo: `v21.0`,
`META_GRAPH_VERSION`). Re-verificar antes de una release; Meta cambia precios hasta cada
trimestre.

## Ventana y categorías (lo que define si se cobra)

- Solo se envía un **mensaje no-plantilla** dentro de la **ventana de servicio de 24 h**
  abierta por el último mensaje del usuario; se reinicia con cada mensaje entrante.
- **Desde 2026-10-01 Meta cobra los mensajes de servicio** (no-plantilla dentro de la
  ventana), a la misma tarifa que utility/authentication por mercado. Hubo **cambio**: los
  mensajes de servicio eran gratis desde 2024-11-01.
- **Nuevo desde 2026-10-01:** cada número recibe **1.000 mensajes de servicio gratis al
  mes**; Meta cobra desde el 1.001. No se acumulan y se reinician por número cada mes.
- **Utility en respuesta a un usuario (dentro de la ventana) también se cobra desde
  2026-10-01** (antes gratis desde 2025-07-01).
- **Agosto 2026:** los mensajes de *Meta Business Agent* se cobran por token, una sola
  tarifa global de USD 2,00 por millón de tokens (≈20.000–25.000 tokens por mensaje,
  ≈USD 0,04–0,05). No es el caso de SolversHub: el negocio usa su propia IA.
- Solo se cobra por **mensaje entregado** (`pricing.type = "regular"` en el webhook de
  estado; `pricing.category` = `service`, `utility`, `marketing`…).
- Un mensaje tiene **una sola categoría** y se cobra una sola vez. Las entrantes son gratis.
- Fuentes: `developers.facebook.com/documentation/business-messaging/whatsapp/pricing`
  (verificado 2026-10-02).

**Consecuencia para el motor (N-02):** partir cada respuesta en varias burbujas multiplica
los cobros por turno. Por defecto, **una burbuja por respuesta** (HU-ARCH-11).

## Ventana de 24 h y plantillas

- Fuera de la ventana **solo se puede escribir con plantilla aprobada** (mensaje
  proactivo/recordatorio). El motor hoy deja esos envíos en `failed` (HU-ARCH-13).
- Las plantillas las aprueba Meta **por WABA**, así que son **por negocio**, no de
  plataforma (decisión D-SPEC-02-2: config por negocio, evento → plantilla + variables).
- Categorías de plantilla: `marketing`, `utility`, `authentication`; se cobran por
  entregada y varían por mercado y categoría.

## Webhook (firma)

- Meta firma cada POST con `X-Hub-Signature-256` = `sha256` con HMAC usando el
  **App Secret** del negocio (`hmac.new(app_secret, raw_body, sha256).hexdigest()`);
  comparar en tiempo constante contra `"sha256=" + firma`.
- **Fail-closed** (HU-ARCH-05): sin firma válida no se procesa. El body crudo se firma,
  no el JSON re-serializado.
- Verificación del webhook (`GET` con `hub.mode=subscribe`, `hub.verify_token`,
  `hub.challenge`): responder el `hub.challenge` si el token coincide.

## Embedded Signup (Tech Provider)

- El negocio necesita ser **Tech Provider** aprobado por Meta (App Review) y un
  **`config_id`** del flujo de Embedded Signup. Sin eso, no hay alta automática.
- Flujo: abrir el popup de Embedded Signup con el `config_id` → el usuario autoriza →
  llega un `code` → **intercambiarlo en el servidor** por un token de negocio
  (`/oauth/access_token`); nunca confiar en tokens que llegan del frontend.
- Guardar `meta_app_id`, `meta_config_id`, WABA id y número; el resolver de credenciales
  por conexión decide qué token usar (HU-CONN-01/05).
- **Embedded Signup v4** (verificado 2026-10-09 en
  `developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation`):
  `FB.login(cb, { config_id, response_type: "code", override_default_response_type: true,
  extras: { setup: {} } })`. **Ya no lleva `sessionInfoVersion` ni `featureType`** (eran de v2/v3).
  El evento llega por `postMessage` desde `facebook.com` con `type: "WA_EMBEDDED_SIGNUP"`:
  `event` = `FINISH` (Cloud API), `FINISH_ONLY_WABA`, `FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING`
  (coexistencia), `FINISH_OBO_MIGRATION`, `FINISH_GRANT_ONLY_API_ACCESS`; `data` trae
  `phone_number_id`, `waba_id`, `business_id`. Abandono: `CANCEL` con `data.current_step`; error
  reportado: `CANCEL` con `error_message`, `error_code`, `session_id`, `timestamp`.
- El `code` vive **30 s**. Canje: `GET /oauth/access_token?client_id&client_secret&code` (sin
  `redirect_uri`). Después: `POST /{waba_id}/subscribed_apps` con ese token, `POST
  /{phone_number_id}/register` con `messaging_product` y `pin` de 6 dígitos, y el negocio agrega su
  método de pago en WhatsApp Manager (fuente: "Onboarding business customers as a Tech Provider").
- Estado en el motor: conectado en el panel nuevo (HU-SH-05, ADR-0128). El PIN lo
  escribe el negocio; el canje exige el intento de `POST /wizard/session`; la app de la plataforma
  solo la registra el master. Falta la verificación con la app de la plataforma aprobada (dueño).
- BYOK ya no queda fijado en sandbox: el wizard elige entorno y una BYOK de prueba se pasa a
  producción (HU-CONN-06, ADR-0127).

## BYOK y ciclo de vida del número

- BYOK: el negocio trae app id/secret y token propios; registrar el número y resolver
  webhook y secreto **por conexión** (dos negocios no comparten credenciales).
- BYOK de producción (ADR-0127): `environment` lo elige el wizard; en producción se rechaza el
  número de prueba (`account_mode = SANDBOX`) y el App ID de la plataforma. Registrado es
  `platform_type = CLOUD_API`; si no, la conexión queda `pending` con
  `MOTIVO_NUMERO_SIN_REGISTRAR` hasta el PIN (`register_number`). "Pasar a producción"
  (`meta_byok_promocion`) crea la conexión de producción de la misma app con
  `save_byok_connection` y, con los chequeos de la app del negocio en verde
  (`checks_de_produccion`: el checklist por conexión de HU-CONN-07 más `own_app` y `real_number`),
  da de baja la de prueba: sus conversaciones nunca pasan al número real.
- Un número que ya está en otra WABA no se puede registrar; manejar el error explícito
  (el repo ya tiene `test_meta_byok_numero_ocupado.py`).
- Baja/baja lógica: nunca borrar en duro números, WABA ni costos (invariante 8, N-19).

## Códigos de error frecuentes (Graph)

- `130429` — rate limit de la Cloud API.
- `131047` — fuera de la ventana de 24 h: **requiere plantilla**.
- `131026` — el mensaje no se pudo entregar al usuario.
- `190` — token inválido/expirado; `100` — parámetro inválido.
- `368` / `131031` — cuenta o WABA restringida/bloqueada.

## Costos (transparencia, D-06)

- Meta factura **directo al negocio**; el panel lo muestra como información por conexión
  (HU-CONN-09). Los créditos de SolversHub cubren solo el fee de plantillas, no Meta.

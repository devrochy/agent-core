---
name: meta-whatsapp
description: >
  Conocimiento vigente de WhatsApp Business Platform / Meta para el motor: Embedded Signup
  (Tech Provider, config_id), plantillas y categorías, ventana de 24 h, precios por mensaje
  desde 2026, firma del webhook, BYOK y número. Úsala al implementar HU-CONN-*, HU-SH-05 y
  HU-ARCH-13 antes de tocar el gateway o el wizard de conexiones. Cada dato lleva fecha y fuente.
---

Datos verificados el **2026-10-02** (versión de Graph en el repo: `v21.0`,
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
- Estado actual: Embedded Signup **no está conectado** y BYOK queda fijado en sandbox
  (N-10). HU-SH-05 y HU-CONN-06 cierran eso.

## BYOK y ciclo de vida del número

- BYOK: el negocio trae app id/secret y token propios; registrar el número y resolver
  webhook y secreto **por conexión** (dos negocios no comparten credenciales).
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

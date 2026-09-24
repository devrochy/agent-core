# ADR-0064 — Comunicación con el propietario: bitácora por tenant con enlace wa.me

- **Estado:** aceptado
- **Fecha:** 2026-09-24

## Contexto

La Épica 9 de `app-cobranza` (HU-63) pide una sección de **conversaciones con cada
socio**: notificaciones de cobro enviadas, **mensajes manuales Admin↔Socio** y
**enlace directo a su WhatsApp** ("espejo de HU-53"). En el modelo del pack, el
"socio" es el **propietario**, y por ADR-0062 **el propietario ES el tenant**: no
hay una segunda parte (un "admin global" distinto) dentro del tenant.

`app-cobranza` resuelve esto en su Fase 1 con **registro manual** y un canal de
WhatsApp simulado; los envíos automáticos y los proveedores reales son Fase 2/3.

## Decisión

Modelar la comunicación con el propietario como una **bitácora por tenant**:

- `pack_cobranza_conversaciones_propietario` (uno por tenant, `unique(tenant_id)`)
  y `pack_cobranza_mensajes_propietario` (`direccion` saliente|entrante, `tipo`
  notificacion|manual, `autor_rol`, `contenido`, `cobro_id` opcional).
- El **WhatsApp del propietario** vive en `CobranzaConfig.propietario_whatsapp`
  (config del pack por tenant; el propietario es el tenant, misma decisión que
  ADR-0062 para `moneda`/`pais`/`nombre_oficina`).
- **Notificación automática**: al generar un `CobroPropietario` se registra un
  mensaje `tipo=notificacion` en el hilo.
- **Mensajes manuales** desde el panel (saliente/entrante), que quedan en el hilo
  y en la auditoría.
- **Envío por WhatsApp = enlace `wa.me`** construido en el cliente con el texto
  del mensaje (envío manual, semántica de Fase 1). **No** hay envío automático por
  el sender del core ni webhook entrante en v1.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Chat plataforma (master) ↔ tenant en `core/admin` | Es un eje distinto (cross-tenant) y no lo pide el vertical; el pack ya modela el cobro al propietario |
| Envío automático vía el sender de WhatsApp del core | Requiere el número del propietario como conexión de canal y plantillas aprobadas; excede el MVP (paridad con la Fase 1 de `app-cobranza`) |
| Conversación entrante real (webhook) | El inbound del propietario iría por el pipeline de conversaciones del core (keyed por teléfono); alcance mayor, sin necesidad hoy |
| Sin entidad de conversación (solo notificaciones) | HU-63 pide explícitamente el hilo con mensajes manuales en ambos sentidos |

## Consecuencias

**Positivas:** cubre HU-63 sin depender de un canal; el propietario ve el historial
completo de cobros notificados; cero infraestructura WhatsApp nueva; el hilo se
puede promover a envío automático cuando exista la conexión del propietario.

**Negativas / lo que aceptamos pagar:** el envío es manual (el operador pulsa
"WhatsApp" y confirma); no hay acuse de recibo ni mensajes entrantes automáticos;
el número del propietario se configura a mano.

**Qué haría que revisáramos esto:** que el negocio requiera notificaciones
automáticas por WhatsApp al propietario (→ conexión de canal + plantilla) o hilo
bidireccional real (→ webhook entrante).

## Impacto en el código

- `packages/pack-cobranza/pack_cobranza/models.py` (2 tablas) y migración `0008`.
- `packages/pack-cobranza/pack_cobranza/admin.py` (`/cobranza/propietario`).
- `packages/pack-cobranza/pack_cobranza/config.py` (`propietario_whatsapp`).
- `packages/pack-cobranza/pack_cobranza/templates/cobranza/propietario.html`.

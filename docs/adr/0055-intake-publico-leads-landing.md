# ADR-0055 — Intake público de leads desde la landing

- **Estado:** reemplazado por [ADR-0057](0057-rutas-publicas-e-identidad-de-pack.md)
- **Fecha:** 2026-09-23

> **Reemplazada.** La superficie `/webhooks/leads/{slug}` aquí propuesta fue descartada
> por ADR-0057 (las rutas públicas de pack viven bajo `/api/pack/<plugin_id>/…`, no
> reutilizan `/webhooks/*`). La implementación vigente es
> `POST /api/pack/pack_commerce/leads/{slug}` en `pack_commerce`, con **secreto por
> tenant** (`CRMConfig.lead_intake_secret`) y configuración por tenant. Este documento
> se conserva como registro histórico del problema original.

## Contexto

La landing de SolversHub tiene un formulario que, en el flujo híbrido acordado, envía
los datos del prospecto al agente de la agencia (tenant `solvershub`) para que queden
en el CRM del panel. La landing ya implementa `POST /api/leads` y soporta reenviar el
lead a un webhook configurable (`LEAD_WEBHOOK_URL`), pero **agent-core no expone hoy
ningún endpoint público para recibir leads de terceros**: solo tiene el webhook de Meta
(firmado) y el panel admin (autenticado).

Enviar el lead a un endpoint existente no es viable: el webhook de Meta espera el
payload de WhatsApp y el panel exige sesión de admin. Hace falta una superficie pública
mínima y acotada.

## Decisión

Agregar un endpoint público dedicado en el gateway:

```
POST /webhooks/leads/{slug}
Header: X-Intake-Secret: <LEADS_INTAKE_SECRET>
```

Reglas:

1. **Autenticación por secreto compartido.** `LEADS_INTAKE_SECRET` vive en `.env` (core).
   Sin secreto correcto → `401`. No hay sesión de admin ni cookie.
2. **Resolución de tenant por `slug`.** El slug es dato público (marca), no un id. Se
   resuelve la tabla `tenants` (sistema, sin RLS) y se escribe dentro de
   `tenant_scope(tenant_id)` para que apliquen RLS y el aislamiento.
3. **Alcance mínimo.** El endpoint **solo crea un lead** (nombre, email, WhatsApp,
   negocio, opt-in, origen). No ejecuta el loop del LLM ni envía mensajes salientes:
   el primer mensaje lo inicia el usuario por WhatsApp (ventana de servicio de Meta).
   El envío proactivo, si el lead marcó opt-in, es un paso posterior y separado
   (plantilla aprobada) — fuera de este ADR.
4. **Escritura idempotente y defensiva.** Rate limit por IP; valida campos; si el lead
   ya existe (email/WhatsApp), actualiza en lugar de duplicar. Reutiliza el modelo de
   leads de `pack_commerce` si el pack está activo para el tenant; si no, responde
   `202` y lo registra en el log (no falla el formulario del usuario).
5. **Sin datos sensibles en la respuesta.** Solo `{ "ok": true }`.

## Consecuencias

- La landing apunta `LEAD_WEBHOOK_URL` a `https://<gateway>/webhooks/leads/solvershub`
  con el header del secreto.
- Es un endpoint **público**: exige el secreto, rate limit y validación estricta; se
  documenta en `security.md` como superficie expuesta.
- Habilita el mismo mecanismo para otras landings o campañas (multi-tenant).

## Pendiente

- Implementación del router + tests de integración (secreto inválido, rate limit,
  duplicado, tenant inexistente).
- Decidir si el alta de lead dispara un aviso interno (email/Slack) al equipo.

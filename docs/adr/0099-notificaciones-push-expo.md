# ADR-0099 — Notificaciones push de la app oficial vía Expo Push Service

- **Estado:** aceptado
- **Fecha:** 2026-10-05
- **Contexto:** HU-APP-05 (SPEC-25, solvershub-docs), decisiones D-SPEC-25-* y ADR-APP-04 (solvershub-apk)

## Contexto

La app oficial (SPEC-25) necesita notificaciones push para eventos del negocio (escalación de una
conversación) y del campo (pago/abono/gasto sincronizado por un gestor). Hoy las notificaciones de
la app de campo son pull (`GET /gestor/notificaciones`) y el core no envía push a ningún
dispositivo. El proyecto Expo ya tiene projectId de EAS (no renombrable), así que el servicio de
push natural es **Expo Push Service** (`https://exp.host/--/api/v2/push/send`).

## Decisión

- **Tabla de NEGOCIO `push_tokens`** (tenant_id + RLS forzada + policy con `nullif`, invariante 1):
  `tenant_id`, `admin_id`, `provider` ("expo"), `token`, `platform` ("android"|"ios"),
  `deleted_at` + índice único parcial `(tenant_id, token) WHERE deleted_at IS NULL`.
- **Registro por la app**: `POST /api/v1/me/push-token {token, platform}` y
  `DELETE /api/v1/me/push-token {token}` con la sesión admin (`get_principal`, sin gate de
  sección: cualquier usuario registra su propio dispositivo). Alta idempotente (reactiva un token
  que existía y se dio de baja); baja por soft delete. El gestor registra con su sesión admin
  (misma identidad del exchange, ADR-0098): el `admin_id` es el de la cuenta, el `tenant_id` el
  negocio activo.
- **Envío determinista desde el servidor** (Celery), nunca desde el modelo: cliente HTTP en
  `core/notifications/push.py`; tarea `core.push.send_batch` en `core/tasks/push_tasks.py` con
  `run_async` (patrón de `outbound_send`). Resuelve tokens dentro de `tenant_scope` y envía por
  lotes; los tickets con error `DeviceNotRegistered` se dan de baja (soft delete) para no
  reintentar infinitamente.
- **Destinatarios v1**: dueños del negocio (`TenantUser.role = "owner"`). Dos disparadores ya
  conectados: (a) escalación creada en el core (`escalate()`) → "Nueva escalación"; (b) eventos de
  campo sincronizados por un gestor (`POST /sync-offline/eventos`) → "Actividad de campo". Los
  textos de título/cuerpo viven en el código como constantes del módulo de push (infraestructura,
  no prompts; invariante 3 no aplica a notificaciones deterministas del servidor).
- **Canal opcional**: `EXPO_PUSH_ENABLED` (default false) + `EXPO_PUSH_URL` +
  `EXPO_PUSH_ACCESS_TOKEN` opcional. Sin habilitar, el envío es log-only (mismo criterio que el
  correo de sistema ADR-0098 y las alertas de Slack): un canal sin configurar no rompe el flujo.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| FCM directo (Firebase) | Agrega proyecto Firebase y credenciales separadas; Expo Push ya cubre Android e iOS con el projectId de EAS existente |
| Notificaciones solo in-app (pull) | No alerta en segundo plano; el objetivo es reaccionar sin abrir la app |
| Push desde el pack de cobranza | Las escalaciones son del core y el push es capacidad transversal; tabla del core con RLS, los packs la consumen |
| Resolver destinatarios por permiso de sección en v1 | Requiere materializar RBAC en el worker; v1 usa dueños del negocio (simple y correcto), granularidad por sección queda como evolución |

## Consecuencias

**Positivas:** un solo canal de push para panel y campo; tokens aislados por negocio (RLS);
bajas automáticas de dispositivos huérfanos; habilitación por entorno sin romper test/dev.

**Negativas / lo que aceptamos pagar:** dependencia del servicio de Expo (cuotas en producción);
los destinatarios v1 son dueños (colaboradores con permiso de sección quedan para después);
los textos son fijos (sin personalización por negocio todavía).

**Qué haría que revisáramos esto:** que se necesite FCM por requisitos de clientes, que se
personalicen textos/tono por negocio, o que la app gestione el badge/sonido por tipo de evento.

## Impacto en el código

- `core/db/models.py` (`PushToken`), `alembic/versions/0054_push_tokens.py`.
- `core/notifications/push.py`, `core/tasks/push_tasks.py` (+ include en `celery_app`),
  `core/config.py` (`EXPO_PUSH_*`), `core/api/routers/push.py` (`/api/v1/me/push-token`).
- `core/orchestrator/escalation.py` (disparo tras `escalate`) y
  `packages/pack-cobranza/pack_cobranza/api_gestor.py` (disparo tras sync de eventos de campo).
- `docs/openapi.json` regenerado.
- App (`solvershub-apk`): `expo-notifications`, registro del token y navegación por deep link
  (parte app de HU-APP-05).

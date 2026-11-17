# ADR-0202 — Fechas y horas: UTC en tránsito, zona del negocio en pantalla

- **Estado:** aceptado (plan aprobado por el dueño el 2026-11-16).
- **Fecha:** 2026-11-16
- **Usa:** ADR-0091 (zona horaria del negocio), ADR-0183 (historial de crons).
- **Relacionado:** ADR-0201 (crons por negocio).

## Contexto

El backend guarda instantes en UTC (`timestamptz`) y las fechas de calendario en `date`. La API
devuelve `datetime` en ISO 8601, pero hay tres fugas que hacen que el panel muestre UTC:

1. **SSR sin zona**: `lib/format.ts::formatDateTime` usa `Intl.DateTimeFormat("es-CO")` sin
   `timeZone`. En el servidor formatea en la zona del contenedor (UTC), no en la del negocio.
2. **Respuestas preformateadas**: varios packs devuelven `strftime("%Y-%m-%d %H:%M")` en JSON
   (`pack-cobranza/admin_api.py`, `pack-commerce`, `pack-calendar`). El panel no puede convertir
   texto ya formateado.
3. **Hardcodes**: `lib/crons.ts` asume Colombia (UTC−5), `components/dashboard/actividad-agentes.tsx`
   usa `timeZone: "UTC"` y hay 36 `date.today()`/`datetime.now()` ingenuos en lógica de negocio
   (mora, cierres, «hoy»), que usan la zona del servidor.

El dueño pidió un formato estándar y que la hora de los registros sea la del negocio.

## Decisión

### 1. Contrato de datos

| Dato | Se guarda | API | Pantalla |
|---|---|---|---|
| Instante (`created_at`, `started_at`, `at`…) | `timestamptz` UTC | ISO 8601 con offset (`2026-11-16T13:23:00Z`) | zona de `app_settings.timezone` |
| Fecha de calendario (`vence`, `fecha`, `period_start`) | `date` | `YYYY-MM-DD` | `dd/MM/yyyy`, sin conversión |
| Hora de pared (cita, horario) | tal cual (no es instante) | `HH:mm` o ISO local | tal cual, sin conversión |
| Expresión cron | UTC (contrato de plataforma) | `0 9 * * *` | UTC + hora del negocio como ayuda |
| Duración relativa | — | — | «hace 5 minutos», sin zona |

Nunca se devuelve una fecha naive ni un `strftime` en una respuesta JSON. Las vistas HTML/PDF y los
mensajes que sí deban mostrar hora local usan `ZoneInfo(app_settings.timezone)`.

### 2. Backend: reloj del negocio

Nuevo `core/shared/tiempo_negocio.py`:

- `zona_del_negocio(session, tenant_id)` → `ZoneInfo` (fallback `America/Bogota`);
- `ahora_del_negocio()` / `hoy_del_negocio()` dentro del `tenant_scope`;
- `a_local(dt)` / `a_utc(dt)` para las fronteras.

Se reemplazan los 36 `date.today()`/`datetime.now()` ingenuos de lógica de negocio, empezando por
cobranza (mora, cierres, «hoy»). Un negocio en Bogotá a las 20:00 (01:00 UTC del día siguiente)
debe seguir viendo «hoy» correcto.

### 3. Panel: un solo formateador

`lib/fechas.ts` con `formatFecha`, `formatFechaHora`, `formatFechaLarga`, `formatHora`,
`formatFechaHoraTecnica` y `formatRelativo`. Las funciones de instantes **exigen** `timeZone`; las
fechas-solo no se convierten. `MeResponse` gana `timezone` y `currency` para que el shell no pida
`/admin/settings` en cada pantalla. Los componentes cliente usan `useFormato()`; los servidor pasan
la zona de `me.timezone` o `getBusinessTimezone()`. El barrido está completo: todas las llamadas
del panel pasan la zona del negocio (o la por defecto del producto en las fechas-solo), y el
guardián de CI lo exige. `formatDateTime` ya no usa nunca la zona del servidor (UTC).

Formatos estándar:

- tablas y listas: `16/11/2026 08:23`;
- fichas y detalle: `16 de noviembre de 2026, 08:23`;
- solo fecha: `16/11/2026`;
- pantallas técnicas: `16/11/2026 08:23 (13:23 UTC)`.

### 4. Barrido completo

Se migran todos los usos de fechas del panel (43 `formatDateTime` y el resto de formateos), se
quitan los hardcodes y se agrega un guardián en CI: falla si aparece `Intl.DateTimeFormat` sin
`timeZone`, `formatDateTime(valor)` con un argumento o `strftime` en una respuesta JSON, con lista
blanca para fechas-solo y pantallas técnicas.

## Alternativas descartadas

| Opción | Por qué no |
|---|---|
| Formatear en el backend y devolver texto | Acopla presentación a API, rompe i18n y duplica la lógica; el panel ya tiene que pintar fechas de varias fuentes. |
| Usar la zona del navegador | El dueño puede estar de viaje; la hora del negocio es la que importa. |
| Fijar `TZ` del proceso a la del negocio | El backend atiende varios negocios en el mismo proceso. |
| Un `Intl` global sin `timeZone` | En SSR usa la zona del contenedor y es la causa del bug. |

## Consecuencias

- `MeResponse` con `timezone`/`currency`; `schema.d.ts` se regenera.
- `lib/fechas.ts` + `useFormato()` + guardián.
- Migración por lotes de las pantallas; primero Conversaciones, Clientes, Pedidos, Pagos, Auditoría,
  Escalaciones, Notificaciones, Calendario, Suscripciones y DLQ.
- Limpieza de `strftime` en respuestas JSON de packs y de `date.today()` en cobranza.
- Tests con `America/Bogota`, `UTC` y `America/New_York` (DST); fechas-solo sin desplazamiento.

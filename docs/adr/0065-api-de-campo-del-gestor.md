# ADR-0065 — API de campo del gestor: el pack implementa el contrato de la APK

- **Estado:** aceptado
- **Fecha:** 2026-09-24

## Contexto

La APK del gestor (`solvershub-apk`, Expo/React Native) ya existe y está madura,
pero hoy consume el backend NestJS `devrochy/app-cobranza` con un contrato propio:
`POST /auth/gestor/login`, `POST /auth/refresh`, `GET /perfil`, rutas `/gestor/…`
en **camelCase** y **IDs numéricos**, y `POST /sync-offline/eventos` autenticado con
`x-device-key`.

El vertical de cobranza vive ahora en agent-core (`pack_cobranza`), cuya API de
campo (ADR-0057) es mínima: 5 rutas (`auth/login` por dispositivo, `mis-carteras`,
`clientes-dia`, `sync/eventos`, `evidencia`), en **snake_case**, con **UUID** y sin
refresh. Hay que decidir **quién se adapta a qué contrato** al migrar la APK.

## Decisión

El **pack implementa el contrato exacto de la APK**, bajo su prefijo público
(`/api/pack/pack_cobranza`):

- Mismas rutas: `/auth/gestor/login`, `/auth/propietario/login`, `/auth/refresh`,
  `PATCH /perfil`, `/gestor/…`, `/sync-offline/…`.
- Respuestas **camelCase** idénticas a `src/types/gestor.ts` (`telefonoWhatsapp`,
  `fechaVencimiento`, `valorEsperado`…), con **IDs como string** (UUID).
- **Paridad total** con lo que la APK hace hoy: lecturas (mis-carteras con `config`
  y `permisos`, trayecto-diario, clientes, tarjeta, estado-cuenta, préstamos,
  detalle-cuota, notas, estadísticas, liquidaciones, notificaciones) y escrituras
  (visitas, gastos y evidencias multipart, crear/editar cliente, crear préstamo,
  editar/eliminar cuota, eliminar pago/abono, liquidación + PDF, apertura, posición,
  trayectoria-real, generar trayecto, editar cartera/configuración, crear nota).
- Nuevo router `pack_cobranza/api_gestor.py`, montado en `public_routes()` y
  **separado** del `api.py` de campo actual (que se conserva para la app de campo v1).
- `sync-offline` se autentica con **Bearer** (la sesión del gestor). Se descarta
  `x-device-key` estático: el pack guarda `api_key_hash` (no reversible) y la sesión
  del gestor ya está disponible.

La APK cambia **lo mínimo**: base URL (`EXPO_PUBLIC_API_URL=<host>/api/pack/pack_cobranza`),
IDs `number → string` y sesión access+refresh.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Reescribir `src/api/*` de la APK al estilo del pack (snake_case, rutas nativas) | Más cambios en la app; el contrato actual ya está probado y con tipos espejo (`types/gestor.ts`) |
| Capa adaptadora intermedia (otro servicio) | Complejidad de despliegue sin valor |
| Dejar la APK en NestJS | El vertical ya vive en agent-core; sería mantener dos backends |
| Mantener `/api/pack/pack_cobranza` y `/gestor/…` en paralelo (dos superficies) | Doble mantenimiento del mismo dominio; se prefiere una sola API de campo v2 |

## Consecuencias

**Positivas:** cambios mínimos en la APK; un solo backend (agent-core); el contrato
de la APK se convierte en el contrato de la API de campo del gestor.

**Negativas / lo que aceptamos pagar:** el pack carga un contrato "ajeno"
(camelCase + IDs string) y debe mantenerlo; los IDs de la APK pasan de `number` a
`string` (refactor transversal, mecánico); el refresh de tokens es nuevo en el
pack; el PDF de liquidación es nuevo.

**Qué haría que revisáramos esto:** que la APK se reescriba por completo, o que
aparezca un segundo consumidor que prefiera el estilo nativo del pack.

## Impacto en el código

- `packages/pack-cobranza/pack_cobranza/api_gestor.py` (nuevo) y su montaje en
  `module.py::public_routes`.
- `packages/pack-cobranza/docs/api-gestor.md` (plan y descomposición).
- `solvershub-apk/src/{config.ts, api/*, types/*, session/*}` y
  `docs/ai/tasks/migracion-api-agent-core.md`.

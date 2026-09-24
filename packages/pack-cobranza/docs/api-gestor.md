# API de campo del gestor — contrato de la APK (plan)

- **ADR:** `docs/adr/0065-api-de-campo-del-gestor.md`
- **Consumidor:** `app-cobranza-apk` (Expo/React Native)
- **Router:** `pack_cobranza/api_gestor.py` (nuevo), montado en `module.py::public_routes()`
- **Estado:** plan aprobado (2026-09-24). Sin código hasta ejecutar por fases.

## Base y convenciones

- **Base URL:** `EXPO_PUBLIC_API_URL = <host>/api/pack/pack_cobranza` (el prefijo público
  del pack; ADR-0057). Rutas relativas: `/auth/…`, `/perfil`, `/gestor/…`, `/sync-offline/…`.
- **Contrato:** **camelCase** idéntico a `src/types/gestor.ts`; **IDs como string** (UUID);
  fechas ISO-8601; montos como número.
- **Auth:** `Bearer <accessToken>` (JWT firmado por el pack). `POST /auth/refresh` renueva.
  `sync-offline` también usa Bearer (se descarta `x-device-key`).
- **Aislamiento:** todo corre dentro de `tenant_scope` (RLS), resuelto por el tenant del token.

## Mapa de rutas

### Auth y perfil (F1) ✅
| Endpoint | Respuesta | Fuente en el pack |
|---|---|---|
| `POST /auth/gestor/login` `{tenant,usuario,password}` | `{accessToken, refreshToken, gestor}` | `gestores.usuario` + `password_hash` |
| `POST /auth/propietario/login` | **501** (decisión abierta) | propietario = tenant (ADR-0062) |
| `POST /auth/refresh` `{refreshToken}` | `{accessToken, refreshToken}` | nuevo (JWT `typ=refresh`, 30 d) |
| `PATCH /perfil` `{nombre,apellido}` | `PerfilPublic` | `gestor` del token |

> `tenant` (slug) es una **adición obligatoria** del contrato: agent-core es multi-tenant y
> el login de la APK no traía identificador de tenant. La APK lo envía desde `EXPO_PUBLIC_TENANT`.

### Lecturas (F2)
| Endpoint | Respuesta | Fuente en el pack |
|---|---|---|
| `GET /gestor/mis-carteras` | `CarteraApkPublic[]` (con `config` + `permisos`) | `Cartera` + `CarteraConfig` + `GestorPermiso` |
| `GET /gestor/notificaciones` | `NotificacionPublic[]` | `Pago`/`Gasto`/`Visita`/`CobroPropietario` recientes |
| `GET /gestor/carteras/:id/trayecto-diario` | `TrayectoDiarioApkPublic` | `_clientes_del_dia` + `CarteraOptimizadaLog` |
| `GET /gestor/carteras/:id/estadisticas` | `EstadisticasCarteraPublic` | `CarteraEstadisticasSnapshot` (actual/anterior/deltas) |
| `GET /gestor/carteras/:id/clientes` | `ClienteGestorPublic[]` | `Cliente` + `Prestamo` + `Cuota` |
| `GET /gestor/carteras/:id/clientes/:cid/tarjeta` | `ClienteTarjetaPublic` | `Cliente` + saldo/mora + `ClienteEvidencia` |
| `GET /gestor/carteras/:id/clientes/:cid/prestamos` | `PrestamoPublic[]` | `Prestamo` + `Cuota` |
| `GET /gestor/carteras/:id/prestamos/:pid/estado-cuenta` | `EstadoCuentaPrestamoPublic` | `Prestamo` + `Cuota` + `Abono` |
| `GET /gestor/carteras/:id/prestamos/:pid/cuotas/:cid/detalle` | `DetalleCuotaPublic` | `Cuota` + `Pago` + `Abono` + `Visita` |
| `GET /gestor/carteras/:id/notas` | `CarteraNotaPublic[]` | `CarteraNota` |
| `GET /gestor/carteras/:id/liquidaciones` | `LiquidacionPublic[]` | `Liquidacion` |
| `GET /sync-offline/trayecto-diario` | `TrayectoDiarioApkPublic` | snapshot cacheado para offline |

### Escrituras (F3)
| Endpoint | Respuesta | Fuente en el pack |
|---|---|---|
| `PATCH /gestor/carteras/:id` `{nombre,descripcion}` | `{id,nombre}` | `Cartera` |
| `PATCH /gestor/carteras/:id/configuracion` `{tipoInteres,numCuotas}` | `{id,tipoInteres,numCuotas}` | `Cartera`/`CarteraConfig` |
| `PATCH /gestor/carteras/:id/clientes/:cid` (geo) | `ClienteGestorPublic` | `Cliente` |
| `POST /gestor/carteras/:id/visitas/pago` | `VisitaPublic` | `Visita` + `Pago`/`Abono` |
| `POST /gestor/carteras/:id/visitas/no-pago` | `VisitaPublic` | `Visita` |
| `POST /gestor/carteras/:id/gastos` (multipart) | `GastoPublic` | `Gasto` + evidencias en disco |
| `POST /gestor/carteras/:id/trayectoria-real` `{puntos}` | `{id,tipo:"real"}` | `CarteraOptimizadaLog(tipo=real)` |
| `POST /gestor/carteras/:id/trayecto` (generar) | `TrayectoPublic` | `CarteraOptimizadaLog` (nearest-neighbor) |
| `POST /gestor/carteras/:id/clientes` (multipart) | `ClienteGestorPublic` | `Cliente` + `ClienteEvidencia` |
| `POST /gestor/carteras/:id/clientes/:cid/evidencias` (multipart) | `{clienteId}` | `ClienteEvidencia` |
| `POST /gestor/carteras/:id/prestamos` | `PrestamoPublic` | `Prestamo` + `Cuota` |
| `PATCH /gestor/carteras/:id/cuotas/:cid` `{valorEsperado,password,motivo}` | `CuotaPublic` | `Cuota` (+ auditoría) |
| `DELETE /gestor/carteras/:id/cuotas/:cid` `{password,motivo}` | `{id}` | soft-delete + auditoría |
| `DELETE /gestor/carteras/:id/abonos/:id` `{password,motivo}` | `{id}` | soft-delete + auditoría |
| `DELETE /gestor/carteras/:id/pagos/:id` `{password,motivo}` | `{id}` | soft-delete + auditoría |
| `POST /gestor/carteras/:id/liquidaciones` `{comentario}` | `LiquidacionPublic` | `Liquidacion` |
| `GET /gestor/carteras/:id/liquidaciones/:id/export-pdf` | bytes | generador PDF (nuevo) |
| `POST /gestor/carteras/:id/apertura` `{latitud,longitud}` | `{id,carteraId}` | `CarteraApertura` |
| `POST /gestor/carteras/:id/posicion` `{latitud,longitud}` | `{carteraId,latitud,longitud}` | `PosicionGestor` |
| `POST /gestor/carteras/:id/notas` `{nota}` | `CarteraNotaPublic` | `CarteraNota` |

### Offline (F4)
| Endpoint | Contrato |
|---|---|
| `POST /sync-offline/eventos` | `{eventos:[{eventoIdCliente,tipoEvento,payload}]}` → `[{eventoIdCliente, estado:"sincronizado"\|"duplicado"\|"error", error?}]`. Tipos: `visita`, `pago`, `abono`, `gasto`, `cambio_cliente`. |
| `GET /sync-offline/trayecto-diario` | snapshot del día para trabajar sin conexión |

## Fases

- **F0 — Contrato y esqueleto.** ADR-0065; este doc; `api_gestor.py` montado con un
  endpoint de eco; helper de serialización (camelCase + IDs string); `config.ts` de la APK.
- **F1 — Auth y perfil.** login gestor (+ propietario según decisión), refresh, `PATCH /perfil`;
  almacén/ttl de refresh; la APK ajusta `api/auth.ts` y `session/*`.
- **F2 — Lecturas.** las 12 rutas de lectura.
- **F3 — Escrituras.** las 22 rutas de escritura, incluidos multipart y el PDF de liquidación.
- **F4 — Offline.** `sync-offline/eventos` (5 tipos, ack por evento) y `sync-offline/trayecto-diario`.
- **F5 — Adaptación APK.** IDs `number→string`, base URL, sesión access+refresh, limpieza; `npm run check`.
- **F6 — Backlog + producción.** `REQ-APK-001..011`; autovinculación de dispositivo (HU-39/43):
  `POST /cobrador/dispositivo` + aprobación admin + APK envía `imei`/`whatsapp` + X25519.

## Verificación

- **Pack:** unit del contrato (camelCase + IDs string), **integración** por ruta con sesión
  real, seed, `ruff` + `pytest`; un test que valide que cada respuesta encaja con
  `types/gestor.ts` (snapshot/JSON schema).
- **APK:** `npm run check` (lint + typecheck + test) y E2E manual contra el pack
  (tenant/dispositivo demo).
- **PR por fase** (rama `feature/…` desde `develop`, Conventional Commits, CI verde).

## Riesgos / decisiones abiertas

- **IDs `number → string`** en la APK: refactor transversal (paths + tipos + comparaciones).
- **`/auth/propietario/login`**: el propietario es el tenant (ADR-0062); no hay usuario de
  propietario en el pack. Opciones: mapear al admin del core, o recortarlo de la v1 de la APK.
- **`password`+`motivo`** en editar/eliminar cuota/pago/abono: se mantiene y se registra en
  `AuditoriaCartera.motivo`.
- **Refresh tokens**: el pack hoy firma un JWT sin refresh; hay que definir store/TTL.
- **PDF de liquidación**: v1 propone **HTML imprimible** (sin dependencia nueva).

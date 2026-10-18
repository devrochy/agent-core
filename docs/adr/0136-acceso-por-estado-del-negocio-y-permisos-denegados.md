# ADR-0136 — Negocio suspendido sin acceso, permisos denegados por negocio y propietarios pares

- **Estado:** aceptado (P0 del ciclo de pruebas 2026-10-13)
- **Fecha:** 2026-10-17
- **Contexto:** defectos P0 de seguridad reportados por los testers en test. **Enmienda:** ADR-0047 (RBAC por
  sección), ADR-0063 (sub-secciones de pack), ADR-0048 (colaboradores). **Usa:** ADR-0134 (rol tester).

## Contexto

Tres cuentas distintas reprodujeron en test:

1. «Aparece suspendido pero igual puedo loguearme con el correo del propietario del tenant.» Ni el login, ni la
   resolución de la sesión (`get_principal`, middleware del panel clásico), ni la app de campo miran
   `tenants.status`; solo el webhook y el consumer (`is_tenant_operational`). `admin_tenants()` devuelve los
   vínculos de `tenant_user` sin filtrar estado.
2. «Revocó reportes cobranza, ingreso con el colaborador y aún así veo la sección»; «no le puedo quitar cobranza
   en general». ADR-0047 §3 da por defecto `read` a todo `colaborador` (y `write` al `owner`): borrar la fila
   explícita devuelve ese default, así que «revocar» no cambia nada. No existe un nivel «denegado» ni permiso a
   nivel de pack: solo las 13 sub-secciones de cobranza (ADR-0063).
3. «No me manda a login, sigue en colaborador.» La API ya responde 401 en el siguiente pedido (la sesión valida
   `admins.active`), pero `toggle_active` y el reset de contraseña dejan las filas de `admin_sessions`: al
   reactivar, las sesiones viejas resucitan; y el panel no reacciona al 401.
4. «Si es un propietario invitado no debería de poder modificar el propietario real»; «no se puede desvincular».
   `target_allowed_for_owner` solo exige que la cuenta sea exclusiva del negocio: un segundo `owner` podía
   resetear, desactivar o dar permisos al propietario original. Había `link` (master) pero no `unlink`.

## Decisión

1. **Un negocio suspendido no tiene sesión.** `core/shared/tenant.py::negocios_de_cuenta(admin)` es el único
   envoltorio sobre `admin_tenants()`: para quien no `actua_como_master` quita los negocios con
   `status != 'active'` o `deleted_at` (los `global` ya vienen filtrados). Lo usan el login, `get_principal`, el
   middleware y el selector del panel clásico, `GET /admin/tenants` y la app de campo. Con contraseña correcta y
   solo negocios suspendidos, el login responde **403 `tenant_suspended`** sin crear sesión (se evalúa después
   de la contraseña: no filtra qué correos existen, y no cuenta como intento fallido). Una sesión abierta
   responde 403 `tenant_suspended` en todo salvo `POST /auth/logout`, que deja de resolver negocio. Con dos
   negocios, solo se corta el suspendido (un `X-Tenant-Id` suspendido da 403 con ese código; sin header se
   elige el primer activo). Master y tester habilitado entran igual: reactivan o purgan desde Plataforma. La
   app de campo (`api_gestor._auth`) revalida por pedido que el negocio esté activo y que la cuenta siga
   vinculada (o sea de plataforma).
2. **Nivel `none` (denegado) en `admin_permissions.access`, solo por negocio.** Migración `0090`: CHECK
   `access in ('read','write') or (access = 'none' and tenant_id is not null)`. Nunca global: la rama de
   `admin_tenants()` que muestra todos los negocios a quien tiene un permiso global no debe dispararse por una
   denegación, y los cuatro consumidores que miran «existe fila global» (`admin_tenants`,
   `target_allowed_for_owner`, `invitations_service._invitado_valido`, `qa/datos._sigue_siendo_de_prueba`)
   siguen correctos. **Resolución** (función pura `resolver_seccion`, compartida por `effective_access`,
   `permissions_for` y `notification_recipients`): la fila de la sub-sección (`pack_cobranza:reportes`) manda
   sobre la del pack completo (`pack_cobranza`); dentro de un nivel, `none` deniega aunque exista un grant
   (global o del negocio); si no, el máximo (`write > read`); sin fila en ningún nivel, el default del rol de
   ADR-0047 §3. `collaborators` no se deniega por fila (sigue siendo del `owner`). **Revocar** con alcance de
   negocio a un miembro cuyo rol heredaría la sección guarda `none` (si no hay default que tapar, borra la fila
   como antes); revocar un pack completo borra además las filas de sus sub-secciones en ese alcance.
   `grant` admite `none` (`admin grant --access none --tenant <slug>` en el CLI). `/admin/me` expone también
   el acceso por pack (`pack_cobranza`, `pack_commerce`, …), no solo las sub-secciones.
3. **Desactivar y cambiar la contraseña cierran las sesiones** en la misma transacción
   (`core/admin/auth.py::revocar_sesiones`): el siguiente pedido es 401 y una cuenta reactivada entra por el
   login. El self-service de recuperación (ADR-0098) ya lo hacía.
4. **Los propietarios son pares.** Un actor que no es master gestiona solo cuentas con rol `colaborador` de su
   negocio activo (además de las guardas de ADR-0048: no de plataforma, exclusivas del negocio): un
   propietario invitado no toca al propietario real ni al revés; a un propietario lo gestiona el master. Un
   propietario crea colaboradores, no otros propietarios (ADR-0048 §3). El tester (ADR-0134) conserva lo que
   ya podía con sus cuentas de prueba, incluidas las de dueño. **`POST /admin/collaborators/{id}/unlink`**
   (`{tenant}`): el master desvincula cualquier vínculo; owner y tester, solo del negocio activo y solo a un
   `colaborador` que no sea de plataforma ni uno mismo, sin exigir exclusividad (sacar de MI negocio a una
   cuenta compartida no toca a los demás). Nadie deja un negocio sin propietario. Borra el `tenant_user` y los
   `admin_permissions` de ese negocio; el perfil de gestor de pack-cobranza queda sin vínculo y la app de campo
   deja de aceptarlo en el siguiente pedido (anotado, no se borra: dato del negocio).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Cambiar el default del `colaborador` a «sin acceso» (solo lo concedido) | Es lo que insinúa el catálogo de pruebas («solo ve lo permitido»), pero deja sin menú a todo colaborador existente sin backfill y contradice ADR-0047 §3. Queda como alternativa revisable por el dueño. |
| Revocar = borrar la fila y que el panel llame `grant none` | El botón «quitar» del panel seguiría sin efecto hasta cambiar el panel; el backend debe bastar. |
| `none` también global | Dispara la rama «ve todos los negocios» de `admin_tenants()` y exige redefinir la función SQL. |
| «Propietario real» = primer `owner` vinculado | Concepto derivado y frágil (el master que crea por el wizard queda primero). Pares es más simple y conservador. |
| Redefinir `admin_tenants()` para filtrar `status` | Cambia la firma usada por la app de campo y el selector; el envoltorio en Python cubre todos los puntos sin migración. |

## Consecuencias

**Positivas:** la suspensión corta de verdad el acceso del negocio (panel, API, app) con un código que el panel
puede explicar; «quitar» una sección o un pack entero se aplica en el siguiente pedido y se refleja en `/me`;
las sesiones no sobreviven a una desactivación; un propietario invitado no escala sobre el real; existe
desvincular.

**Negativas / lo que aceptamos pagar:** una consulta más por pedido para los admins no-plataforma
(`tenants` por ids); las filas `none` aparecen en `GET /admin/collaborators` (`access: "none"`) y el panel debe
renderizarlas como «denegado»; un propietario no puede sacar al co-propietario que invitó (lo hace el master).

**Qué haría que revisáramos esto:** que el dueño prefiera default-deny para colaboradores (alternativa 1), o
permisos por acción/recurso (ADR-0047, «qué haría que revisáramos esto»).

## Impacto en el código

`alembic/versions/0090_admin_permissions_denegado.py`; `core/shared/tenant.py` (`negocios_de_cuenta`);
`core/admin/auth.py` (`NegocioSuspendido`, `revocar_sesiones`); `core/admin/permissions.py` (resolutor);
`core/admin/collaborators_service.py` (revocar → `none`, pares, `unlink_tenant`); `core/api/deps.py`,
`core/api/routers/{auth,tenants,collaborators}.py`; `core/main.py`, `core/admin/{views,context}.py`;
`packages/pack-cobranza/pack_cobranza/api_gestor.py` (`_auth`); `cli/commands/admin.py`; `docs/openapi.json`;
`docs/MULTITENANT.md`. Tests: `tests/integration/test_p0_seguridad_pruebas.py` (`aislamiento`),
`tests/test_permisos_denegado_y_jerarquia.py`, `tests/test_api_auth.py`, `tests/test_qa_roles.py`.

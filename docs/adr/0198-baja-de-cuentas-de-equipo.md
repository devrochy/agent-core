# ADR-0198 — Baja de cuentas de equipo

- **Estado:** aceptado
- **Fecha:** 2026-11-15
- **Contexto:** Equipo (ADR-0047/0048), historial de Cobranza (ADR-0066/0176),
  invitaciones (ADR-0081) y cuotas de plan (ADR-0142).

## Contexto

Equipo ya permitía **desactivar** una cuenta (`toggle_active`) y **desvincularla** de un
negocio (`unlink_tenant`), pero no **eliminarla**. Eso deja cuentas que siguen apareciendo en
los listados y que el master puede reactivar sin una decisión explícita. A la vez, el
historial de cartera guarda el correo del gestor como texto (`Pago.registrado_por`,
`Abono.registrado_por`) y las notas/gastos guardan su `admin_id`/`gestor_id`; cualquier baja
tiene que conservar esa trazabilidad.

## Decisión

1. **Baja lógica de la cuenta.** `admins` gana `deleted_at`, `deleted_by`,
   `deleted_from_tenant_id`, `deleted_links` y `deletion_reason`. La fila **no se borra** y conserva `email`,
   `username`, `nombre` y `telefono`. `active = false` y las sesiones se revocan.
2. **No se anonimiza ni se reasigna el correo.** El correo es la identidad y el ancla del
   historial. Un correo dado de baja **no** se reutiliza automáticamente.
3. **Reactivación explícita de la MISMA identidad.** `POST …/reactivate` limpia la baja,
   conserva el `admin_id` y el historial, y **restaura los vínculos que tenía al darse de
   baja** (`deleted_links`, con su rol). Así la misma cuenta vuelve a sus negocios y el pack
   revive su perfil. El master reactiva cualquiera; un dueño/tester solo las que dio de baja
   en su negocio activo y solo ese vínculo. Reactivar vuelve a ocupar cupo del plan.
4. **El perfil de pack se da de baja lógica, no se borra.** Nuevo hook
   `on_collaborator_removed` / `on_collaborator_restored` en `BaseModule`. `pack-cobranza`
   marca el `Gestor` con `deleted_at`/`estatus = baja` y lo revive al reactivar; sus carteras,
   pagos, visitas, promesas, gastos y notas quedan intactos. `on_collaborator_role` también
   revive un Gestor dado de baja si se vuelve a asignar el rol.
5. **Limpieza de acceso.** Se borran vínculos (`tenant_user`), permisos
   (`admin_permissions`) e invitaciones pendientes; se revocan sesiones y tokens de
   recuperación; por negocio se dan de baja lógica `push_tokens` y `account_devices` y se
   invalida la caché de acceso. Una reactivación no hereda equipos aprobados.
6. **Reglas de quién puede eliminar.**
   - Master: cualquier cuenta de equipo, salvo cuentas de plataforma (`master`/`tester`).
   - Dueño/tester: solo colaboradores **exclusivos** de su negocio activo (la misma regla de
     `target_allowed_for_owner`), nunca a sí mismo, a otro dueño ni a cuentas con permisos
     globales.
   - Nadie deja un negocio sin propietario.
7. **Correo ocupado, alta bloqueada.** `GET /admin/collaborators/existe` responde
   `eliminada: true` y `POST /admin/collaborators` con ese correo devuelve un error que pide
   reactivar; nunca crea una segunda identidad.

## Consecuencias

- El historial y la auditoría conservan el correo real del gestor.
- “Mismo correo” se trata como “misma persona”: si quien vuelve es otra persona, hay que
  reactivar y revisar el historial; el correo no se reasigna solo.
- La reactivación devuelve la cuenta a los mismos negocios y roles que tenía; si un negocio
  ya no existe, ese vínculo se omite.
- El listado de Equipo gana `?eliminadas=1` (master: todas; dueño: las suyas) y campos
  `deleted`, `can_delete` y `can_reactivate`.

## Alternativas descartadas

- **Borrado físico:** rompe FKs y la invariante 8, y pierde el historial.
- **Anonimizar el correo:** `Pago.registrado_por` mostraría un correo falso.
- **Reactivar automáticamente al dar de alta el mismo correo:** permite heredar historial
  ajeno y duplica/mezcla perfiles de gestor.

# ADR-0176 — Hook `collaborator_profiles`: el perfil del pack en Equipo

- **Estado:** aceptado (HU-ACC-07, 2026-10-30).
- **Fecha:** 2026-10-27
- **Contexto:** SPEC-42 (D-54, REQ-ACC-04).
- **Extiende:** ADR-0066 (el gestor es un colaborador del core con perfil del pack; punto de extensión `collaborator_roles` + `on_collaborator_role`).
- **Usa:** ADR-0047/0048 (RBAC y colaboradores), ADR-0097 (el core no importa de los packs: el pack se registra), ADR-0061 (`admin_menu` como patrón de extensión declarativa).

## Contexto

ADR-0066 hizo que el gestor sea un `Admin` + `TenantUser(colaborador)` con un perfil `Gestor` del pack, y dio al core una vía **de ida** hacia el pack: cuando se crea un colaborador con un rol del pack, el core avisa con `on_collaborator_role`. No hay vía **de vuelta**: la página Equipo (`/colaboradores`) no sabe que un colaborador es gestor. `CollaboratorItem` no trae roles de pack, estatus, carteras ni los 14 permisos de la app de campo. Quien administra el equipo no ve a un gestor como tal y debe saltar a Cobranza › Gestores para saberlo o corregirlo.

La invariante 5 manda que ningún pack importe de otro y que el core no importe de los packs (lectura vigente en ADR-0097 y ADR-0167 §3). Leer las tablas de cobranza desde `collaborators_service` la rompería.

## Decisión

**El core pide a los packs habilitados los perfiles de un lote de colaboradores a través de un hook nuevo, y los agrega al listado; el pack decide qué describir y cómo se edita.**

1. **Hook opcional en `BaseModule`,** junto a `on_collaborator_role` (`core/mcp/types.py`):
   `async def collaborator_profiles(self, tenant_id, admin_ids) -> dict[uuid.UUID, list[PackProfile]]`. Por defecto devuelve `{}`. Es **por lote** (los ids de la página), así que son una consulta por pack y por página, no una por colaborador.
2. **`PackProfile`** (modelo del core, `extra="forbid"`): `pack` (id del plugin), `role_id` (el rol de `collaborator_roles`), `ref_id` (id del perfil dentro del pack), `etiqueta` (texto para la persona), `resumen` (objeto pequeño y plano que el cliente muestra sin interpretar: estatus, carteras `{id, nombre}`, permisos activos de los 14), y `edit` = `{kind, ref_id}` o ausente.
3. **El core agrega, no interpreta.** `collaborators_service` llama el hook de cada pack habilitado del negocio **en paralelo, con un tiempo máximo de 1 s por pack**, dentro de `tenant_scope`. Si el pack falla o se pasa del tiempo, el listado sale **intacto** (sin `pack_profiles` de ese pack) y se registra un `log.warning` con el pack y el negocio; un perfil roto nunca tumba Equipo.
4. **`CollaboratorItem.pack_profiles`** es una lista nueva y aditiva en la API de colaboradores (el campo ausente equivale a lista vacía), reflejada en `docs/openapi.json`.
5. **Quién ve el detalle.** El core entrega `resumen` solo al owner, al master y a quien tenga acceso a alguna sección de ese pack; para los demás conserva `pack`, `role_id` y `etiqueta` y descarta `resumen` y `edit`. El pack no ve quién pregunta.
6. **Editar es del pack.** `edit.kind` es una cadena abierta; el primero es `cobranza_gestor_permisos`. El panel reutiliza el diálogo de permisos de cobranza y el `PUT /pack/cobranza/collectors/{id}/permissions` que ya existe (con sus propios permisos del pack); un `kind` que el panel no conoce se muestra **solo en lectura**. Las carteras se muestran en lectura con un enlace a Cobranza › Gestores, donde se asignan; el alta del gestor también sigue allí.
7. **Sin estado nuevo:** ni tablas ni migraciones ni caché. El listado de colaboradores sigue siendo la fuente; el hook es una lectura.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| El core lee las tablas de cobranza | Rompe la invariante 5 y acopla Equipo a un pack concreto. |
| Añadir `admin_id` al DTO `Collector` y cruzar en el panel | Obliga a una llamada por colaborador (N+1) o a traer todos los gestores, y filtra el esquema del pack al cliente. |
| Tabla `tenant_user_pack_roles` en el core | Ya descartada en ADR-0066: migración y tabla de sistema para algo que el perfil del pack ya guarda, con riesgo de desfase. |
| Que el pack publique un evento al cambiar el perfil y el core lo copie | Copia que puede desfasarse y exige un consumidor por pack; una lectura bajo demanda siempre está al día. |
| Un hook por colaborador | Una consulta por fila; el lote es la única forma de no repetir el N+1. |
| Mostrar y editar todo en Equipo | Duplicaría el módulo de Gestores (carteras, alta, baja); se limita a ver, editar permisos y enlazar. |

## Consecuencias

**Positivas:** el dueño ve en Equipo quién es gestor y qué puede hacer sin salir de la página; cualquier pack con rol de colaborador puede aparecer igual (el mecanismo no es de cobranza); sin migración; un pack caído no rompe el listado.

**Negativas / lo que aceptamos pagar:** una dependencia más del tiempo de respuesta de Equipo (mitigada por el lote, el paralelismo y el tope de 1 s); un contrato nuevo (`PackProfile`) que los packs deben respetar; el `resumen` es un objeto abierto y su forma la define cada pack, así que el panel solo sabe pintar los `kind` que conoce; un fallo silencioso del hook solo se ve en logs.

**Qué haría que revisáramos esto:** que el hook deba ser transaccional con el alta del colaborador (ADR-0066 ya lo anticipaba); que varios packs devuelvan perfiles del mismo colaborador con prioridades que haya que resolver; o que Equipo necesite filtrar o buscar por datos del perfil (hoy el filtro es solo del core).

## Impacto en el código

- `core/mcp/types.py` (`collaborator_profiles`, `PackProfile`), `core/admin/collaborators_service.py` (agregación en paralelo con tiempo máximo y recorte por permiso), `core/api/routers/` de colaboradores (campo `pack_profiles`), `docs/openapi.json`.
- `packages/pack-cobranza/pack_cobranza/module.py` (implementa el hook con una consulta por lote a `Gestor`, carteras y `GestorPermiso`) y `permisos.py` (los 14 permisos).
- Panel: `tabla-de-colaboradores.tsx` y el diálogo `cobranza/_components/collector-permissions-dialog.tsx`, más `schema.d.ts` regenerado (HU-ACC-07).
- Pruebas: hook por lote sin N+1, tiempo máximo (el listado sale intacto), recorte por permiso, aislamiento entre negocios y `kind` desconocido en el panel.

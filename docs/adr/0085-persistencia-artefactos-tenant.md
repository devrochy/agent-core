# ADR-0085 — Persistencia de los artefactos de tenant en despliegue

- **Estado:** aceptado
- **Fecha:** 2026-09-30
- **Relacionado:** ADR-0026 (override de playbooks), ADR-0054 (flow por tenant), ADR-0073 (prefijo de ruta)

## Contexto

Cada tenant tiene artefactos en **filesystem** bajo `TENANTS_ROOT` (default `/app/tenants`,
`core/onboarding.py:42-53`):

- `flow.json` — flujo de atención autoritativo del tenant (ADR-0054,
  `core/agents/flow.py:108-113,225-231`).
- `playbooks/*.md` — override de playbooks sobre pack/core (ADR-0026,
  `core/agents/playbooks.py:87-124`).
- `catalog.json`, `docs/`, `evals/` — catálogo sembrable y golden del tenant.

En **Coolify el contenedor no monta volúmenes**: verificado en test, el app
`agent-core-test` (`tr0vp…`) tiene `Mounts = []` y `/app/tenants/solvershub` solo contiene
`docs/` (creado en runtime). Consecuencia: **todo `flow.json`/playbook/catálogo que se cree
o siembre en el despliegue se pierde en el próximo redeploy**. Solo lo que vive en la **BD**
sobrevive (p. ej. `brand_voice` en `AppSettings`; `AgentSpec` en `agent_specs`).

Esto ya rompió el despliegue antes: el `flow.json` del tenant `solvershub` no llega al
contenedor, así que `resolve_flow` cae al flujo del pack y las secciones propias del tenant
no aplican.

## Decisión

Persistir `TENANTS_ROOT` con un **volumen** en el despliegue (Coolify), y dejar la migración
a BD como decisión posterior:

1. **Coolify**: montar un volumen persistente en `TENANTS_ROOT` para **cada** contenedor que
   lee/escribe artefactos de tenant: app (`api`), `worker` y `beat`. En `docker-compose.yml`
   añadir el volumen equivalente.
2. **Arranque**: el entrypoint/onboarding asegura la existencia de `TENANTS_ROOT` y de los
   subdirs por tenant; los seeds y el panel escriben en ese volumen.
3. **Backup**: incluir `TENANTS_ROOT` en el respaldo (hoy vive dentro del contenedor).
4. **Follow-up (REQ)**: evaluar mover `flow` y playbooks **a BD** (versionados) para tener
   una sola fuente de verdad y evitar dependencia de filesystem; se hará con ADR propio si
   se decide.

## Alternativas consideradas

| Opción | Por qué no (ahora) |
|---|---|
| No persistir (estado actual) | Los artefactos se pierden en cada redeploy; rompe flujos por tenant |
| Hornear `tenants/` en la imagen | El repo NO contiene datos de clientes (invariante); multi-tenant en un binario |
| Mover todo a BD ya | Más alcance (cambia loaders de flow/playbooks y su versionado); se difiere |
| Bind-mount del repo host | Atado a la máquina; Coolify remoto no lo tiene |

## Consecuencias

**Positivas:** los artefactos de tenant sobreviven redeploys; `flow`/overrides por tenant
funcionan en test y prod; respaldo posible.

**Negativas / lo que aceptamos pagar:** un volumen que administrar por entorno; sigue
habiendo dos fuentes (BD para spec/brand, filesystem para flow/playbooks); el volumen debe
montarse en app+worker+beat o se desincroniza.

**Qué haría que revisáramos esto:** que se necesite versionar/rollback de flow y playbooks
como el `AgentSpec` → mover a BD (follow-up).

## Impacto en el código / despliegue

- `docker-compose.yml` (volumen `tenants-data` en api/worker/beat).
- `docs/despliegue-coolify.md` (montaje del volumen en Coolify + backup).
- `core/onboarding.py` (asegurar dirs) y `scripts/entrypoint.sh` (mkdir de `TENANTS_ROOT`).

## Enmienda 2026-10-21 — controlador único de almacenamiento

- **Estado:** aceptada
- **Decisión de producto:** D-SPEC-04-2 pasa a ✅ (archivos bajo `TENANTS_ROOT`, no blobs en
  Postgres; S3/R2 como evolución).

### Contexto

El ADR fijó **dónde** viven los artefactos, pero no **cómo** se guardan. Cada feature que
recibe archivos reimplementó validación, límites, ruta segura, nombrado y limpieza:
`core/qa/evidencias.py` (capturas), `packages/pack-knowledge/pack_knowledge/archivos.py`
(documentos) y, próximamente, las imágenes de producto (HU-CRM-05), la media entrante
(HU-MEDIA-02) y los comprobantes. Eso multiplica el riesgo de un MIME mal validado, un path
traversal o una cuota olvidada.

### Decisión

1. **Un solo controlador** `core/almacenamiento` (puerto + backend de filesystem). Todo archivo de
   usuario o de canal pasa por él; ninguna feature escribe archivos por su cuenta.
2. **Perfiles por `kind`** (`producto`, `conocimiento`, `qa`, `media`, `comprobante`): MIME +
   magic bytes + tamaño + extensión + carpeta + si es por negocio o de plataforma + retención.
3. **`key` = ruta relativa a `TENANTS_ROOT`**, con el nombre físico puesto por el servidor y
   chequeo de traversal. Se **mantienen los paths actuales** de QA (`.plataforma/pruebas/`) y
   conocimiento (`<slug>/conocimiento/`) para no mover archivos existentes.
4. **URLs firmadas** con `APP_SECRET_KEY` y vida corta (`/api/v1/files/{token}`): 15 min para
   panel/APK y 60 min para que Meta descargue al enviar. No se exponen rutas internas.
5. **Registro `stored_files`** (tabla de sistema, `tenant_id` nulo = plataforma): metadatos
   para retención, cuota, auditoría y limpieza de huérfanos. **Siguiente paso de HU-ALM-01**
   (con su migración): hoy el controlador es stateless y el `Content-Type` sale de la extensión.
6. **Evolución S3/R2** detrás del mismo puerto; el filesystem es el backend por defecto.

### Prerrequisito

El volumen `tenants-data` debe estar **compartido y sano en api, worker y beat**. Hoy el
worker y el beat aparecen vacíos (pendientes del dueño, A.4); con un controlador único, un
archivo escrito por el worker que el api no ve es un fallo, no una molestia.

### Consecuencias

**Positivas:** una sola validación, un solo firmado, una sola retención; QA, conocimiento,
productos y media comparten contrato; migrar a S3/R2 no toca features.

**Negativas / lo que aceptamos pagar:** un perfil más que mantener; la tabla de sistema con
scoping explícito en vez de RLS (como `tenant_invitations`); el controlador es un punto único
que hay que probar bien (aislamiento, traversal, MIME, tamaño, atómico).

### Impacto en el código

- `core/almacenamiento/` (`perfiles.py`, `firmas.py`, `__init__.py`), `core/api/routers/files.py`.
- (siguiente paso) `core/db/models.py` (`StoredFile`) y su migración, cuando el número de
  migración quede libre.
- `pack_commerce` (HU-CRM-05), `core/qa/evidencias.py` y `pack_knowledge/archivos.py` (refactor
  con los mismos paths).

## Adenda 2026-10-25 — las keys son relativas y limpias, y los llamadores acotan por carpeta

Una auditoría halló que `pack_commerce` validaba la `image_key` solo por prefijo de texto
(`<slug>/productos/`), de modo que `<slug>/productos/../../<otro>/…` llegaba al archivo de otro
negocio (URL firmada y borrado). Reglas desde ahora:

- `abrir`/`borrar` descartan toda key con segmentos vacíos, `.` o `..`, barras invertidas, NUL o ruta
  absoluta (`key_limpia`), además de lo que ya escapaba de `TENANTS_ROOT`.
- Quien recibe una key del exterior (API, base) la acota con `dentro_de=carpeta_de(kind, slug)`: la ruta
  resuelta, enlaces simbólicos incluidos, tiene que quedar dentro de la carpeta de ese negocio. Un prefijo
  de texto no es una validación.
- `borrar` solo borra archivos y nunca carpetas; una key hostil ya guardada en la base no se sirve ni se
  borra (no se migran datos: se detectan con una consulta).

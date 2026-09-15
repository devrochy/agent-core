# ADR-0026 — Playbooks: override de contenido por tenant vía filesystem

- **Estado:** aceptado
- **Fecha:** 2026-09-15

## Contexto

`load_playbook` (`core/agents/playbooks.py`, ADR-0014) resuelve el
contenido de un playbook (`.md`) iterando `playbook_roots()`: `core/
playbooks/`, `plugins/*/playbooks/`, `packages/pack-*/playbooks/` —
ubicaciones fijas por deploy, sin ningún componente derivado del
tenant. Dos tenants con el mismo pack comparten literalmente el mismo
archivo físico. Un negocio que necesite un guion de ventas distinto al
de otro tenant del mismo pack no tiene forma de personalizarlo sin
editar el archivo del pack (rompería a TODOS los tenants que lo usan)
o forkear el pack — ítem 5 del mapa de parametrización por tenant
(`docs/ROADMAP.md`, fila 170, sesión 2026-09-12).

`load_playbook` ya separa dos fases independientes: (1) la allowlist
`AgentSpec.playbooks` decide QUÉ nombres puede pedir un tenant
(anti-inyección, control de acceso); (2) recién si el nombre está
permitido, se busca el contenido físico. Esta separación es la que
hace viable un override sin tocar el contrato ni el control de acceso.

`TENANTS_ROOT/<slug>/` (`core/onboarding.py::tenants_root()`/
`tenant_dir(slug)`) ya es la convención establecida para artefactos de
un tenant fuera del código del framework: `catalog.json` (seed),
`docs/` (documentación del cliente), `evals/` (golden set) —
100% filesystem, gitignored, gestionado por quien tenga acceso al
despliegue, nunca por el panel admin.

ADR-0017 fijó una frontera deliberada: "instrucciones (playbooks) →
NO editables por tenant en panel; eso es copy, no instrucción" — una
decisión consciente, no una omisión. Este ADR no la reabre: sigue sin
haber autoservicio de instrucciones en el panel para el tenant. Lo que
agrega es un mecanismo de override a nivel filesystem, del mismo nivel
de acceso que ya requiere colocar `catalog.json` antes de
`tenant seed` — no un editor self-service.

## Decisión

`TENANTS_ROOT/<slug>/playbooks/<nombre>.md`, si existe, gana sobre el
default del pack/core para ESE tenant. `load_playbook` chequea esta
ruta ANTES del loop existente de `playbook_roots()`, después de la
allowlist (que sigue sin cambios: el override no agrega nombres
nuevos, solo cambia dónde se busca uno ya permitido).

La carpeta se nombra por **slug**, no por `tenant_id` — consistente
con `catalog.json`/`docs/`/`evals/` del mismo tenant, para que quien
gestione el archivo a mano lo encuentre donde espera encontrar todo lo
demás de ese tenant. Costo aceptado: hace falta resolver el slug
(`core.shared.tenant.tenant_slug`, tabla `tenants` sin RLS por diseño)
— se resuelve **una sola vez por turno** en
`core/llm/prompt_builder.py::build_system_blocks` (que puede pedir 4-6
playbooks por turno) y se reenvía a cada `load_playbook`, en vez de
una query por playbook (N+1 evitado a propósito). Los otros call-sites
de producción (`core/tools/core_skill.py` — tool ad-hoc del LLM,
`core/agents/responses.py::load_response` — una vez por invocación) no
necesitan el parámetro: como mucho se llaman una vez, así que resuelven
el slug por su cuenta sin costo relevante.

`playbook_roots()` (y su otro consumidor, `core/onboarding.py::
_pack_playbook_refs`, que la usa para indexar `AgentSpec.playbooks` al
habilitar un pack) quedan **intactos**: la raíz de tenant NO vive
dentro de esa lista — si lo hiciera, `_pack_playbook_refs` intentaría
asociarla a un pack inexistente por nombre de carpeta.

**El primero que exista gana — sin merge, sin lógica condicional.**
Un override reemplaza el playbook COMPLETO, igual que el mecanismo que
ya existe entre `core/playbooks/` y `packages/pack-*/playbooks/`.
Mismo criterio de ADR-0014: *"qué haría que revisáramos esto: que un
playbook necesite lógica condicional o variables por turno"* — señal
explícita a vigilar, no una feature de esta tanda.

Mismo `MAX_SIZE_BYTES` (8 KB) y mismo criterio fail-open ante error de
IO que el resto de `load_playbook`: un override roto (permisos,
encoding, archivo demasiado grande) degrada al default del pack, no
tumba el turno — a diferencia del resto de la función (que si no
encuentra nada en `playbook_roots()` devuelve `None`), acá SÍ hay un
fallback razonable: seguir al loop de siempre.

Resolver el slug puede fallar por dos razones distintas, ambas
fail-open a "sin override" (nunca una excepción que tumbe el turno):
`spec.tenantId` inválido como UUID (specs de test/evals) o la consulta
a BD falla — capturado con un `except Exception` amplio en los dos
puntos donde se resuelve (`load_playbook`, `build_system_blocks`), no
solo `ValueError`, porque un hipo transitorio de BD no puede tumbar el
ensamblado del prompt entero.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Editor de contenido en el panel admin | Reabre la frontera que ADR-0017 trazó a propósito (instrucciones vs. copy); requeriría además un mecanismo de versionado/activación como el de `/admin/spec`, que no existe para playbooks. Alcance de una tanda propia, no de "evaluar una raíz TENANTS_ROOT" que pedía el ROADMAP. |
| Carpeta nombrada por `tenant_id` en vez de slug | Cero queries nuevas (ya disponible en `spec.tenantId`), pero la carpeta de overrides de un tenant no se vería como sus otras carpetas (`catalog.json`, `docs/`, `evals/`, todas por slug) — más incómodo para quien gestione el archivo a mano. Descartado explícitamente con el usuario a favor de slug. |
| Merge/diff entre el override y el default (en vez de reemplazo completo) | Complejidad no justificada por ningún caso real hoy — mismo criterio "sin lógica condicional" de ADR-0014. Reemplazo completo es la opción más simple que resuelve el problema real (guion de ventas distinto por vertical). |
| Agregar la raíz de tenant dentro de `playbook_roots()` | Ese mismo generador lo usa `_pack_playbook_refs` para indexar `AgentSpec.playbooks` al habilitar un pack (infiere el pack dueño por nombre de carpeta) — una raíz de tenant ahí se asociaría mal a un pack inexistente. |

## Consecuencias

**Positivas:** un tenant puede tener instrucciones propias (guion de
ventas, reglas de operación, tono) para un pack que comparte con otros
tenants, sin fork de código ni deploy — coherente con la invariante 2
de `CLAUDE.md` ("nunca se hace fork por cliente"). El mecanismo es
aditivo: ningún tenant sin override cambia de comportamiento.

**Negativas / lo que aceptamos pagar:** una query extra por turno
(resolver el slug), no cero como sería con `tenant_id`. Sin
autoservicio en el panel — seguir dependiendo de acceso al despliegue
para editar el override, igual que hoy con `catalog.json`.

**Qué haría que revisáramos esto:** que aparezca una necesidad real de
autoservicio (un negocio no técnico que quiera editar su propio guion
de ventas sin depender de un dev) — ahí sí valdría reabrir ADR-0017 y
diseñar un editor con versionado, no antes.

## Impacto en el código

`core/shared/tenant.py` (`tenant_slug`, nuevo), `core/agents/
playbooks.py` (`load_playbook` gana el kwarg `tenant_slug` y el
chequeo de override), `core/llm/prompt_builder.py`
(`build_system_blocks`/`_render_playbook` resuelven el slug una vez
por turno), `docs/MULTITENANT.md` (documentada la carpeta nueva).

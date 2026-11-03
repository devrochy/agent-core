# ADR-0184 — Registro de retiros: lo que el binario quita se avisa y se migra, nunca se corrige solo

- **Estado:** propuesto
- **Fecha:** 2026-11-03
- **Contexto:** incidente de test del 2026-11-03 (SPEC-43 PR-4 retiró `pack_commerce.contratar_plan` e `informar_plan`) y HU-AOS-59 (SPEC-26). Enmienda ADR-0113 §1 (el linter de `agentspec/3`) y ADR-0117 (releases inmutables).

## Contexto

Una versión de agente publicada (`agent_versions`) y una release construida (`connection_releases.manifest`) guardan los nombres de las acciones y de los procedimientos que usan, y ambas son inmutables. Cuando el código retira uno, la fila sigue nombrándolo: el chat del negocio inicia un procedimiento que ya no existe, el siguiente build falla con `contrato_invalido` y `migrar-a-v3` queda bloqueado. Nada lo avisaba; se reparó a mano, negocio por negocio, en cinco negocios de test. Con negocios reales, cada retiro de código sería un incidente.

## Decisión

El retiro de una acción o un procedimiento es parte del contrato del catálogo y se declara en un **registro versionado en el código** (`core/agents/retiros.py`): nombre viejo → reemplazo con rango de versión, o «quitar». Tres piezas lo sostienen, y ninguna corrige sola:

1. **CI.** `packages/catalogo-nombres.lock.json` es una instantánea de los nombres que el binario trae (core y habilidades de la plataforma); `tests/test_catalogo_retiros.py` falla si un nombre desaparece sin entrada en el registro, si aparece uno nuevo sin pasar por `scripts/catalogo_nombres_lock.py`, o si una entrada apunta a algo que el binario todavía trae o a un reemplazo que no existe.
2. **Detección** (`agent-core db auditar-referencias`, solo lectura). Un detector puro compara con el catálogo del binario (la misma regla de `lint_agentspec` para acciones; para procedimientos, los que publican los packs de la plataforma) la **última versión publicada** de cada agente activo y el **manifiesto de la release activa** de cada conexión viva conversacional. Mira el manifiesto, no el estado del negocio: `migrar-a-v3 --dry-run` dice `ya_estaba` aunque la release sea vieja. Los borradores se avisan aparte y no cuentan; las conexiones borradas, las de pruebas (`playground`) y las que no producen conversaciones (integraciones) se reportan aparte y no cuentan. Exit code distinto de cero si hay afectados.
3. **Migración** (`agent-core db migrar-referencias --tenant <slug>`, dry-run por defecto). Con `--aplicar`, por agente afectado: parte de la última versión **publicada** (nunca del borrador), aplica el registro, la valida con `revisar_spec` y la publica con etiqueta y nota; luego reconstruye y promueve la release de cada conexión afectada con `build.construir` (sin refrescar desde la ficha v2) y `promocion.promover` con la exención `migrar_referencias`, la misma ruta de `migrar-a-v3 --construir`. El reemplazo solo se agrega si **todas** las conexiones del agente tienen la habilidad que trae el procedimiento nuevo (si no, se quita lo retirado y el informe lo dice). Con un canary en curso la release se construye pero **no** se promueve (promover con exención lo detendría con `reemplazado`). Idempotente; deja fila en `audit_log` y eventos `built`/`promoted` con actor `system:migrar-referencias`.

**Decisión del dueño (2026-11-03): solo avisar.** Ninguna corrección corre sola tras un despliegue: la hace una persona con el comando (o, en una entrega posterior, el botón «Corregir» del master). Se revisa cuando haya negocios reales y canary activo.

Detalles que el diseño fija:

- `publicar` es idempotente por hash: si el contenido corregido repite una versión anterior, devuelve esa y el build seguiría tomando la última (la rota). En ese caso la migración publica con la marca `extensions.referencias_retiradas` (campo reservado y sin semántica, ADR-0050) para que exista una versión nueva.
- Publicar mueve `agent_drafts.base_version_id` a la versión nueva, pero no toca el contenido del borrador: «el borrador no se toca» se cumple y queda listado como «borrador con referencias retiradas».
- En el `kernelspec/1` solo se marcan los nombres que el registro conoce (`routing.rules[].start`, `when.flowStage.procedure`): pueden apuntar a un procedimiento abierto derivado del flujo, que el binario no ve.
- Los procedimientos abiertos (`=0.0.0`) no se comparan con el binario; la ficha v2 del negocio (secciones, voz de marca) queda fuera: solo la lee el motor legacy.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Corregir solo en cada despliegue | Cambia el comportamiento de un agente en vivo sin que nadie lo decida; el dueño pidió solo avisar. |
| Dejar que el build falle y reparar a mano | Es lo que pasó: el negocio queda con el agente roto hasta que alguien lee los logs. |
| Detectar solo por el linter al publicar | Solo protege versiones nuevas; las ya publicadas y las releases inmutables siguen rotas. |
| Duplicar la ruta de construir/promover | Se desviaría de `migrar-a-v3`; se reutilizan `construir` y `promover`. |

## Consecuencias

**Positivas:** un retiro de código ya no puede llegar a un negocio sin que el equipo lo sepa (CI + auditoría tras cada despliegue); la corrección es una orden con simulación previa y auditoría; las releases rotas que no importan (conexiones borradas) no generan falsas alarmas.

**Negativas / lo que aceptamos pagar:** quitar una acción exige una entrada en el registro y regenerar el lock (un paso más por PR); el registro crece sin borrarse (es historia: una versión de hace un año puede seguir nombrando lo retirado); para negocios en `legacy` el General y el Coordinador se regeneran desde la ficha v2 en cada build normal, y la ficha puede traer lo retirado de vuelta.

**Qué haría que revisáramos esto:** negocios reales con canary activo en varias conexiones (la corrección dejaría releases construidas sin promover con frecuencia: habría que enlazarla con el flujo del canary), o más de un retiro por mes (un PR con migración automática dentro de la ventana de despliegue sería más barato que el comando).

## Impacto en el código

- `core/agents/retiros.py` (registro y lock), `core/agents/referencias_retiradas.py` (detector y reescritor puros), `core/releases/referencias_retiradas.py` (auditoría y migración con base).
- `cli/commands/db.py`: `auditar-referencias` y `migrar-referencias`.
- `core/releases/build.py`: `habilidades_efectivas` (lectura); `core/releases/promocion.py` y `core/api/routers/connection_releases.py`: exención y motivo reservado `migrar_referencias`.
- `packages/catalogo-nombres.lock.json`, `scripts/catalogo_nombres_lock.py`, `tests/test_catalogo_retiros.py`, `tests/test_referencias_retiradas.py`, `tests/integration/test_referencias_retiradas.py`.
- Runbook: `docs/runbooks/referencias-retiradas.md`. Pendiente (entrega posterior de la HU): el botón «Corregir» en el panel (la corrección sigue siendo solo del CLI).

## Adenda (2026-11-03): API de solo lectura del aviso

El aviso llega al panel por tres rutas de solo lectura; ninguna escribe ni corrige (decisión del dueño: solo avisar).

- **Master:** `GET /api/v1/admin/platform/tenants/{tenant_id}/retired-references`. Es `auditar-referencias --tenant` por HTTP: filas con código interno, borradores y conexiones aparte. Solo `master` (ni tester ni colaborador, como el resto de Agent OS).
- **Dueño:** `GET /api/v1/admin/agents/retired-references` (todos los agentes del negocio con avisos) y `GET /api/v1/admin/agents/{id}/retired-references` (uno, para el editor). Aislados por `tenant_scope`; el texto sale de `core/agents/aviso_retiros.py`, sin códigos internos.
- **Publicar:** el borrador que nombra algo retirado da 422 `invalid_spec` con ese mismo texto por campo (y `details.retired_references`), antes de publicar. Para una acción retirada el status ya era 422; para un procedimiento retirado, el linter del contrato no lo veía: la versión salía publicada (200) y el build de la release fallaba después, por conexión. Ahora es el mismo 422 antes de publicar.

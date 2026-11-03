# Runbook · Referencias retiradas en agentes y releases (HU-AOS-59, ADR-0184)

Cuando una versión del código retira una acción o un procedimiento, las versiones de agente ya
publicadas y las releases ya construidas siguen nombrándolo. Este runbook es lo que se corre **tras
migrar la base de cada despliegue** y cómo se corrige. Decisión del dueño (2026-11-03): **solo avisar**;
ninguna corrección corre sola, la decide una persona.

## 1. Tras cada despliegue (después de `agent-core db migrate`)

```bash
agent-core db auditar-referencias --todos
```

Solo lectura. Imprime una fila por negocio: agentes afectados (última versión publicada), releases
afectadas (manifiesto de la release activa de una conexión viva y conversacional), borradores y
conexiones «aparte». **Exit code distinto de cero si hay afectados.**

- **No des el despliegue por bueno con afectados.** Corrige primero (sección 2) o decide por escrito
  por qué un negocio queda así.
- Los **borradores** (`Borradores`) y lo **aparte** (conexiones borradas, de pruebas o de integración)
  se avisan y no cuentan como falla: el borrador es del dueño; esas conexiones no corren una release.
- Un negocio con `(ERROR)` no se pudo auditar: el motivo sale debajo de la tabla; es un fallo, no un limpio.

Detalle de un negocio (agente, versión, ruta, código, qué hará la migración y dónde corre):

```bash
agent-core db auditar-referencias --tenant <slug>
```

## 2. Corregir un negocio

```bash
agent-core db migrar-referencias --tenant <slug>             # simula: no escribe
agent-core db migrar-referencias --tenant <slug> --aplicar   # publica y reconstruye
```

Siempre de a un negocio. Lee la simulación antes de aplicar:

- **Agentes:** cada uno parte de su **última versión publicada** (nunca del borrador, que no se toca y
  queda listado) y publica una versión nueva con etiqueta `sin referencias retiradas` y una nota con
  los cambios. Se revierte desde las versiones del panel (HU-AOS-27).
- **Reemplazo con la habilidad:** `pack_commerce.contratar_plan` pasa a `pack_subscriptions.contratar_plan ^2.0`
  solo si **todas** las conexiones del agente tienen `pack_subscriptions`; si no, se quita y el informe
  lo dice (habilítala en el negocio y agrega el procedimiento al agente a mano si corresponde).
- **Releases:** por cada conexión viva cuya release activa nombraba lo retirado se construye una release
  nueva y se promueve (sin pasar el gate de pass^k, como `migrar-a-v3 --construir`; actor
  `system:migrar-referencias`, motivo `migrar_referencias`).
- **Canary en curso:** la release se **construye pero no se promueve** (resultado `construida_sin_promover`):
  publícala por el flujo del canary o detén el canary primero.
- **Bloqueado:** un agente con un nombre retirado **sin entrada en el registro** (`core/agents/retiros.py`),
  un contrato que no pasa tras migrar o un build que falla (`contrato_invalido`, `habilidad_no_disponible`…)
  sale `bloqueado`/`bloqueada` con el motivo y exit code 1. Resuélvelo y vuelve a correr: es idempotente.
- Una segunda corrida no hace nada. Deja filas en `audit_log` (`referencias_retiradas_migradas`,
  `referencias_retiradas_release_promovida`).
- Negocios en `legacy`: el General y el Coordinador se regeneran desde la ficha v2 en cada build normal;
  si la ficha nombra algo retirado, reaparecerá (la ficha v2 está fuera de esta HU).

Verifica al terminar: `agent-core db auditar-referencias --tenant <slug>` debe salir limpio (exit 0).

## 3. Quien retira algo del código

Quitar una acción o un procedimiento del binario exige, en el mismo PR:

1. Una entrada en `core/agents/retiros.py` (`Retiro(...)` con `Reemplazo(id, versión)` o sin él, «quitar»).
2. Regenerar la instantánea: `python scripts/catalogo_nombres_lock.py` (se niega si falta la entrada).
3. Una nota en el PR: «tras migrar la base, corre `auditar-referencias --todos`».

`tests/test_catalogo_retiros.py` falla en el CI si falta alguno de los pasos. Las entradas no se borran:
una versión publicada hace un año puede seguir nombrando lo retirado.

## 4. Si algo sale mal

- **Se corrigió un agente que no debía:** revierte la versión desde el panel (versiones del agente) o
  haz rollback de la release de la conexión; el borrador del dueño nunca se tocó.
- **`ya_activa` / `ya_estaba`:** la release construida ya era la activa; no es un error.
- **Un build falla con otro código distinto de `contrato_invalido` por lo retirado:** no es de esta
  herramienta (falta una habilidad, un playbook, un flujo roto…): se resuelve con el mensaje del build.

## 5. Dónde lo ve el panel (solo lectura)

- Master: `GET /api/v1/admin/platform/tenants/{tenant_id}/retired-references` (mismo contenido que `auditar-referencias --tenant`).
- Dueño: `GET /api/v1/admin/agents/retired-references` y `GET /api/v1/admin/agents/{id}/retired-references`, en palabras simples.
- Publicar un agente que nombra algo retirado da 422 `invalid_spec` con el mismo texto; corregirlo sigue siendo `migrar-referencias`.

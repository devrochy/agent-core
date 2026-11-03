# Migración al Agent OS (runbook de HU-AOS-15)

> **D-60 (2026-11-03):** el Agent OS es el único motor. El panel del master ya no vuelve un negocio a `legacy` (422); el rollback de emergencia es solo por consola, `agent-core db migrar-a-v3 --volver --tenant <slug>`, mientras no se aplique el *contract*. Los negocios nuevos nacen en v3 si `AOS_CONNECTION_ROUTING=true`.

Cómo pasar un negocio del runtime anterior (`legacy`) al runtime por releases (`v3`), encender el motor de procedimientos y, al final de todo, cerrar la migración con el *contract*. Es el runbook que pide la
[HU-AOS-15](https://github.com/devrochy/solvershub-docs/blob/main/04-fase-1-competir/SPEC-26-agent-os/hu/HU-AOS-15-migracion-sin-regresion.md); las decisiones están en ADR-0112 (banderas), ADR-0115 (conexión canónica y contract),
ADR-0117 (releases) y ADR-0119 (pass^k).

Quién lo corre: el **master** de la plataforma. Los comandos van en el contenedor de la app o del worker (mismo entorno). Las banderas por negocio las escribe solo el master (CLI con el rol dueño o panel).

## 0. Las banderas y su orden

| Bandera | Alcance | Dónde se cambia | Qué hace |
|---|---|---|---|
| `AOS_CONNECTION_ROUTING` | **Global** (variable de entorno, requiere despliegue) | Coolify | El consumer resuelve la conversación por (conexión, cliente) y deja de reescribir `connection_id` |
| `aos_runtime` = `legacy` \| `v3` | Por negocio | CLI `migrar-a-v3` o panel | `v3`: el turno corre la release activa de la conexión |
| `aos_procedures` | Por negocio (exige `v3`) | Panel del master | El motor gobierna las tareas (contratar plan, pedido, cita…) |
| `aos_routing` = `off` \| `shadow` \| `active` | Por negocio (exige `v3`) | CLI `ruteo` | Enrutamiento por procedimiento (HU-AOS-17) |

**Se enciende en este orden:** `AOS_CONNECTION_ROUTING` → `aos_runtime = v3` → `aos_procedures`. **Se apaga al revés.** El panel y el CLI rechazan las combinaciones inválidas
(`aos_procedures` sin `v3`; `v3` sin `AOS_CONNECTION_ROUTING`; apagar `AOS_CONNECTION_ROUTING` con algún negocio en `v3`).

Una conexión pausada (ADR-0122) no ejecuta el Coordinador con ninguna combinación: el mensaje se guarda sin IA, como siempre.

## 1. Antes de empezar (una vez por despliegue)

1. **Migraciones y versiones de habilidades.** El entrypoint del contenedor (`scripts/entrypoint.sh`) corre `agent-core db migrate` y `agent-core db sync-packs`. Si la misma versión de un pack llega con otro contenido, `sync-packs` alerta y no la registra:
   sube la versión del pack.
2. **Versiones de procedimientos.** Un cambio en un procedimiento (por ejemplo `contratar_plan` 1.3.0, pack-commerce 2.4.0) tiene dos costos al desplegar:
   - los agentes con release **siguen con la versión anterior** (1.2.0) hasta que se **republican**;
   - las tareas abiertas de la versión anterior que no estén en una release **dejan de estar gobernadas** (flujo v2 de respaldo). Cancélalas en test antes de probar.
3. **`AOS_CONNECTION_ROUTING=true`** en el entorno (ver `docs/despliegue-coolify.md`) y despliegue. Con la bandera encendida pero ningún negocio en `v3`, el único cambio es el ruteo por conexión; apagarla sigue siendo seguro hasta el *contract*.
4. **Medir pass^4 antes de encender el motor** (§ 3.5). Solo se lanza a mano y con el OK del dueño: cada intento gasta saldo del LLM (1 a 3 USD con `k=4`).

## 2. Piloto

Empieza por **un** negocio de prueba (el dogfooding de SolversHub, HU-AOS-22) y recién después los demás, de a uno o por lotes chicos. Entre negocios, mira el log del consumer y los turnos con `blocked_by`/escalamientos.
Cada paso de la § 3 se corre primero con `--dry-run` y se revisa el reporte.

## 3. Paso a paso por negocio (`<slug>` o UUID)

### 3.1 Conexión canónica, agentes y releases (backfills, idempotentes)

```bash
agent-core db backfill-conexiones --tenant <slug> --dry-run   # revisa los choques con conexiones manuales del mismo número
agent-core db backfill-conexiones --tenant <slug>
agent-core db backfill-agentes   --tenant <slug> --dry-run
agent-core db backfill-agentes   --tenant <slug>
agent-core db backfill-releases  --tenant <slug> --dry-run
agent-core db backfill-releases  --tenant <slug>
```

El orden importa: los agentes necesitan las conexiones y las releases necesitan los agentes. Ninguno cambia lo que el cliente ve: con `aos_runtime = legacy` el runtime ignora la release.

### 3.2 Encender `v3`

```bash
agent-core db migrar-a-v3 --tenant <slug> --dry-run
agent-core db migrar-a-v3 --tenant <slug>              # solo si el reporte no trae bloqueos
agent-core db migrar-a-v3 --tenant <slug> --construir  # si la release activa está desactualizada: construye y promueve la nueva
```

`migrar-a-v3` enciende `v3` (**nunca** `aos_procedures`) solo si el negocio está listo; cualquier precondición que falle bloquea a ese negocio con el motivo y el comando que lo arregla:
`AOS_CONNECTION_ROUTING` encendida, backfills sin trabajo pendiente, cada conexión conversacional viva con release activa, release al día y ejecutable.
Con `--todos` aplica a todos los negocios vivos (no combina con `--construir`). Desde el panel del master (Agent OS del negocio): `GET/PUT /api/v1/admin/platform/tenants/{id}/agent-os` (cuerpo `runtime`, `procedures`, `routing`, `construir`) y `POST …/simular` para ver el resultado sin escribir.

### 3.3 Observar `v3`

Con `v3` y sin procedimientos, el negocio se atiende con su release (Coordinador + General) y la conducta debe ser la misma. Observa **una semana** como mínimo (HU-AOS-15, criterio de observación): ningún incidente P1 atribuible a la migración.
Si el pass^4 del negocio (§ 3.5) o el comportamiento empeoran, vuelve a `legacy` (§ 4).

### 3.4 Enrutamiento (opcional, HU-AOS-17)

```bash
agent-core db ruteo --tenant <slug> --modo shadow       # decide y registra, sin cambiar quién atiende
agent-core db reporte-ruteo --tenant <slug> --dias 7     # meta de R3: precisión ≥ 0,90 y recall ≥ 0,85
agent-core db ruteo --tenant <slug> --modo active
```

`reporte-ruteo` trae **dos fuentes** y rotula qué mide cada una (`--fuente golden|trafico|ambas`, por defecto ambas):

1. **Golden de ruteo** (verdad por escenario, `evals/ruteo/golden.jsonl`; `--golden <archivo>` para uno propio del negocio). Pasa cada frase por el router de la **release activa** de cada conexión y la compara con el procedimiento esperado. **No depende del tráfico ni de que el agente inicie nada**, y sirve igual con el ruteo en `off`, `shadow` o `active`. Es la fuente para decidir pasar a `active`: mide la configuración de ruteo (ejemplos, reglas, umbrales). Lista los casos que fallan y los «no aplicables» (su procedimiento no está en la release). Costo cero, sin LLM.
2. **Tráfico real**: lo que el router predijo en turnos reales (sin tarea en foco) frente al primer procedimiento que se inició en esa conversación hasta el siguiente turno. Cada turno lleva su **origen**: `sombra` (`shadow_route`) o `activo`.

Cómo leer el tráfico real:

- **En `shadow` el router no inicia nada**: lo inicia el agente por defecto con `iniciar_procedimiento`. Si no lo hace, la predicción queda **«Sin verdad»** (no se puede saber si era falsa) y la precisión sale **«no medible»**, no 0,00. Falso positivo = un turno donde se inició *otro* procedimiento. Recall = aciertos entre los inicios reales.
- Solo hay verdad con **`aos_procedures` encendida**; el reporte lo avisa si está apagada. Y como `iniciar_procedimiento` ofrece solo los procedimientos del agente en foco, el tráfico es justo si el agente por defecto los declara todos (el General).
- **En `active` no se escribe `shadow_route`**, así que ya no hay sombra que leer. El reporte usa los turnos enrutados por **disparador** (`route_source = 'trigger'`, su procedimiento es el primer candidato) frente a lo que inicia el especialista al que se enrutó. Las **reglas no se miden** en activo (la `start` la inicia el motor, acierta por construcción; la de agente no predice un procedimiento): se cuentan aparte. Si en la ventana no hubo disparadores, la tabla sale vacía y la leyenda lo dice; la sombra de antes de activar sigue contando mientras caiga en la ventana.
- Un reporte vacío o «sin verdad» no es un fallo del router: mide con el golden.

Para cerrar R3: el golden en `CUMPLE` y, en `shadow`, durante 7 días el tráfico real sin falsos positivos (turnos donde se inició otro procedimiento).

### 3.5 pass^4 antes de encender `aos_procedures`

La medición de la plataforma es el workflow **`pass^k de los procedimientos`** (`.github/workflows/evals-procedimientos.yml`, `workflow_dispatch`, runner propio): `k=4`, `runtime=ambos`, sobre la rama/commit que se va a desplegar.
Mide con y sin el motor los journeys de comercio, agenda, suscripciones y cobranza (cliente simulado + LLM real). Criterio (ADR-0112): **con motor, pass^4 igual o mejor que sin él** (hoy 100 %). Anota **valor y fecha en la bitácora de solvershub-docs**.

**Lanzarlo (solo con el OK explícito del dueño: gasta LLM):**

```bash
git fetch origin develop && git rev-parse --short origin/develop   # anota el commit que se mide
gh workflow run evals-procedimientos.yml --ref develop -f k=4 -f runtime=ambos
gh run list --workflow=evals-procedimientos.yml --limit 1          # id de la corrida; luego: gh run watch <id>
```

- **Qué corre:** 4 journeys (`evals/journeys/procedimientos/`: contratar plan, cambiar de plan antes de confirmar, pedido a domicilio, agendar una demo) × `k = 4` × 2 runtimes (sin motor y con motor) = 32 intentos, cada uno con cliente simulado y asistente contra el LLM real. Solo con el motor: `-f runtime=v3` (la mitad), pero el criterio compara contra sin motor.
- **Costo y duración:** 1 a 3 USD por corrida `ambos` (estimación; el workflow no registra el gasto, mídelo en el panel del proveedor del LLM y anótalo) y 20 a 35 minutos (corridas #11 a #13; el límite del job es 180). El workflow **no tiene tope de gasto propio**: lo acotan `k` y el límite de tiempo. El tope mensual por negocio (`EVAL_TOPE_USD_MENSUAL`, 10 USD) es del gate por release (`evaluar-release`, HU-AOS-21), no de este workflow.
- **Costo cero antes de lanzar:** `bash scripts/gate_evals.sh replay` (golden y journeys de suscripciones grabados, sin red ni clave; exige una base recién migrada y sembrada). **El set `procedimientos` no tiene grabaciones**: `journeys.py --set procedimientos --grabacion replay` falla cerrado (exit 3) y nunca cae al LLM real. Regrabarlo también gasta (`--grabacion record`, con OK).
- **Comparte el grupo de concurrencia `evals-nightly`:** no corre a la vez que el nightly ni otra corrida de este workflow.
- **Después:** valor y fecha en la bitácora de solvershub-docs (con el commit medido y el id de la corrida), y el HU-AOS-15 / HU-AOS-21 según el resultado.

Qué leer si no llega: el log del job (`≈ n/4` = inestable, con el motivo y el intento fallido). Un solo intento fallido baja el pass^4 a 75 % con 4 journeys: investiga la causa raíz (no re-corras hasta que salga).

> Límite conocido: esta medición es de **la plataforma** (sets de journeys del repo), no del golden set de cada negocio. HU-AOS-15 pide el pass^4 *por negocio*; hasta que exista una corrida por negocio, usa además el golden set del negocio
> (`evals-nightly`) y la observación de la § 3.3.

### 3.6 Encender el motor

Con § 3.2 a § 3.5 en verde: en el panel del master (Agent OS del negocio) o por la API (`PUT …/agent-os` con `procedures: true`, y `runtime`/`routing` como están). Antes, **republica los agentes** del negocio para que tomen la versión vigente de cada procedimiento.
Las conversaciones **nuevas** nacen con tareas; las abiertas pasan al motor al empezar su siguiente episodio (tras `episode.idleMinutes` sin mensajes).

Después: otra semana de observación con la bandera de reversa disponible (§ 4).

## 4. Apagar (orden inverso)

Mientras no se haya aplicado el *contract* (§ 5), todo se puede revertir:

1. **Motor:** panel o `PUT …/agent-os` con `procedures: false` → vuelve el flujo por prompt. Las tareas abiertas dejan de gobernarse.
2. **Runtime:** `agent-core db migrar-a-v3 --tenant <slug> --volver` (si `aos_procedures` sigue encendida pide `--apagar-procedimientos`). Vuelve a `legacy` y apaga también `aos_routing`.
3. **Ruteo por conexión:** `AOS_CONNECTION_ROUTING=false` y despliegue, **solo** con todos los negocios en `legacy`. El camino anterior reescribe `connection_id` y limpia `release_id`.

## 5. Contract (C1) — el punto sin retorno

Se hace **una sola vez**, cuando **todos** los negocios vivos están en `v3` y llevan al menos 2 releases estables sin rollback. Después, el camino anterior ya no existe: no se apaga `AOS_CONNECTION_ROUTING`.

> No es una migración de Alembic (correría sola en el siguiente despliegue): es el comando `agent-core db contract-aos`, en seco por defecto.

### 5.1 Precondiciones (las revisa el comando; si una falla, bloquea sin escribir nada)

1. `AOS_CONNECTION_ROUTING` encendida en este entorno.
2. Ningún negocio **vivo** en `aos_runtime = legacy`.
3. **Ninguna conversación sin `connection_id`**, de cualquier estado y también las de negocios dados de baja (las cubre `backfill-conexiones`; si son de un negocio dado de baja, purga el negocio).
4. Cada conexión conversacional viva con release activa y **2 releases estables sin rollback**: al menos dos promociones (sin contar las de un rollback) y ningún rollback posterior a la penúltima.
5. Confirmación explícita (`--confirmo-sin-vuelta-atras`).
6. **Requisito de despliegue que el comando no puede comprobar:** todo camino que cree una conversación debe asignarle una conexión. Eso ya está resuelto en el código (adenda 2026-10-24 de ADR-0115): el Centro de pruebas usa su conexión interna `playground` y, con `AOS_CONNECTION_ROUTING` encendida, el consumer asegura la conexión del número de la cuenta al primer mensaje.
   **Comprueba que ese código esté desplegado y que `backfill-conexiones` (paso 5) haya ligado las conversaciones de prueba de antes** (si no, la precondición 3 las cuenta y bloquea). Mira el log `conversacion_sin_conexion` de los últimos días: si aparece, hay un camino que todavía crea conversaciones sin conexión. El comando lo recuerda como aviso en cada corrida.

### 5.2 Orden

```bash
agent-core db contract-aos                      # en seco: precondiciones, pasos y bloqueos de TODOS los negocios
agent-core db contract-aos --tenant <slug>      # en seco: ¿este negocio está listo?
# …arregla los bloqueos, deja pasar la ventana de observación y haz un respaldo de la base…
# (el rol dueño va por MIGRATIONS_DATABASE_URL del entorno: evita --database-url, que deja la credencial en el historial)
agent-core db contract-aos --aplicar --confirmo-sin-vuelta-atras
```

Conviene una hora de poco tráfico. Qué hace al aplicar (cada paso es idempotente: si se corta, se vuelve a correr el mismo comando):

1. **Deduplica sin cerrar**: en cada (negocio, conexión, cliente) con más de una conversación abierta (`open`/`escalated`) queda primero la **escalada o con el bot pausado** (la atiende una persona) y, entre iguales, la de `started_at` más reciente;
   a las otras se les pasan los mensajes, se conserva la ventana de servicio más larga y se marcan con `deleted_at`. Se hace con el mismo candado por cliente que el consumer (un mensaje que entra a mitad no se pierde) y se salta el par que cambió desde el cálculo.
   Sus tareas, `task_events` y el ledger (`turn_id` de los mensajes movidos) no se mueven (append-only). `state` no se copia. No hay borrado físico.
2. Índice único parcial `uq_conversations_open_per_connection` (`CONCURRENTLY`; si un intento anterior lo dejó inválido, lo rehace).
3. `conversations.connection_id` **NOT NULL** (CHECK `NOT VALID` → `VALIDATE` → `SET NOT NULL`, sin escaneo bajo bloqueo exclusivo).
4. `core_decisions`: CHECK `ck_core_decisions_contract` — conexión obligatoria y, si el turno fue `routed`, release obligatoria — **solo para filas posteriores al contract** (el histórico `legacy` no impide validarlo).
5. `VALIDATE CONSTRAINT` de toda FK y CHECK `NOT VALID` de `public` (no solo las del Agent OS; el comando las lista en seco como aviso). El bloqueo no frena las escrituras; espera hasta 60 s a otro DDL.
6. Quita la FK simple `conversations_connection_id_fkey`, que duplica `fk_conv_connection`.

Si un `VALIDATE` falla (filas que violan el constraint), el comando sale con 1 y dice cuál: corrige o purga esas filas y vuelve a correrlo.

### 5.3 Qué NO hace (cambios de código con su propia revisión)

- Quitar `meta_asset_claims.mover_conversaciones`: solo cuando el sender y las guardas dejen de leer `conversations.meta_connection_id` (si no, tras una reconexión la conversación apuntaría a una cuenta sin números vivos).
- Quitar `web_chat_resolve` y borrar el secreto en claro de `app_settings.web_chat_config` (la conexión `webchat` pasa a ser la única fuente).
- `agent_specs` de solo lectura.
- Marcar `Conversation.connection_id` como no nulo en el modelo SQLAlchemy y limpiar el camino anterior de `consumer._resolve_conversation`.

## 6. Checklist de cierre de HU-AOS-15

- [ ] `AOS_CONNECTION_ROUTING` encendida en producción y los backfills (§ 3.1) hechos en todos los negocios.
- [ ] Cada negocio en `v3` con su pass^4 (§ 3.5) igual o mejor que antes; si no, quedó en `legacy`.
- [ ] `aos_procedures` encendida donde corresponde, agentes republicados y una semana de observación sin incidentes P1 atribuibles.
- [ ] *Contract* aplicado (§ 5) y los cambios de código de la § 5.3 abiertos como HU/PR.
- [ ] Valor y fecha del pass^4 en la bitácora de solvershub-docs.

## 7. Si algo sale mal

| Síntoma | Qué hacer |
|---|---|
| `migrar-a-v3` bloquea a un negocio | Lee el motivo: trae el comando que lo arregla (`backfill-conexiones`, `backfill-agentes`, `backfill-releases`, `--construir`) |
| Un negocio en `v3` responde distinto o peor | `migrar-a-v3 --tenant <slug> --volver` (§ 4); revisa la release activa y el rollback de la conexión |
| El motor dice «ya quedó registrada tu confirmación» sin pedido | El verificador de `contratar_plan` 1.3.0 lo bloquea; si aparece, revisa que el agente esté en la versión vigente (republicar) |
| pass^4 baja a 75 % con motor | Un intento fallido: lee el intento (`≈ n/4`), no promedies ni re-corras sin causa raíz |
| `contract-aos` falla en el índice único | Quedaron conversaciones abiertas repetidas creadas durante la corrida: vuelve a correr (deduplica de nuevo) |
| `contract-aos` falla en un `VALIDATE` | Hay filas que violan el constraint (por ejemplo una referencia a un turno que no existe): corrígelas o purga el negocio dueño y repite |

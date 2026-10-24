# Migración al Agent OS (runbook de HU-AOS-15)

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
agent-core db reporte-ruteo --tenant <slug> --dias 7     # meta de R3: precisión ≥ 0,90 y recall ≥ 0,85 durante 7 días
agent-core db ruteo --tenant <slug> --modo active
```

### 3.5 pass^4 antes de encender `aos_procedures`

La medición de la plataforma es el workflow **`pass^k de los procedimientos`** (`.github/workflows/evals-procedimientos.yml`, `workflow_dispatch`, runner propio): `k=4`, `runtime=ambos`, sobre la rama/commit que se va a desplegar.
Mide con y sin el motor los journeys de comercio, agenda, suscripciones y cobranza (cliente simulado + LLM real). Criterio (ADR-0112): **con motor, pass^4 igual o mejor que sin él** (hoy 100 %). Anota **valor y fecha en la bitácora de solvershub-docs**.

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

### 5.2 Orden

```bash
agent-core db contract-aos                      # en seco: precondiciones, pasos y bloqueos de TODOS los negocios
agent-core db contract-aos --tenant <slug>      # en seco: ¿este negocio está listo?
# …arregla los bloqueos, deja pasar la ventana de observación y haz un respaldo de la base…
agent-core db contract-aos --aplicar --confirmo-sin-vuelta-atras
```

Qué hace al aplicar (cada paso es idempotente: si se corta, se vuelve a correr el mismo comando):

1. **Deduplica sin cerrar**: en cada (negocio, conexión, cliente) con más de una conversación abierta (`open`/`escalated`) queda la de `started_at` más reciente; a las otras se les pasan los mensajes y se marcan con `deleted_at`.
   Sus tareas y `task_events` no se mueven (append-only). No hay borrado físico.
2. Índice único parcial `uq_conversations_open_per_connection` (`CONCURRENTLY`; si un intento anterior lo dejó inválido, lo rehace).
3. `conversations.connection_id` **NOT NULL** (CHECK `NOT VALID` → `VALIDATE` → `SET NOT NULL`, sin escaneo bajo bloqueo exclusivo).
4. `core_decisions`: CHECK `ck_core_decisions_contract` — conexión obligatoria y, si el turno fue `routed`, release obligatoria — **solo para filas posteriores al contract** (el histórico `legacy` no impide validarlo).
5. `VALIDATE CONSTRAINT` de toda FK y CHECK que el expand dejó `NOT VALID` (bloqueo que no frena las escrituras).
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

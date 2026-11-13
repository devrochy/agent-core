## Retiro del camino legacy, primera tanda (HU-AOS-69, ADR-0191, 2026-11-12)

- `AOS_CONNECTION_ROUTING` deja de existir: el ruteo por (conexión, cliente) de ADR-0115 es siempre. El consumer siempre busca la conversación por conexión y asegura la conexión del número; el alta, los presets v2, los datos de QA, el contract y `migrar-a-v3` ya no la exigen. Si la variable quedó cargada en Coolify no hace nada.
- Las claves reservadas de los agentes pasan a `core/agents/claves.py` y los procedimientos publicados del negocio a `core/agents/procedimientos_de_negocio.py`, fuera del lector tolerante, que se retira en la siguiente tanda.
- La API de plataforma sigue respondiendo `aos_connection_routing: true` hasta que se retire el campo junto con `runtime`.

### Segunda tanda
- Los negocios nacen en `aos_runtime = 'v3'` (migración 0126: valor por defecto, guardia de 0097 y `aos_fijar_banderas`, que ya no acepta `legacy`). El alta deja de hacer el paso «nacer en legacy y migrar».
- Sin vuelta a legacy: se retiran `agent-core db migrar-a-v3 --volver` y `--apagar-procedimientos`; el rollback es la release anterior de v3 o el respaldo.
- Sin el 403 `aos_runtime_legacy` en agentes, instancias por conexión, releases, habilidades por conexión y A2A.
- Antes de desplegar: confirmar que ningún negocio vivo sigue en `legacy` (`select slug from tenants where aos_runtime = 'legacy' and deleted_at is null`).

### Tercera tanda
- Release 1 al alta de la conexión (ADR-0191 §2): cuando una conexión conversacional nueva o revivida recibe sus instancias base, la misma transacción deja `connection.release_1` en el outbox y el handler del core construye y promueve su release 1 después del commit (idempotente; un error reintenta con el backoff del outbox).
- Sin release no hay turno (ADR-0191 §1): un negocio v3 sin release activa ya no responde con el AgentSpec v2, tampoco en la conexión interna del Centro de pruebas. El playground, sin conexión elegida, prueba un negocio v3 por su conexión conversacional con release activa (la principal primero).
- Configuración deja de editar agentes (decisión del dueño del 2026-11-13, ADR-0191 §4): en un negocio v3 el build ya no refresca el General desde el AgentSpec v2. Identidad, Voz y Cerebro quedan como datos del negocio para el contenido inicial de un agente nuevo; cada agente se edita en su ficha.
- Hacia el retiro del General (ADR-0192): el build falla `sin_especialista` si la conexión no tiene un especialista habilitado por defecto; el primer especialista habilitado que se agrega a una conexión sin agente por defecto queda por defecto; la release 1 se construye para toda conexión sin release activa (también la que solo tiene candidatas).

### Retiro del General, primera parte (HU-AOS-68, ADR-0192)
- El alta crea solo el Coordinador (`core/agents/base.py`: `KernelSpec` por defecto con la voz y el presupuesto del negocio, sin lector tolerante), antes de aplicar el preset. Sin General ni bandera de recorte.
- El primer especialista nace con la primera conexión conversacional (D-35): el handler de `connection.release_1` aplica los agentes del preset del negocio y construye la release 1; sin especialista posible no reintenta.
- Los especialistas del preset y del editor parten de la configuración del negocio (`contenido_base`) y nacen con los bindings de todas sus tools propias (su pack y las esenciales del core).
- La release 1 de un negocio v3 congela las versiones nativas de sus agentes, sin refrescar desde el AgentSpec v2.
- El especialista por defecto cuenta en el cupo `agentes` del plan (decisión del dueño, ADR-0192 §7): sale la excepción del General en la métrica y en `agregar_instancia`. Se retiran el recorte del General (`core/agents/coordinacion.py`, `agent-core db recorte-general`) y el acople de especialistas al General en el editor; `general_updated` de la publicación queda siempre en `null` hasta retirar el campo.

### Cuarta tanda: fuera el lector tolerante
- Se borran `core/agents/tolerante.py`, `core/agents/backfill_agentes.py` y `core/releases/migracion_v3.py`, con los comandos `agent-core db backfill-agentes` y `agent-core db migrar-a-v3`. El build y la release 1 ya no refrescan nada desde el AgentSpec v2 (`refrescar_desde_v2` y el parámetro `refrescar` salen).
- Una conexión conversacional nueva de un negocio que ya tiene agentes nace con el Coordinador y el especialista por defecto del negocio (el que es el por defecto en sus otras conexiones), en la misma transacción del alta; sin ninguno, queda pendiente y el preset se lo da por `connection.release_1`.
- El panel del master ya no corre `migrar-a-v3`: el runtime es siempre v3, `construir` no tiene efecto y pedir `legacy` es 422. «Preparar mis datos» asegura el Coordinador y sus instancias en lugar de los backfills.
- La exención de gate `migrar_a_v3` sale (no queda quien la use); `migrar_a_v3` sigue como motivo en el historial de eventos.

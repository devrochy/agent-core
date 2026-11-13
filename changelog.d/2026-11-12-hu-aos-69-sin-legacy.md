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

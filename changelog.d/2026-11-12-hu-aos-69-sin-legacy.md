## Retiro del camino legacy, primera tanda (HU-AOS-69, ADR-0191, 2026-11-12)

- `AOS_CONNECTION_ROUTING` deja de existir: el ruteo por (conexión, cliente) de ADR-0115 es siempre. El consumer siempre busca la conversación por conexión y asegura la conexión del número; el alta, los presets v2, los datos de QA, el contract y `migrar-a-v3` ya no la exigen. Si la variable quedó cargada en Coolify no hace nada.
- Las claves reservadas de los agentes pasan a `core/agents/claves.py` y los procedimientos publicados del negocio a `core/agents/procedimientos_de_negocio.py`, fuera del lector tolerante, que se retira en la siguiente tanda.
- La API de plataforma sigue respondiendo `aos_connection_routing: true` hasta que se retire el campo junto con `runtime`.

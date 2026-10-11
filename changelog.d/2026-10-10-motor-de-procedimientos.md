## Motor de procedimientos del Agent OS (2026-10-10)

- HU-AOS-05: motor de procedimientos en el kernel (`core/kernel/procedures/`), activo solo con
  `tenants.aos_procedures`. Con una tarea en foco, solo se ofrecen y ejecutan las acciones de su
  etapa (más las del core). Una acción fuera de la etapa se deniega con un error de negocio.
- Los datos se validan en el servidor (tipo, opciones, formato y el *resolver* de la habilidad),
  todo o nada.
- Lo que compromete espera la confirmación explícita del cliente, atada a los argumentos (o, en la
  salida de una etapa, a los valores de la tarea). El botón firmado `confirmar:<tarea>:<seq>:<firma>`
  confirma sin LLM. "dale" y "sí" a secas no confirman, y un sí escrito solo vale para la petición
  del turno anterior.
- La tarea se cierra por un evento de dominio del outbox que correlaciona por la clave de identidad
  del topic (`CLAVES_DE_CORRELACION`, p. ej. `reference` en `payment.confirmed`) con un dato fijado
  por una acción. Corre en un savepoint del despachador.
- Datos sin LLM: un botón o una fila de lista (con candidatos) y la media del mensaje llenan el
  dato de la tarea en foco desde el consumer. El parser de WhatsApp pasa el id del botón
  (`interactive_id`) al stream.
- Modo sombra con `aos_runtime = 'v3'` y `aos_procedures` apagada: el motor solo observa y
  registra las diferencias en `audit_log`, sin cambiar el prompt, las acciones ni la respuesta.
- API de plugins: `MCPHost.register_procedures` y `register_resolver`. El linter reserva el
  prefijo `confirmar:` y exige que el dueño de un evento de cierre sea el core, la habilidad o una
  de `requires`. Los fixtures de `procedure/1` cierran con `core.payment.confirmed`.
- La tarjeta de estado la arma el motor con las definiciones de los procedimientos (se retira
  `tarjeta_del_turno`).
- Adendas de ADR-0112 y ADR-0113.

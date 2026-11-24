## Tests que faltaban para cerrar HU-CRM-28 (2026-11-24)

- Dos agentes de Ventas del mismo negocio con políticas de datos distintas: cada uno exige lo suyo en la API y en la
  evaluación del turno.
- `crear_pedido` y el resolver `pack_commerce.metodo_de_entrega` rechazan un método de entrega apagado para el agente.
- 404 al leer o guardar la política de un agente de otro negocio.
- Script `scripts/migrar_datos_cliente_agentes.py`: el dry-run no escribe nada; `--apply` copia a cada especialista
  solo sus operaciones, conserva el aviso del negocio, deja auditoría y es idempotente.
- HU-AOS-63, «Precedencia»: la regla conexión › agente › negocio pasa a `packs_del_turno()` (misma conducta) y queda
  probada en `tests/test_precedencia_de_habilidades.py`.

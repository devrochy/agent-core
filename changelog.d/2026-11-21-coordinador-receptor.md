## Conexión atendida por el Coordinador y lead base siempre (2026-11-21, HU-AOS-88)

- La release de una conexión se arma aunque no tenga especialistas: lleva solo al Coordinador y él es el foco del turno (fuente `coordinador_receptor`, migración 0151). Con especialistas y ninguno por defecto, el build falla con `sin_especialista_por_defecto` (antes `sin_especialista`).
- Ningún alta crea especialistas: ni la primera conexión (`connection.release_1`) ni el preset del alta del negocio. El dueño crea su primer agente en la bienvenida; `agent-core db presets-v2` sigue como comando explícito.
- Al publicar el primer especialista en una conexión que atendía solo el Coordinador, su release se activa sola (`exento_primer_especialista`, una sola vez por conexión) y la conversación pasa a él en su siguiente turno. La respuesta de publicar suma `release_activated`.
- El lead base se asegura en toda conversación real, antes de los cortes por pausa, cuota o presupuesto, tenga o no el foco Captar leads. El aviso «Lead nuevo» sale solo con el Coordinador en foco o con Captar leads.
- Mientras no llegue la vista de recepción (HU-AOS-89), el turno con el Coordinador en foco sale en silencio. **Se despliega junto con HU-AOS-89.**

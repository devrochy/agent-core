## Cada agente con solo sus poderes (2026-10-26)

- HU-AOS-51, SPEC-39 (D-49). Un agente creado desde una plantilla ya no hereda tools de otro enfoque: las `sub_*`
  (que declara el core pero son de Suscripciones) solo llegan a un agente de Suscripciones, y la familia
  `cob_*_suscripcion` de Cobranza solo si Cobranza es el proveedor de suscripciones del negocio. Siguen en todos
  los especialistas las esenciales del core (escalar a humano, perfil del cliente, memoria). La regla vive en
  `herramienta_propia` (`core/presets/agentes.py`); `derivar_contenido` recibe `proveedor_suscripciones`.
  Solo afecta a agentes nuevos: los ya publicados no se republican.
- `GET /api/v1/admin/agent-templates` suma `section` (sección del menú: Ventas, Agenda, Cobranza, Suscripciones)
  y `tools` (tools de pack que tendrá el agente). `PlantillaAgente` suma `seccion`.
- `POST /api/v1/admin/agents/{id}/versions` también respeta el enfoque del agente: un superpoder fuera de su
  plantilla responde 422 `superpoder_fuera_de_enfoque` junto al campo, igual que el editor (el CLI sigue sin la
  guarda).

### Agregado

- Habilidades instaladas por agente (ADR-0190, HU-AOS-61): `pack_installations` acepta `scope='agent'` con `agent_id` (migración 0125), `core/agents/instalaciones_agente.py` instala, quita y configura (cifrado con AAD por instalación), y `plugin_manager.load_config(pack, agent_id=…)` combina la config del negocio con la del agente.
- `BaseModule.tipo` (`especialidad` | `transversal`) y `BaseModule.config_agente`; `config_de_agente()` arma el esquema del agente desde el del pack. Ventas, Agenda y Suscripciones declaran los campos que son de cada agente.
- La release congela en cada componente las habilidades activas del agente (`skills`, solo ids) y el turno v3 ofrece solo las acciones de esas habilidades (HU-AOS-63). Un agente sin instalaciones propias hereda las de la release hasta la resiembra (HU-AOS-65).
- Crear un agente desde una plantilla instala sus habilidades en ese agente (ADR-0190 reemplaza ADR-0173).

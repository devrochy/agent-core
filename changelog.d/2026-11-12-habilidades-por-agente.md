### Agregado

- Habilidades instaladas por agente (ADR-0190, HU-AOS-61): `pack_installations` acepta `scope='agent'` con `agent_id` (migración 0125), `core/agents/instalaciones_agente.py` instala, quita y configura (cifrado con AAD por instalación), y `plugin_manager.load_config(pack, agent_id=…)` combina la config del negocio con la del agente.
- `BaseModule.tipo` (`especialidad` | `transversal`) y `BaseModule.config_agente`; `config_de_agente()` arma el esquema del agente desde el del pack. Ventas, Agenda y Suscripciones declaran los campos que son de cada agente.

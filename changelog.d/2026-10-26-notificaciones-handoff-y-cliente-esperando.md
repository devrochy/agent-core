## Avisos de handoff y «cliente esperando respuesta» (ADR-0167)

- **Handoff:** tomar una conversación escalada (la primera vez) y resolverla o devolverla al agente avisan al equipo con acceso a Escalaciones (categoría Conversaciones). Quien hizo la acción no recibe el push y el aviso le nace leído.
- **Cliente esperando:** un job cada 5 min avisa (warning, con push inmediato) cuando un cliente con escalación abierta lleva más de 15 min sin respuesta humana. Un aviso por episodio; si la escalación está asignada, va dirigido a esa persona. Ignora episodios de más de 24 h.
- Sin migración ni cambio de contrato de API: los clientes (panel y app) ya muestran la categoría Conversaciones.

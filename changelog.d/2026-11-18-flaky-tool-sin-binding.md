## Tests

- `tests/test_tool_sin_binding_v3.py` deja de depender del orden de la suite: los tres tests que miraban el evento `tool_sin_binding` con `capture_logs` ahora sustituyen el logger del loop por uno que anota (mismo patrón que `test_leads_follow_up_sin_config.py`). Con `cache_logger_on_first_use` (que activa `configure_logging` al importar `core.main`), el logger del loop quedaba atado a la lista de procesadores de su primer uso y un test que configuraba una lista nueva en el mismo worker de xdist lo dejaba fuera de `capture_logs`.

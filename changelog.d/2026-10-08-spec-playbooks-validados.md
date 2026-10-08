## El primer turno tras sumarse un playbook de pack ya no se cae (2026-10-08)

- `_ensure_default_playbooks` agregaba los playbooks faltantes con `model_copy(update=...)`, que no valida: el
  AgentSpec devuelto traía dicts y el turno se caía en `load_playbook` (`'dict' object has no attribute 'name'`).
  Pasaba en el primer mensaje de un negocio después de habilitar un pack (o de que un pack sumara un playbook);
  la fila sí quedaba bien, así que el siguiente turno funcionaba. Ahora el spec se valida. Visto con los journeys
  de HU-SUB-12.

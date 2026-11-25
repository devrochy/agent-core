## El flujo del Coordinador sin borrador ya no da 500 (2026-11-25)

- `GET /api/v1/admin/agents/{id}/flow` y `GET .../flow/options` daban 500 para un Coordinador sin borrador (el sembrado por
  el alta no lo tiene). Ahora responden 200 con un flujo vacío de semilla (`lock_version: 0`), sin escribir nada; guardarlo
  (`PUT`) sigue dando 409 `coordinador_inmutable`. Un especialista sin borrador da 404 en lugar de 500. El contrato no cambia.

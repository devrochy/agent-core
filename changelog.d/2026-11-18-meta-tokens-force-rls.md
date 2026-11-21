## RLS forzada en `meta_private.meta_tokens` (2026-11-18)

- Migración `0148`: `FORCE ROW LEVEL SECURITY` en `meta_private.meta_tokens`. La `0018` solo la habilitó y la `0029` forzó únicamente `public`, así que el rol dueño leía los tokens sin la policy (invariante 1). Con esto `scripts/verificar_aislamiento.py` pasa `rls_otros_esquemas` y se cierra el pendiente de la fase 0 de `docs/runbooks/produccion-vps.md`. Nada cambia para `app_runtime`.
- Test de integración (`-m aislamiento`): ninguna tabla fuera de `public` queda con RLS sin FORCE.

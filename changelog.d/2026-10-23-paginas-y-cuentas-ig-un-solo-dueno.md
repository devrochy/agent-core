## Una Página de Facebook o una cuenta de Instagram pertenecen a un solo negocio (ADR-0152, HU-CHAN-01) (2026-10-23)

Primer paso del rescate de Messenger e Instagram: cerrar, antes de que existan altas, el mismo defecto que
ADR-0095 corrigió para la WABA y el número.

- Migración `0104`: `resolve_tenant_for_page` y `resolve_tenant_for_ig_user` cuentan los negocios que
  reclaman el activo (solo conexiones vivas, no borradas y fuera de `draft`) y devuelven NULL con cero o con
  más de uno, en vez de `LIMIT 1` sin orden. Nueva `meta_messaging_asset_claimed_by_other_tenant`. Las tres
  fijan `search_path`, sin EXECUTE para PUBLIC. La migración aborta si el rol no puede saltar RLS y lista en
  el log los activos que hoy tienen dos dueños (hoy nada escribe en `fb_pages` ni `ig_accounts`).
- `webhook_route_tenant.py`: un evento `page` o `instagram` cuyo activo reclama más de un negocio queda como
  conflicto (`tenant_conflict`) y no va a ninguno, como con la WABA.
- `meta_asset_claims.py`: `claim_messaging_assets` (candado por activo en la misma transacción del insert) y
  `messaging_asset_claimed_by_other_tenant`, para las altas de Messenger e Instagram que vienen después.
- ADR-0152 pasa a aceptado. Sin cambios de API ni de contratos.

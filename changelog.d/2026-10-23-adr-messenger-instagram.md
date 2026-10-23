## ADR-0152: Messenger e Instagram con salida, portados sobre la conexión canónica (2026-10-23)

- Propone rescatar el código de `feat/messenger-connection` y `feat/instagram-connection` (PRs #45 y #46, nunca
  llegaron a `develop`) portándolo en dos entregas: Messenger (HU-CHAN-01) e Instagram (HU-CHAN-04).
- Documenta el defecto de propiedad de Páginas y cuentas de Instagram que hay hoy (`resolve_tenant_for_page` e
  `…_for_ig_user` con `LIMIT 1` sin orden) y la corrección previa a abrir la salida. Sin cambios de código.

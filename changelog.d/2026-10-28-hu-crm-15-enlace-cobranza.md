## Cobranza: el deudor cargado a mano se reconoce por teléfono (2026-10-28)

- HU-CRM-15 (SPEC-33, ADR-0179 §7). `Cliente.customer_id` ahora se enlaza con el `Customer` de WhatsApp cuando
  coinciden los dígitos del teléfono (`pack_cobranza/enlazar.py`): antes ningún código lo asignaba y un deudor
  creado en el panel o en la app del gestor no lo encontraban las tools `cob_*` cuando escribía por WhatsApp.
- Tres entradas: perezoso en `_cliente_de_customer` (caché negativo de 10 min en Redis; si Redis falla se
  intenta igual), al crear el cliente (panel, app del gestor y alta SSR; el teléfono se guarda en E.164) y el
  backfill `python -m pack_cobranza.enlazar [--dry-run] [--tenant] [--desde-id] [--lote]`, idempotente y reanudable.
- Una coincidencia enlaza con `UPDATE … WHERE customer_id IS NULL`; dos deudores con el mismo teléfono no se
  enlazan (`ambiguo`) y un `Customer` ya enlazado no se reasigna. El log `cobranza_enlace{resultado, origen}`
  no lleva teléfono, documento ni nombre.
- Limitaciones: solo se enlazan Customers de WhatsApp (Instagram, web y otros canales no), y un número local
  solo cruza con un `wa_id` internacional si es del país por defecto (CO; aún no hay país por negocio).

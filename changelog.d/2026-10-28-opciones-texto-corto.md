## Opciones directas: el texto corto ya no paga la bandera (2026-10-28)

- SPEC-40 · C2 (seguimiento del arquitecto sobre #624). La bandera `opciones_directas.habilitada` sale de la fila de
  `tenants` que `handle_inbound` ya lee para el gate «negocio operativo» (`tenant_operativo`, `cargar_tenant_activo`),
  en vez de abrir una sesión y consultar `tenants.settings` por cada texto corto en web, QR, Messenger e Instagram.
  Bandera apagada: cero consultas extra por mensaje (antes una sesión). Encendida y el último saliente no es un menú:
  una consulta (antes dos). Sin caché ni TTL: apagar la bandera rige desde el siguiente mensaje y no hay estado entre
  negocios. `is_tenant_active` e `is_tenant_operational` conservan firma y semántica. Sin migración ni cambio de API.

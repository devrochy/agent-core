## El horario de atención documenta su índice de días: 0=lunes … 6=domingo (2026-10-18)

- API de Configuración: `IdentitySettings.business_ranges` e `IdentityUpdate.business_ranges` describen en el
  openapi que `day` va de 0=lunes a 6=domingo, el criterio de `within_business_hours` (ADR-0051). El panel y la
  app mostraban «0=Dom…6=Sáb», así que un horario cargado como lunes se aplicaba el martes; se corrigen en sus
  repos. El backend no cambia de convención ni de comportamiento.
- Tests: ida y vuelta de «lunes 08:00–18:00» por `PUT /api/v1/admin/settings/identity`, que el runtime abre el
  lunes y no el martes ni el domingo.

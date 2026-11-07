## Retirar los datos de un cliente es solo del propietario o el master (2026-11-06)

- agent-core#692. `POST /admin/leads/{id}/consentimiento-datos/revocar` aceptaba a un colaborador con Clientes en escritura aunque la API dice «Solo dueño o master»: ahora responde 403 «Solo el propietario del negocio puede retirar los datos de un cliente.» antes de leer o cambiar nada. Propietario y admin de plataforma siguen igual; ver el estado (`GET …/consentimiento-datos`) sigue siendo de lectura. Sin cambios en el contrato (`docs/openapi.json`) ni migración.

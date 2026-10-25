## Avisos de cobro en horario legal (HU-SUB-18, Ley 2300 de 2023)

Los avisos de cobro de pack-subscriptions (cargo nuevo, recordatorios, pausa por mora, reenvío del panel) y la oferta de fin de prueba solo salen de lunes a viernes de 7:00 a 19:00 y los sábados de 8:00 a 15:00, en la zona horaria del negocio, nunca domingos ni festivos de Colombia, y como máximo uno por cliente y día.

- `avisos_de_cobro` corre cada hora y salta los negocios fuera de su ventana. Lo que no cabe hoy sale el siguiente día hábil.
- El aviso del cargo nuevo ya no sale a las 22:15: `generar_cargos` lo deja en el outbox con `entregar_desde` en la próxima ventana legal, en un día en que el cliente no tenga otro aviso de cobro. `emit_domain` acepta `entregar_desde`.
- Configuración nueva `ventana_cobro` (se puede achicar, nunca ampliar) y `pais_festivos`. Los festivos de Colombia se calculan con la Ley Emiliani.
- Migración `pack_subscriptions_0009`: registro `pack_subscriptions_avisos_cobro` (un aviso por cliente y día, RLS forzada).
- El reenvío manual del link desde el panel responde 409 fuera de horario o si el cliente ya tuvo su aviso del día.

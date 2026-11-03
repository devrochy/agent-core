## El reenganche de leads vuelve a enviar con la configuración por defecto (2026-11-02)

- `pack_commerce.follow_up_leads` pedía leads con actividad más vieja que `follow_up_hours` **y** dentro de las
  últimas 24 h (ventana de WhatsApp). Con `follow_up_hours = 24` (el default) las dos cotas coincidían: el cron
  corría cada día a las 9:00 y no enviaba nada. Los tests usaban `follow_up_hours = 1` y no lo veían.
- `follow_up_hours` pasa a 4 por defecto. Una espera que no cabe en la ventana (0, o 24 o más: una config
  guardada con el valor viejo) se lee como el valor por defecto en vez de dejar el seguimiento muerto. El rango
  del esquema (`le=8760`) no cambia, para que las configuraciones ya guardadas con 24 sigan cargando.
- Un lead con actividad hace más de 24 h no se reengancha (no se puede mandar texto libre).
- Test de integración `test_pack_commerce_reenganche_leads_ventana.py`.

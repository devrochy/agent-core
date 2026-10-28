### Corregido

- Elegir dos plantillas a la vez en un negocio sin habilidades ya no deja un agente sin su habilidad ni
  responde `template_unavailable`: `asegurar_habilidades` leía el spec, y si la otra petición confirmaba su
  versión justo después, la segunda guardaba encima y borraba la habilidad de la primera. `save_spec_version`
  acepta `base_id` (guarda solo si la fila de la que se partió sigue activa) y la habilitación lo usa; quien
  pierde relee y reintenta, o responde el 409 `concurrent_edit` reintentable de siempre.

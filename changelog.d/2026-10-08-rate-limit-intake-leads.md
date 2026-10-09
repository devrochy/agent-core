## El rate limit del intake de leads es por tenant y vive en Redis (2026-10-08)

- `POST /api/pack/pack_commerce/leads/{slug}` limitaba por IP con un dict del proceso compartido por todos los
  tenants: una IP que mandaba leads a un tenant gastaba el cupo de los demás, y las IPs nunca se borraban (el dict
  crecía sin límite). Ahora el cupo (30 por minuto) es por `(slug, IP)`, en un sorted set de Redis con ventana
  deslizante y TTL de la ventana: las IPs inactivas se borran solas y el límite vale para todas las instancias.
  El set guarda como mucho el cupo + 1, así que una inundación no infla Redis. La IP sale de `client_ip()` (la
  real detrás de proxies de confianza, `TRUSTED_PROXY_CIDRS`), no la del proxy, que juntaba a todos los clientes
  en un solo cupo. La respuesta sigue siendo 429 `rate_limited`. Si Redis no responde, deja pasar (el secreto
  del intake sigue siendo la barrera). Señalado por el auditor multitenant.

## El rate limit del chat web es por widget y vive en Redis (2026-10-08)

- `POST /api/v1/public/web-chat/{key}/messages` limitaba por IP con un dict del proceso compartido por todos los
  tenants: una IP que escribía a un widget gastaba el cupo de los demás, y las IPs nunca se borraban (el dict
  crecía sin límite). Ahora el cupo (30 por minuto) es por `(key del widget, IP)`, en un sorted set de Redis con
  ventana deslizante y TTL de la ventana: las IPs inactivas se borran solas y el límite vale para todas las
  instancias. El set guarda como mucho el cupo + 1, así que una inundación no infla Redis, y una `key` con forma
  inválida cae en un cupo común en vez de crear claves arbitrarias. La IP sale de `client_ip()` (la real detrás
  de proxies de confianza, `TRUSTED_PROXY_CIDRS`), no la del proxy, que juntaba a todos los visitantes de todos
  los tenants en un solo cupo; esa misma IP es la que se manda a Turnstile. La respuesta sigue siendo 429
  `rate_limited`. Si Redis no responde, deja pasar (el `Origin` y Turnstile siguen siendo barreras).

## Un solo rate limit en el core, con cupo de claves inexistentes por IP (2026-10-08)

- El widget del chat web y el intake de leads de pack-commerce tenían el mismo limitador copiado. Ahora los dos
  usan `core/security/rate_limit.py`: ventana deslizante en un sorted set de Redis, tope de tamaño, TTL de la
  ventana y fail-open si Redis no responde. Cada ruta solo arma su clave; el cupo por (widget o slug, IP) no
  cambia (30 por minuto).
- Una `key` del widget o un `slug` al azar en cada request abría un cupo nuevo cada vez, así que esos 404 no
  tenían límite y cada uno consultaba la base. Ahora cada 404 por clave inexistente suma en un cupo de fallos por
  IP (20 por minuto): con ese cupo lleno, la IP recibe 429 `rate_limited` antes de resolver el tenant. Solo
  cuentan los fallos, y no frenan a las claves que se sabe que existen: cada resolución exitosa anota la `key` o
  el `slug` como conocido en Redis por 15 minutos. Así, tenants que comparten IP (hosting compartido, CGNAT de los
  operadores móviles) no se cortan entre sí, ni con tráfico legítimo ni porque uno mande claves inexistentes.
  Aplica a `POST /api/v1/public/web-chat/{key}/messages` y a
  `POST /api/pack/pack_commerce/leads/{slug}`.
- El log de Redis caído pasa a ser `rate_limit_unavailable` con `name` (`web_chat` o `lead_intake`), en vez de
  `web_chat_rate_limit_unavailable` y `lead_intake_rate_limit_unavailable`.

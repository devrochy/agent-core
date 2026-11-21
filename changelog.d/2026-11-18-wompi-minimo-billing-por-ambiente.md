## Mínimo de Wompi del cobro de plataforma según el ambiente (2026-11-18)

- `core/billing/providers/wompi.py` ya no fija 150.000 COP: usa 150.000 en sandbox y 1.500 en producción, igual que el pago en línea. Un plan de producción bajo 150.000 ya no se rechaza antes de llegar a Wompi.
- El mínimo por ambiente pasa a `core/payments/wompi_minimo.py`; `pack_payments` lo importa de ahí (mismos nombres) en vez de tener su copia.

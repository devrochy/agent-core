## Replay de evals dentro de `integracion` (2026-10-28)

- El job `evals-replay` desaparece: el gate (12 s) ocupaba un hueco de runner entero por ~190 s. Ahora es el último paso de `integracion`, contra la base plantilla que los workers de xdist no tocan. Adenda 4 de ADR-0120.

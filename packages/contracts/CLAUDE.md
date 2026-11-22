# agentcore-contracts

Contratos congelados v4.0.0 en Python (ADR-0011; major a v2.0.0 por ADR-0050; `procedure/1`
aditivo en v2.1.0 y major a v3.0.0 con `agentspec/3` y `kernelspec/1`, ambos por ADR-0113; major a
v4.0.0 al salir el `AgentSpec` v2, ADR-0206 §8). `AgentSpecV3` y `KernelSpec` son los contratos del
Agent OS. Solo Python: el espejo TS se retiró en 3.0.0.
No importa de ningún otro paquete.
Cualquier cambio de forma requiere un ADR nuevo y un incremento major. Excepción: un campo
**opcional con default** nuevo en un modelo v3 existente es un **minor** (adenda de ADR-0113
del 2026-10-20; primer caso, `AgentSpecV3.knowledge` en 3.1.0 por ADR-0145; `ExposedQuery.action` en 3.2.0 por la adenda HU-AOS-19).

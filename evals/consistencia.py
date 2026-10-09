"""Consistencia pass^k de los evals (HU-ARCH-28, ADR-0119).

Cada escenario se corre k veces. Por escenario se guarda:
- **pass@1**: fracción de intentos que pasaron (la tasa de éxito esperada de un intento);
- **pass^k**: 1 si pasaron los k intentos, 0 si no;
- **inestable**: pasó en algunos intentos y falló en otros, con lo que cambió entre ellos.

En total, pass@1 es el promedio de los escenarios y pass^k la fracción de escenarios que
pasaron los k intentos. El gate del Hito 0A no cambia: se evalúa sobre pass@1 (con k = 1
es exactamente la tasa de antes) y pass^k se reporta junto a él.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class Intento:
    ok: bool
    detalle: str = ""
    herramientas: list[str] = field(default_factory=list)
    aserciones: list[dict[str, Any]] = field(default_factory=list)  # esperado/real por aserción
    simulador: dict[str, Any] | None = None  # semilla, turnos y cómo terminó
    segundos: float = 0.0


@dataclass
class ResultadoEscenario:
    nombre: str
    intentos: list[Intento] = field(default_factory=list)

    @property
    def k(self) -> int:
        return len(self.intentos)

    @property
    def pasaron(self) -> int:
        return sum(i.ok for i in self.intentos)

    @property
    def pass_1(self) -> float:
        return self.pasaron / self.k if self.k else 0.0

    @property
    def pass_k(self) -> float:
        return 1.0 if self.k and self.pasaron == self.k else 0.0

    @property
    def inestable(self) -> bool:
        return 0 < self.pasaron < self.k

    @property
    def ok(self) -> bool:
        """Para el reporte de una línea: con k = 1 es el resultado del intento."""
        return self.pass_k == 1.0

    def atribucion(self) -> str:
        """Qué cambió entre los intentos que pasaron y los que fallaron."""
        if not self.inestable:
            return ""
        fallidos = [i for i in self.intentos if not i.ok]
        buenos = [i for i in self.intentos if i.ok]
        en_buenos = Counter(h for i in buenos for h in set(i.herramientas))
        en_fallidos = Counter(h for i in fallidos for h in set(i.herramientas))
        faltaron = sorted(h for h in en_buenos if en_buenos[h] == len(buenos) and en_fallidos[h] < len(fallidos))
        sobraron = sorted(h for h in en_fallidos if en_fallidos[h] and h not in en_buenos)
        partes = [f"falló {len(fallidos)} de {self.k}"]
        if faltaron:
            partes.append(f"en los fallidos faltó {', '.join(faltaron)}")
        if sobraron:
            partes.append(f"en los fallidos apareció {', '.join(sobraron)}")
        motivos = Counter(i.detalle for i in fallidos if i.detalle)
        if motivos:
            partes.append("motivo: " + " | ".join(f"{m} ({n} veces)" for m, n in motivos.most_common(2)))
        return "; ".join(partes)

    def detalle(self) -> str:
        if self.inestable:
            return "INESTABLE: " + self.atribucion()
        fallidos = [i.detalle for i in self.intentos if not i.ok and i.detalle]
        return fallidos[0] if fallidos else ""


@dataclass
class Resumen:
    k: int
    escenarios: int
    pass_1: float
    pass_k: float
    inestables: list[str]


def resumir(resultados: list[ResultadoEscenario], k: int) -> Resumen:
    n = len(resultados) or 1
    return Resumen(
        k=k,
        escenarios=len(resultados),
        pass_1=sum(r.pass_1 for r in resultados) / n,
        pass_k=sum(r.pass_k for r in resultados) / n,
        inestables=[r.nombre for r in resultados if r.inestable],
    )


def linea(resultado: ResultadoEscenario) -> str:
    """`✓ PASS  nombre` (k = 1) o `✓ 4/4  nombre` / `≈ 3/4  nombre — INESTABLE: …`."""
    if resultado.k == 1:
        estado = "✓ PASS" if resultado.ok else "✗ FAIL"
    else:
        marca = "✓" if resultado.ok else ("≈" if resultado.inestable else "✗")
        estado = f"{marca} {resultado.pasaron}/{resultado.k}"
    detalle = resultado.detalle()
    return f"  {estado}  {resultado.nombre}" + (f"  — {detalle}" if detalle else "")


def texto_resumen(nombre: str, resumen: Resumen, umbral: float | None = None) -> str:
    base = f"{nombre}: {resumen.escenarios} escenarios · pass@1 {resumen.pass_1:.0%}"
    if resumen.k > 1:
        base += f" · pass^{resumen.k} {resumen.pass_k:.0%}"
        if resumen.inestables:
            base += f" · inestables: {', '.join(resumen.inestables)}"
    if umbral is not None:
        base += f" · umbral {umbral:.0%}"
    return base


def reporte(nombre: str, resultados: list[ResultadoEscenario], k: int, simulador: dict[str, Any] | None = None) -> dict[str, Any]:
    resumen = resumir(resultados, k)
    return {
        "set": nombre,
        "fecha": datetime.now(UTC).isoformat(),
        "k": k,
        "pass_1": round(resumen.pass_1, 4),
        f"pass_{k}": round(resumen.pass_k, 4),
        "pass_k": round(resumen.pass_k, 4),
        "inestables": resumen.inestables,
        "simulador": simulador,
        "escenarios": [
            {
                "nombre": r.nombre,
                "pass_1": round(r.pass_1, 4),
                "pass_k": r.pass_k,
                "inestable": r.inestable,
                "atribucion": r.atribucion(),
                "intentos": [asdict(i) for i in r.intentos],
            }
            for r in resultados
        ],
    }


def escribir_reporte(ruta: str, datos: dict[str, Any]) -> None:
    destino = Path(ruta)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(datos, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


async def persistir(
    tenant_id: uuid.UUID,
    spec_version: str | None,
    git_ref: str | None,
    resultados: list[ResultadoEscenario],
    k: int,
    simulador: dict[str, Any] | None = None,
) -> None:
    """`eval_runs` + `eval_results` con pass@1, pass^k, inestabilidad y aserciones."""
    from core.db.models import EvalResult, EvalRun
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    resumen = resumir(resultados, k)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            run = EvalRun(
                tenant_id=tenant_id,
                spec_version=spec_version,
                git_ref=git_ref,
                passed=sum(r.ok for r in resultados),
                total=len(resultados),
                k=k,
                pass_1=round(resumen.pass_1, 4),
                pass_k=round(resumen.pass_k, 4),
                simulator=simulador,
            )
            session.add(run)
            await session.flush()
            for r in resultados:
                session.add(
                    EvalResult(
                        tenant_id=tenant_id,
                        run_id=run.id,
                        scenario=r.nombre,
                        ok=r.ok,
                        detail=r.detalle()[:2000],
                        k=r.k,
                        pass_1=round(r.pass_1, 4),
                        pass_k=r.pass_k,
                        inestable=r.inestable,
                        state_assertions=[
                            {"intento": n + 1, "ok": i.ok, "aserciones": i.aserciones}
                            for n, i in enumerate(r.intentos)
                            if i.aserciones
                        ]
                        or None,
                        simulator={"intentos": [i.simulador for i in r.intentos if i.simulador]} if any(i.simulador for i in r.intentos) else None,
                    )
                )

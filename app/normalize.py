"""
Normalización de datos crudos de ScadaVision.

Puerto fiel de la lógica que vivía en el front (normFeeder / normGen /
computeTotals / busSummary). Es la ÚNICA fuente de verdad sobre cómo se
interpretan los campos crudos del SCADA; tanto el poller como cualquier
consumidor de la base deben pasar por acá.

Notas de unidades (idénticas al front):
  - /api/protection  -> p, q ya vienen en MW / MVAr.
  - /api/genprotection -> p, q, s vienen en W / VA CRUDOS (se dividen por 1e6).
  - datetime de alarmas: ms desde la época del relé 01/01/2000.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Iterable

# Época del relé: 01/01/2000 UTC (los datetime de alarma son ms desde acá).
EPOCH_2000 = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _avg(values: Iterable[float]) -> float:
    vals = [v for v in values]
    return sum(vals) / len(vals) if vals else 0.0


def relay_datetime(ms: float) -> datetime:
    """Convierte ms desde la época del relé (2000) a datetime UTC."""
    return EPOCH_2000 + timedelta(milliseconds=ms)


def norm_feeder(f: dict[str, Any]) -> dict[str, Any]:
    """Normaliza una celda de MT (/api/protection)."""
    seca = bool(f.get("seca_cerrado"))
    secb = bool(f.get("secb_cerrado"))
    if seca and secb:
        bus = "AB"
    elif seca:
        bus = "A"
    elif secb:
        bus = "B"
    else:
        bus = None

    if f.get("int52_extraido"):
        state = "extraida"
    elif f.get("pat_cerrado"):
        state = "a_tierra"
    elif f.get("int52_indefinido"):
        state = "indefinida"
    elif f.get("int52_cerrado"):
        state = "en_servicio"
    else:
        state = "abierta"

    alarm_list = f.get("alarmList") or []
    alarms_active = [a for a in alarm_list if a.get("alarm_active")]

    ir, is_, it = f.get("ir", 0), f.get("is", 0), f.get("it", 0)
    vrs, vst, vtr = f.get("vrs", 0), f.get("vst", 0), f.get("vtr", 0)

    fid = f.get("id")
    kind = "acople" if fid == "AC" else ("aux" if f.get("mode") == 2 else "alim")

    return {
        "id": fid,
        "name": f.get("nombre_Alimentador") or None,
        "kind": kind,
        "bus": bus,
        "state": state,
        "secA": seca,
        "secB": secb,
        "closed": bool(f.get("int52_cerrado")),
        "grounded": bool(f.get("pat_cerrado")),
        "out": bool(f.get("int52_extraido")),
        "i": [ir, is_, it],
        "imax": max(ir or 0, is_ or 0, it or 0),
        "vll": _avg([v for v in (vrs, vst, vtr) if v and v > 1]),
        "p": f.get("p", 0) or 0,
        "q": f.get("q", 0) or 0,
        "fp": f.get("fPmed", 0) or 0,
        "fr": f.get("fr", 0) or 0,
        "connected": f.get("connection_State") == 0,
        "alarms": alarm_list,
        "alarmsActiveCount": len(alarms_active),
        "alarmsUnacked": len([a for a in alarms_active if not a.get("alarm_ack")]),
    }


def norm_gen(g: dict[str, Any]) -> dict[str, Any]:
    """Normaliza un turbogenerador (/api/genprotection)."""
    p_mw = (g.get("p", 0) or 0) / 1e6
    q_mvar = (g.get("q", 0) or 0) / 1e6
    s_mva = (g.get("s", 0) or 0) / 1e6

    i_l = _avg([g.get(k, 0) or 0 for k in ("irl", "isl", "itl")])
    i_n = _avg([g.get(k, 0) or 0 for k in ("irn", "isn", "itn")])
    i_ref = max(i_l, i_n)

    bus = "A" if g.get("seca_cerrado") else ("B" if g.get("secb_cerrado") else None)

    # El pie de maquina (intPie) es la condicion fisica de acople: si esta
    # ABIERTO, la maquina esta desacoplada y su potencia DEBE ser 0, sin
    # importar lo que midan los TI. El SCADA a veces reporta valores espurios
    # de P/Q con la maquina parada; se fuerzan a 0 aqui, en origen, para que
    # esos picos falsos no lleguen a la base ni al grafico.
    foot_closed = bool(g.get("intPie_cerrado"))
    if not foot_closed:
        p_mw = 0.0
        q_mvar = 0.0
        s_mva = 0.0

    running = foot_closed and bool(g.get("int52_cerrado")) and (i_ref > 5 or p_mw > 0.5)

    # Discrepancia entre juegos de TI línea/neutro con la máquina en marcha:
    # en operación sana ambos juegos deben medir casi lo mismo.
    ti_mismatch = running and abs(i_l - i_n) > 0.25 * max(i_l, i_n, 1)

    prot = [g.get(f"estProt_{i}", 0) or 0 for i in range(1, 5)]
    prot_bits = []
    for wi, word in enumerate(prot):
        for b in range(16):
            if word & (1 << b):
                prot_bits.append(f"P{wi + 1}·b{b}")

    vphase = _avg([v for v in (g.get("vr"), g.get("vs"), g.get("vt")) if v and v > 1]) / 1000.0

    return {
        "id": g.get("id"),
        "bus": bus,
        "running": running,
        "closed": bool(g.get("int52_cerrado")),
        "out": bool(g.get("int52_extraido")),
        "pMW": p_mw,
        "qMVAr": q_mvar,
        "sMVA": s_mva,
        "fp": g.get("fPmed", 0) or 0,
        "fr": g.get("fr", 0) or 0,
        "iL": i_l,
        "iN": i_n,
        "iRef": i_ref,
        "tiMismatch": ti_mismatch,
        "vphase": vphase,
        "intPie": bool(g.get("intPie_cerrado")),
        "intCe": bool(g.get("intCe_cerrado")),
        "ct": {"f": g.get("ctRf"), "n": g.get("ctRn")},
        "vt": {"f": g.get("vtRf"), "n": g.get("vtRn")},
        "protBits": prot_bits,
        "connected": g.get("connection_State") == 0,
    }


def compute_totals(gens: list[dict], feeders: list[dict]) -> dict[str, Any]:
    """Totales agregados del sistema."""
    gen_mw = sum(g["pMW"] for g in gens)
    alim = [f for f in feeders if f["kind"] == "alim"]
    aux = [f for f in feeders if f["kind"] == "aux"]
    dist_mw = sum(f["p"] for f in alim)
    aux_mw = sum(f["p"] for f in aux)
    live = [f for f in feeders if f["closed"] and f["vll"] > 1]
    freq = _avg([f["fr"] for f in live if f["fr"] > 40])
    vll = _avg([f["vll"] for f in live if f["vll"] > 1])
    fp_med = _avg([f["fp"] for f in alim if f["closed"] and f["p"] > 0.05])
    return {
        "genMW": gen_mw,
        "distMW": dist_mw,
        "auxMW": aux_mw,
        "diff": gen_mw - dist_mw - aux_mw,
        "freq": freq,
        "vll": vll,
        "fpMed": fp_med,
        "gensRunning": len([g for g in gens if g["running"]]),
        "feedersOn": len([f for f in alim if f["closed"]]),
        "feedersTotal": len(alim),
        "alarmsActive": sum(f["alarmsActiveCount"] for f in feeders),
        "alarmsUnacked": sum(f["alarmsUnacked"] for f in feeders),
    }


def bus_summary(feeders: list[dict]) -> dict[str, Any]:
    """Totales por barra: solo magnitudes físicamente sumables (P, Q) o
    promediables (V). No se fabrica una 'corriente de barra' porque no se mide
    ningún CT de acople/entrada, solo corrientes de bahía."""
    def mk(bus_id: str) -> dict[str, Any]:
        cells = [
            f for f in feeders
            if f["closed"] and (f["bus"] == bus_id or f["bus"] == "AB") and f["kind"] != "acople"
        ]
        p = sum(f["p"] for f in cells)
        q = sum(f["q"] for f in cells)
        v = _avg([f["vll"] for f in cells if f["vll"] > 1])
        s = (p ** 2 + q ** 2) ** 0.5
        return {"p": p, "q": q, "v": v, "fp": (p / s if s > 0.01 else None)}

    return {"A": mk("A"), "B": mk("B")}


def build_snapshot(raw_feeders: list[dict], raw_gens: list[dict],
                   ts: datetime | None = None) -> dict[str, Any]:
    """Arma el snapshot completo que consume el front, a partir de los JSON
    crudos de ambos endpoints. Este es el objeto que se publica por SSE y que
    devuelve GET /api/state."""
    feeders = [norm_feeder(f) for f in raw_feeders]
    gens = [norm_gen(g) for g in raw_gens]
    totals = compute_totals(gens, feeders)
    buses = bus_summary(feeders)
    return {
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "feeders": feeders,
        "gens": gens,
        "totals": totals,
        "buses": buses,
    }

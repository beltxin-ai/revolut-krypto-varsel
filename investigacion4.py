#!/usr/bin/env python3
"""
Investigación «Cazador concentrado» con 600 € (velas diarias de 06:00 UTC, costes reales de Revolut X).

Variantes fijadas de antemano:
  A1 · Agresivo, salidas actuales     3 huecos de 1/3 del capital; stop 2 ATR, trailing 3 ATR, salida bajo mínimo 20 d
  A2 · Agresivo, plan de salida       igual entrada; stop −1R (R = 2 ATR); a +1R stop a la entrada; a +2R vende la mitad;
                                      el resto con trailing 2 ATR; sin llegar a +1R en 10 días, cierra
  A3 · A2 + filtro de volumen         solo rupturas con volumen ≥ 1,5 × media de 20 días
  M2 · Moderado, plan de salida       como A2 pero cada operación arriesga un 2 % del capital (tope 1/3)
  M3 · Moderado + filtro de volumen
  P1 · Momentum, la mejor moneda      rotación semanal por rentabilidad de 28 días, con filtro BTC
  P2 · Momentum, las 2 mejores

Todas: solo entradas con BTC por encima de su SMA200; señales al cierre, ejecución a la apertura siguiente;
supuesto pesimista intradía (si en un día se tocan stop y objetivo, primero el stop).
Además de las métricas clásicas: euros, peor racha de pérdidas y Monte Carlo (bloques de 20 días) de la
probabilidad de bajar de 350 € en un año empezando con 600 €.

Salida: resultados4.json
"""
import json
import math
import random
import time
from statistics import mean

import fuentes as f
import indicadores as ind
import investigacion as inv
import investigacion2 as i2
import investigacion3 as i3

log = lambda *a: print(*a, flush=True)  # noqa: E731
CAPITAL, LIMITE = 600.0, 350.0


def preparar(datos):
    S = {}
    for tk, v in datos.items():
        c, h, l, vol = v["c"], v["h"], v["l"], v["v"]
        S[tk] = {"sma200": ind.sma(c, 200), "atr": ind.atr(h, l, c, 14), "max55": ind.maximo_previo(h, 55),
                 "min20": ind.minimo_previo(l, 20), "vol20": ind.sma(vol, 20), "idx": {t: i for i, t in enumerate(v["t"])}}
    return S


def simular(cfg, datos, S, alts, coste, desde, hasta):
    fechas = [t for t in datos["BTC"]["t"] if desde <= t <= hasta]
    jb = S["BTC"]["idx"]
    caja, pos, curva, trades, pend = CAPITAL, {}, [], [], []
    salir = set()
    semana = 0
    K = cfg.get("huecos", 3)

    def J(tk, t):
        return S[tk]["idx"].get(t)

    def valor(t):
        tot = caja
        for tk, p in pos.items():
            j = J(tk, t)
            tot += p["uds"] * (datos[tk]["c"][j] if j is not None else p["ult"])
        return tot

    def vender(tk, precio, fraccion=1.0, motivo=""):
        nonlocal caja
        p = pos[tk]
        uds = p["uds"] * fraccion
        neto = uds * precio * (1 - coste[tk])
        caja += neto
        p["uds"] -= uds
        p["cobrado"] += neto
        if fraccion >= 0.999:
            pos.pop(tk)
            pnl = p["cobrado"] - p["invertido"]
            trades.append({"t": p["t0"], "pnl": pnl, "r": pnl / p["riesgo_eur"] if p.get("riesgo_eur") else None,
                           "ret": pnl / p["invertido"], "motivo": motivo})

    for n, t in enumerate(fechas):
        # 1) apertura: ventas y compras pendientes
        for tk in list(salir):
            j = J(tk, t)
            if tk in pos and j is not None:
                vender(tk, datos[tk]["o"][j], motivo="salida al cierre")
        salir = set()
        eq0 = valor(fechas[n - 1]) if n else caja
        for tk, R in pend:
            j = J(tk, t)
            if j is None or tk in pos or len(pos) >= K:
                continue
            o = datos[tk]["o"][j]
            if cfg["tipo"] == "momentum":
                importe = min(caja, eq0 / K)
                stop, riesgo = None, None
            else:
                frac = 1 / K if cfg["riesgo"] is None else min(1 / K, cfg["riesgo"] * o / R)
                importe = min(caja, eq0 * frac)
                stop, riesgo = o - R, None
            if importe < 5:
                continue
            caja -= importe
            pos[tk] = {"uds": importe * (1 - coste[tk]) / o, "entrada": o, "R": R, "stop": stop, "max": o, "ult": o,
                       "invertido": importe, "cobrado": 0.0, "be": False, "mitad": False, "dias": 0, "t0": t,
                       "riesgo_eur": importe * R / o if R else None}
        pend = []
        # 2) intradía: stops y objetivos (pesimista: primero el stop)
        if cfg["tipo"] != "momentum":
            for tk in list(pos):
                p, j = pos[tk], J(tk, t)
                if j is None:
                    continue
                o, h, l = datos[tk]["o"][j], datos[tk]["h"][j], datos[tk]["l"][j]
                if l <= p["stop"]:
                    vender(tk, min(o, p["stop"]), motivo="stop")
                    continue
                if cfg["plan"]:
                    if not p["mitad"] and h >= p["entrada"] + 2 * p["R"]:
                        vender(tk, max(o, p["entrada"] + 2 * p["R"]), 0.5, "objetivo 2")
                        p["mitad"] = True
                    if h >= p["entrada"] + p["R"]:
                        p["alcanzo_1r"] = True
        # 3) cierre
        eq = valor(t)
        curva.append(eq)
        b = jb[t]
        ok = S["BTC"]["sma200"][b] and datos["BTC"]["c"][b] > S["BTC"]["sma200"][b]
        if cfg["tipo"] == "momentum":
            semana += 1
            if semana % 7 == 0:
                rank = []
                for tk in alts:
                    j = J(tk, t)
                    if j is not None and j >= 28:
                        rank.append((datos[tk]["c"][j] / datos[tk]["c"][j - 28] - 1, tk))
                rank.sort(reverse=True)
                top = [tk for r, tk in rank[:K] if r > 0] if ok else []
                salir |= {tk for tk in pos if tk not in top}
                pend = [(tk, None) for tk in top if tk not in pos]
            continue
        for tk in list(pos):
            p, j = pos[tk], J(tk, t)
            if j is None:
                continue
            c, a = datos[tk]["c"][j], S[tk]["atr"][j]
            p["ult"], p["dias"] = c, p["dias"] + 1
            p["max"] = max(p["max"], datos[tk]["h"][j])
            if cfg["plan"]:
                if p.get("alcanzo_1r") and not p["be"]:
                    p["stop"], p["be"] = max(p["stop"], p["entrada"]), True
                if p["mitad"] and a:
                    p["stop"] = max(p["stop"], p["max"] - 2 * a)
                if not p.get("alcanzo_1r") and p["dias"] >= 10:
                    salir.add(tk)
            else:
                if a:
                    p["stop"] = max(p["stop"], p["max"] - 3 * a)
                mn = S[tk]["min20"][j]
                if mn and c < mn:
                    salir.add(tk)
        libres = K - len(pos) + len(salir)
        if ok and libres > 0:
            cands = []
            for tk in alts:
                j = J(tk, t)
                if j is None or tk in pos:
                    continue
                c, mx, a = datos[tk]["c"][j], S[tk]["max55"][j], S[tk]["atr"][j]
                if not (mx and a and c > mx):
                    continue
                if cfg["filtro_vol"]:
                    v20 = S[tk]["vol20"][j - 1] if j else None
                    if not v20 or datos[tk]["v"][j] < 1.5 * v20:
                        continue
                cands.append((c / mx, tk, 2 * a))
            pend = [(tk, R) for _, tk, R in sorted(cands, reverse=True)[:libres]]
    return curva, trades


def resumen(curva, trades, dias_ano=365):
    m = inv.metricas(curva)
    peor, racha = 0, 0
    for x in sorted(trades, key=lambda x: x["t"]):
        racha = racha + 1 if x["pnl"] <= 0 else 0
        peor = max(peor, racha)
    pico, dd_eur = curva[0], 0
    for x in curva:
        pico = max(pico, x)
        dd_eur = min(dd_eur, x - pico)
    g = [x["pnl"] for x in trades if x["pnl"] > 0]
    p = [-x["pnl"] for x in trades if x["pnl"] <= 0]
    rs = [x["r"] for x in trades if x["r"] is not None]
    meses = len(curva) / 30.4
    return {**m, "final_eur": curva[-1] * CAPITAL / curva[0], "max_dd_eur": dd_eur * CAPITAL / curva[0],
            "operaciones": len(trades), "ops_mes": len(trades) / meses if meses else 0,
            "aciertos": len(g) / len(trades) if trades else None, "profit_factor": sum(g) / sum(p) if p else None,
            "r_medio": mean(rs) if rs else None, "peor_racha": peor,
            "calmar": (m["cagr"] / -m["max_dd"]) if m and m.get("max_dd") else None}


def monte_carlo(curva, n=3000, dias=365, bloque=20, semilla=7):
    rets = [curva[i] / curva[i - 1] for i in range(1, len(curva))]
    rnd = random.Random(semilla)
    bajo, finales = 0, []
    for _ in range(n):
        v, minimo, k = CAPITAL, CAPITAL, 0
        while k < dias:
            s = rnd.randrange(0, len(rets) - bloque)
            for r in rets[s:s + bloque]:
                v *= r
                minimo = min(minimo, v)
                k += 1
                if k >= dias:
                    break
        bajo += minimo < LIMITE
        finales.append(v)
    finales.sort()
    return {"prob_bajo_350": bajo / n, "mediana_1a": finales[n // 2], "p10_1a": finales[n // 10], "p90_1a": finales[9 * n // 10]}


VARIANTES = [
    ("A1 · Agresivo, salidas actuales", dict(tipo="ruptura", plan=False, filtro_vol=False, riesgo=None)),
    ("A2 · Agresivo, plan de salida", dict(tipo="ruptura", plan=True, filtro_vol=False, riesgo=None)),
    ("A3 · Agresivo, plan de salida + filtro volumen", dict(tipo="ruptura", plan=True, filtro_vol=True, riesgo=None)),
    ("M2 · Moderado 2 %, plan de salida", dict(tipo="ruptura", plan=True, filtro_vol=False, riesgo=0.02)),
    ("M3 · Moderado 2 %, plan de salida + filtro volumen", dict(tipo="ruptura", plan=True, filtro_vol=True, riesgo=0.02)),
    ("P1 · Momentum, la mejor moneda", dict(tipo="momentum", huecos=1)),
    ("P2 · Momentum, las 2 mejores", dict(tipo="momentum", huecos=2)),
]


def main():
    t0 = time.time()
    rx = inv.universo_revolut_x() or {}
    bn = f.binance_simbolos()
    cands = [(t, q["spread"]) for t, q in rx.items()
             if t not in i2.ESTABLES and f"{t}USDT" in bn and q["vol_usd"] >= inv.MIN_VOL_USD and q["spread"] <= inv.MAX_SPREAD]
    if "BTC" not in [c[0] for c in cands]:
        cands.append(("BTC", 0.0002))
    datos, coste = {}, {}
    for tk, sp in cands:
        try:
            v = i3.velas_desplazadas(f"{tk}USDT", 2000)
            if len(v["c"]) >= 260 and v["ct"][-1] > (time.time() - 3 * 86400) * 1000:
                datos[tk], coste[tk] = v, inv.COMISION + sp / 2 + inv.DESLIZ
        except Exception as e:  # noqa: BLE001
            log("datos", tk, e)
    alts = sorted(t for t in datos if t not in ("BTC", "ETH"))
    S = preparar(datos)
    todas = datos["BTC"]["t"]
    inicio, fin = todas[210], todas[-1]
    corte = todas[210 + int((len(todas) - 210) * 0.6)]
    periodos = {"entrenamiento": (inicio, corte), "fuera_de_muestra": (corte, fin), "completo": (inicio, fin)}
    log(f"Altcoins: {len(alts)}")
    resultados = []
    for nombre, cfg in VARIANTES:
        fila = {"nombre": nombre, "cfg": cfg}
        for per, (a, b) in periodos.items():
            curva, trades = simular(cfg, datos, S, alts, coste, a, b)
            fila[per] = resumen(curva, trades)
            if per == "completo":
                fila["monte_carlo"] = monte_carlo(curva)
        resultados.append(fila)
        e, o, c = fila["entrenamiento"], fila["fuera_de_muestra"], fila["completo"]
        log(f"{nombre}: IS Sharpe {e.get('sharpe', 0):.2f} · OOS Sharpe {o.get('sharpe', 0):.2f} CAGR {o.get('cagr', 0):.0%} "
            f"DD {o.get('max_dd', 0):.0%} · 5 años: {c['final_eur']:.0f} € · P(<350 €) {fila['monte_carlo']['prob_bajo_350']:.0%}")
    fecha = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))  # noqa: E731
    out = {"generado": int(time.time()), "segundos": round(time.time() - t0), "capital": CAPITAL, "limite": LIMITE,
           "universo": {"n": len(alts), "monedas": alts}, "periodos": {k: [fecha(a), fecha(b)] for k, (a, b) in periodos.items()},
           "resultados": resultados}
    with open("resultados4.json", "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    log("Listo en", round(time.time() - t0), "s")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Investigación fase 2 y 3: mejoras de gestión de riesgo y de reacción sobre el sistema COMPLETO
(núcleo BTC/ETH + satélite Donchian 55/20), simulado como una sola cartera.

Variantes fijadas de antemano (sin optimizar parámetros):
  - Tamaño por riesgo: la posición se reduce si su stop está a más de un 10 % (= 1 % del capital con 10 %).
  - Control de volatilidad: las entradas nuevas se escalan por min(1, vol objetivo / vol de BTC a 30 días).
    La vol objetivo es la mediana de la vol de BTC en el periodo de ENTRENAMIENTO (no se elige a mano).
  - Cortacircuitos: sin entradas nuevas si la cartera cae más de un 20 % desde su máximo,
    hasta que recupere a menos de un 10 %.
  - Entrada intradía: compra en cuanto el precio cruza el máximo de 55 días, con 0,3 % de deslizamiento
    extra y supuesto pesimista (si el mínimo del día toca el stop, se da por perdido).
  - Tres bloques: núcleo, ruptura y momentum 28 días (top 5 semanal), un tercio cada uno.

Costes por lado: comisión 0,09 % + medio spread real del par en Revolut X + 0,05 % de deslizamiento.
Regla de adopción: una variante solo se adopta si mejora la base EN ENTRENAMIENTO Y FUERA DE MUESTRA.

Salida: resultados2.json
"""
import json
import math
import time
from statistics import median

import fuentes as f
import indicadores as ind
import investigacion as inv

log = lambda *a: print(*a, flush=True)  # noqa: E731
EXTRA_INTRADIA = 0.003
ESTABLES = {"USDC", "USDT", "EURC", "DAI", "USDE", "PYUSD", "RLUSD", "FDUSD", "TUSD", "USDS", "USD1"}


def preparar(datos):
    S = {}
    for tk, v in datos.items():
        c, h, l = v["c"], v["h"], v["l"]
        rets = [0.0] + [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
        vol30 = [None] * len(c)
        for i in range(30, len(c)):
            w = rets[i - 29:i + 1]
            m = sum(w) / 30
            vol30[i] = math.sqrt(sum((x - m) ** 2 for x in w) / 30) * math.sqrt(365)
        S[tk] = {"sma50": ind.sma(c, 50), "sma200": ind.sma(c, 200), "atr": ind.atr(h, l, c, 14),
                 "max55": ind.maximo_previo(h, 55), "min20": ind.minimo_previo(l, 20), "vol30": vol30,
                 "idx": {t: i for i, t in enumerate(v["t"])}}
    return S


def simular(cfg, datos, S, tickers, coste, desde, hasta, vol_obj):
    """Cartera única. cfg: tres_bloques, tam_riesgo, vol_target, corte, intradia."""
    btc = S["BTC"]["idx"]
    fechas = [t for t in datos["BTC"]["t"] if desde <= t <= hasta]
    w_core, w_sat, w_mom, n_sat, n_mom = (1 / 6, 1 / 15, 1 / 15, 5, 5) if cfg["tres_bloques"] else (0.25, 0.10, 0, 5, 0)
    caja, pos, ops, curva = 1.0, {}, [], []
    pico, bloqueado, pend = 1.0, False, {"vender": set(), "comprar": []}
    alts = [t for t in tickers if t not in ("BTC", "ETH")]
    semana = 0

    def J(tk, fecha):
        return S[tk]["idx"].get(fecha)

    def valor(fecha):
        tot = caja
        for k, p in pos.items():
            j = J(p["tk"], fecha)
            tot += p["uds"] * (datos[p["tk"]]["c"][j] if j is not None else p["ult"])
        return tot

    def comprar(k, tk, precio, peso, stop, eq):
        nonlocal caja
        if k in pos:
            return
        importe = min(caja, eq * peso)
        if importe < eq * 0.01:
            return
        caja -= importe
        pos[k] = {"tk": tk, "uds": importe * (1 - coste[tk]) / precio, "stop": stop, "max": precio,
                  "coste_in": importe, "ult": precio}

    def vender(k, precio):
        nonlocal caja
        p = pos.pop(k, None)
        if p:
            neto = p["uds"] * precio * (1 - coste[p["tk"]])
            caja += neto
            ops.append((k.split(":")[0], neto / p["coste_in"] - 1))

    def factor(tk, j, fecha_btc_j, stop_dist=None):
        m = 1.0
        if cfg["vol_target"]:
            v = S["BTC"]["vol30"][fecha_btc_j]
            if v:
                m *= min(1.0, vol_obj / v)
        if cfg["tam_riesgo"] and stop_dist:
            m *= min(1.0, 0.10 / stop_dist)
        return m

    for n, fecha in enumerate(fechas):
        jb = btc[fecha]
        # 1) apertura: órdenes pendientes del cierre anterior
        for k in list(pend["vender"]):
            if k in pos:
                j = J(pos[k]["tk"], fecha)
                if j is not None:
                    vender(k, datos[pos[k]["tk"]]["o"][j])
        eq0 = valor(fechas[n - 1]) if n else caja
        for k, tk, peso, stop_mult in pend["comprar"]:
            j = J(tk, fecha)
            if j is None:
                continue
            o = datos[tk]["o"][j]
            stop = o - stop_mult if stop_mult else None
            comprar(k, tk, o, peso, stop, eq0)
        pend = {"vender": set(), "comprar": []}
        # 2) intradía: entradas por cruce (modo intradía) y stops
        if cfg["intradia"] and n and not bloqueado:
            jb1 = jb - 1
            ok = S["BTC"]["sma200"][jb1] and datos["BTC"]["c"][jb1] > S["BTC"]["sma200"][jb1]
            libres = n_sat - sum(1 for k in pos if k.startswith("sat:"))
            if ok and libres > 0:
                cands = []
                for tk in alts:
                    j = J(tk, fecha)
                    if j is None or j < 1 or f"sat:{tk}" in pos:
                        continue
                    L, a = S[tk]["max55"][j], S[tk]["atr"][j - 1]
                    if L and a and datos[tk]["h"][j] > L:
                        cands.append((datos[tk]["c"][j - 1] / L, tk, j, L, a))
                for _, tk, j, L, a in sorted(cands, reverse=True)[:libres]:
                    precio = max(datos[tk]["o"][j], L) * (1 + EXTRA_INTRADIA)
                    stop = precio - 2 * a
                    peso = w_sat * factor(tk, j, jb1, 2 * a / precio)
                    comprar(f"sat:{tk}", tk, precio, peso, stop, valor(fechas[n - 1]))
                    if f"sat:{tk}" in pos:
                        pos[f"sat:{tk}"]["hoy"] = True       # entrada de hoy: el stop se ejecuta al propio stop
        for k in list(pos):
            p = pos[k]
            j = J(p["tk"], fecha)
            if j is None or not p["stop"]:
                continue
            if datos[p["tk"]]["l"][j] <= p["stop"]:
                vender(k, p["stop"] if p.pop("hoy", False) else min(datos[p["tk"]]["o"][j], p["stop"]))
            else:
                p.pop("hoy", None)
        # 3) cierre: valoración, cortacircuitos, trailing y señales para mañana
        eq = valor(fecha)
        curva.append(eq)
        pico = max(pico, eq)
        dd = eq / pico - 1
        if cfg["corte"]:
            if dd < -0.20:
                bloqueado = True
            elif dd > -0.10:
                bloqueado = False
        ok = S["BTC"]["sma200"][jb] and datos["BTC"]["c"][jb] > S["BTC"]["sma200"][jb]
        # núcleo
        for tk in ("BTC", "ETH"):
            j = J(tk, fecha)
            if j is None:
                continue
            c, a50, a200 = datos[tk]["c"][j], S[tk]["sma50"][j], S[tk]["sma200"][j]
            dentro = bool(a50 and a200 and c > a50 > a200)
            k = f"core:{tk}"
            if k in pos and not dentro:
                pend["vender"].add(k)
            elif k not in pos and dentro and not bloqueado:
                pend["comprar"].append((k, tk, w_core * factor(tk, j, jb), None))
        # satélite: trailing, salidas y (modo cierre) entradas
        cands = []
        for tk in alts:
            j = J(tk, fecha)
            if j is None:
                continue
            k = f"sat:{tk}"
            c, a = datos[tk]["c"][j], S[tk]["atr"][j]
            if k in pos:
                p = pos[k]
                p["ult"] = c
                p["max"] = max(p["max"], datos[tk]["h"][j])
                if a:
                    p["stop"] = max(p["stop"] or 0, p["max"] - 3 * a)
                mn = S[tk]["min20"][j]
                if mn and c < mn:
                    pend["vender"].add(k)
                continue
            mx = S[tk]["max55"][j]
            if not cfg["intradia"] and ok and not bloqueado and mx and a and c > mx:
                cands.append((c / mx, tk, j, a))
        libres = n_sat - sum(1 for k in pos if k.startswith("sat:"))
        for _, tk, j, a in sorted(cands, reverse=True)[:max(0, libres)]:
            c = datos[tk]["c"][j]
            pend["comprar"].append((f"sat:{tk}", tk, w_sat * factor(tk, j, jb, 2 * a / c), 2 * a))
        # momentum semanal
        if n_mom:
            semana += 1
            if semana % 7 == 0:
                rank = []
                for tk in alts:
                    j = J(tk, fecha)
                    if j is not None and j >= 28:
                        rank.append((datos[tk]["c"][j] / datos[tk]["c"][j - 28] - 1, tk, j))
                rank.sort(reverse=True)
                top = {tk: j for r, tk, j in rank[:n_mom] if r > 0} if ok else {}
                for k in [k for k in pos if k.startswith("mom:")]:
                    if pos[k]["tk"] not in top:
                        pend["vender"].add(k)
                if not bloqueado:
                    for tk, j in top.items():
                        k = f"mom:{tk}"
                        if k not in pos:
                            a, c = S[tk]["atr"][j], datos[tk]["c"][j]
                            pend["comprar"].append((k, tk, w_mom * factor(tk, j, jb, (2 * a / c) if a else None), None))
    m = inv.metricas(curva)
    o = [r for _, r in ops]
    res = {**m, **inv.stats_ops(o), "calmar": (m["cagr"] / -m["max_dd"]) if m and m.get("max_dd") else None}
    res["por_bloque"] = {b: inv.stats_ops([r for bb, r in ops if bb == b]) for b in ("core", "sat", "mom")}
    return res


VARIANTES = [
    ("A · Sistema actual", dict(tres_bloques=False, tam_riesgo=False, vol_target=False, corte=False, intradia=False)),
    ("B · + tamaño por riesgo", dict(tres_bloques=False, tam_riesgo=True, vol_target=False, corte=False, intradia=False)),
    ("C · + control de volatilidad", dict(tres_bloques=False, tam_riesgo=False, vol_target=True, corte=False, intradia=False)),
    ("D · + cortacircuitos 20 %", dict(tres_bloques=False, tam_riesgo=False, vol_target=False, corte=True, intradia=False)),
    ("E · Riesgo completo (B+C+D)", dict(tres_bloques=False, tam_riesgo=True, vol_target=True, corte=True, intradia=False)),
    ("F · Entrada intradía", dict(tres_bloques=False, tam_riesgo=False, vol_target=False, corte=False, intradia=True)),
    ("G · Riesgo completo + intradía", dict(tres_bloques=False, tam_riesgo=True, vol_target=True, corte=True, intradia=True)),
    ("H · Tres bloques (con momentum)", dict(tres_bloques=True, tam_riesgo=False, vol_target=False, corte=False, intradia=False)),
    ("I · Tres bloques + riesgo completo", dict(tres_bloques=True, tam_riesgo=True, vol_target=True, corte=True, intradia=False)),
]


def main():
    t0 = time.time()
    rx = inv.universo_revolut_x() or {}
    bn = f.binance_simbolos()
    cands = [(t, q["spread"]) for t, q in rx.items()
             if t not in ESTABLES and f"{t}USDT" in bn and q["vol_usd"] >= inv.MIN_VOL_USD and q["spread"] <= inv.MAX_SPREAD]
    for t, sp in (("BTC", 0.0002), ("ETH", 0.0003)):
        if t not in [c[0] for c in cands]:
            cands.append((t, sp))
    log(f"Universo: {len(cands)} monedas")
    datos, coste = {}, {}
    for tk, sp in cands:
        try:
            v = inv.velas_binance(f"{tk}USDT", "1d", 2000)
            if len(v["c"]) >= 260 and v["ct"][-1] > (time.time() - 3 * 86400) * 1000:
                datos[tk], coste[tk] = v, inv.COMISION + sp / 2 + inv.DESLIZ
        except Exception as e:  # noqa: BLE001
            log("datos", tk, e)
    tickers = sorted(datos)
    S = preparar(datos)
    todas = datos["BTC"]["t"]
    inicio = todas[210]
    corte = todas[210 + int((len(todas) - 210) * 0.6)]
    fin = todas[-1]
    periodos = {"entrenamiento": (inicio, corte), "fuera_de_muestra": (corte, fin), "completo": (inicio, fin)}
    iv = S["BTC"]["idx"]
    vol_obj = median(x for t, x in zip(todas, S["BTC"]["vol30"]) if x and inicio <= t <= corte)
    log(f"Monedas con historia: {len(tickers)} · vol objetivo BTC (mediana entrenamiento): {vol_obj:.1%}")
    resultados = []
    for nombre, cfg in VARIANTES:
        fila = {"nombre": nombre, "cfg": cfg}
        for per, (a, b) in periodos.items():
            fila[per] = simular(cfg, datos, S, tickers, coste, a, b, vol_obj)
        resultados.append(fila)
        e, o = fila["entrenamiento"], fila["fuera_de_muestra"]
        log(f"{nombre}: IS Sharpe {e.get('sharpe', 0):.2f} DD {e.get('max_dd', 0):.0%} · "
            f"OOS Sharpe {o.get('sharpe', 0):.2f} CAGR {o.get('cagr', 0):.0%} DD {o.get('max_dd', 0):.0%}")
    base = resultados[0]
    for r in resultados[1:]:
        r["mejora_ambos"] = all(r[p].get("sharpe", -9) > base[p].get("sharpe", -9) and
                                (r[p].get("calmar") or -9) >= (base[p].get("calmar") or -9)
                                for p in ("entrenamiento", "fuera_de_muestra"))
    fecha = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))  # noqa: E731
    out = {"generado": int(time.time()), "segundos": round(time.time() - t0),
           "universo": {"n": len(tickers), "monedas": tickers}, "vol_objetivo": vol_obj,
           "periodos": {k: [fecha(a), fecha(b)] for k, (a, b) in periodos.items()},
           "regla_adopcion": "mejor Sharpe y Calmar que la base en entrenamiento Y fuera de muestra",
           "resultados": resultados}
    with open("resultados2.json", "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    log("Listo en", round(time.time() - t0), "s")


if __name__ == "__main__":
    main()

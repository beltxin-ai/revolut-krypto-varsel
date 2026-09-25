#!/usr/bin/env python3
"""
Investigación: ¿a qué hora debe cerrar el «día» del sistema?

El sistema se validó con velas diarias que cierran a las 00:00 UTC (02:00 en Noruega en verano),
pero el usuario opera al despertar (~06:00 UTC). Se comparan tres escenarios con el sistema actual
(núcleo + satélite + control de volatilidad), mismos costes y misma división entrenamiento / fuera de muestra:

  A · Cierre 00:00 UTC, ejecución a las 00:00 (teórico, como se validó)
  B · Cierre 00:00 UTC, ejecución a las 06:00 UTC (lo que pasa hoy en la práctica)
  C · Cierre 06:00 UTC, ejecución a las 06:00 UTC (propuesta)

Las velas de 06:00 se piden a Binance con el parámetro timeZone=-6 (días de 06:00 a 06:00 UTC).
Si Binance no respetara ese parámetro, se construyen agregando velas de 1 hora.

Salida: resultados3.json
"""
import json
import time
from statistics import median

import fuentes as f
import investigacion as inv
import investigacion2 as i2

log = lambda *a: print(*a, flush=True)  # noqa: E731
D = 86400 * 1000
H6 = 6 * 3600 * 1000


def velas_desplazadas(par, total):
    """Velas diarias de 06:00 a 06:00 UTC."""
    filas, fin = [], None
    while len(filas) < total:
        url = f"{f.BINANCE}/klines?symbol={par}&interval=1d&limit=1000&timeZone=-6" + (f"&endTime={fin}" if fin else "")
        d = f.pedir(url)
        if not d:
            break
        filas = d + filas
        fin = int(d[0][0]) - 1
        if len(d) < 1000:
            break
        time.sleep(0.2)
    ahora = int(time.time() * 1000)
    filas = [k for k in filas if int(k[6]) <= ahora][-total:]
    if filas and int(filas[-1][0]) % D != H6:
        raise RuntimeError("Binance no aplicó timeZone=-6")
    v = {"t": [], "ct": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    for k in filas:
        v["t"].append(int(k[0])); v["ct"].append(int(k[6]))
        v["o"].append(float(k[1])); v["h"].append(float(k[2])); v["l"].append(float(k[3]))
        v["c"].append(float(k[4])); v["v"].append(float(k[7]))
    return v


def agregar_1h(par, total_dias):
    """Respaldo: construye las velas de 06:00 UTC a partir de velas de 1 h."""
    v1 = inv.velas_binance(par, "1h", total_dias * 24 + 48)
    dias = {}
    for i, t in enumerate(v1["t"]):
        k = (t - H6) // D * D + H6
        d = dias.setdefault(k, {"n": 0, "o": v1["o"][i], "h": v1["h"][i], "l": v1["l"][i], "c": v1["c"][i], "v": 0.0})
        d["n"] += 1; d["h"] = max(d["h"], v1["h"][i]); d["l"] = min(d["l"], v1["l"][i]); d["c"] = v1["c"][i]; d["v"] += v1["v"][i]
    v = {"t": [], "ct": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    for k in sorted(dias):
        d = dias[k]
        if d["n"] < 20 or k + D > int(time.time() * 1000):
            continue
        v["t"].append(k); v["ct"].append(k + D - 1)
        for x in "ohlcv":
            v[x].append(d[x])
    return v


def main():
    t0 = time.time()
    rx = inv.universo_revolut_x() or {}
    bn = f.binance_simbolos()
    cands = [(t, q["spread"]) for t, q in rx.items()
             if t not in i2.ESTABLES and f"{t}USDT" in bn and q["vol_usd"] >= inv.MIN_VOL_USD and q["spread"] <= inv.MAX_SPREAD]
    for t, sp in (("BTC", 0.0002), ("ETH", 0.0003)):
        if t not in [c[0] for c in cands]:
            cands.append((t, sp))
    log(f"Universo: {len(cands)} monedas")
    utc, seis, coste, metodo = {}, {}, {}, "timeZone=-6"
    for tk, sp in cands:
        try:
            a = inv.velas_binance(f"{tk}USDT", "1d", 2000)
            try:
                b = velas_desplazadas(f"{tk}USDT", 2000)
            except Exception as e:  # noqa: BLE001
                log("timeZone no disponible, agrego 1 h:", tk, e)
                metodo = "agregado de velas de 1 h"
                b = agregar_1h(f"{tk}USDT", 2000)
            reciente = (time.time() - 3 * 86400) * 1000
            if len(a["c"]) >= 260 and len(b["c"]) >= 260 and a["ct"][-1] > reciente and b["ct"][-1] > reciente:
                utc[tk], seis[tk], coste[tk] = a, b, inv.COMISION + sp / 2 + inv.DESLIZ
        except Exception as e:  # noqa: BLE001
            log("datos", tk, e)
    tickers = sorted(utc)
    log(f"Con historia: {len(tickers)} · velas de 06:00 por {metodo}")

    # B: señales con velas de 00:00, pero la ejecución de cada día se hace al precio de las 06:00 UTC
    tardio = {}
    for tk in tickers:
        a, b = utc[tk], seis[tk]
        ap6 = {t - H6: o for t, o in zip(b["t"], b["o"])}           # apertura 06:00 del mismo día UTC
        o = [ap6.get(t, oo) for t, oo in zip(a["t"], a["o"])]
        tardio[tk] = {**a, "o": o}

    cfg = dict(tres_bloques=False, tam_riesgo=False, vol_target=True, corte=False, intradia=False)
    escenarios = [("A · Cierre 00:00 UTC, compra al instante (teórico)", utc),
                  ("B · Cierre 00:00 UTC, compra a las 06:00 (lo que pasa hoy)", tardio),
                  ("C · Cierre 06:00 UTC, compra al instante (propuesta)", seis)]
    resultados = []
    for nombre, datos in escenarios:
        S = i2.preparar(datos)
        todas = datos["BTC"]["t"]
        inicio, fin = todas[210], todas[-1]
        corte = todas[210 + int((len(todas) - 210) * 0.6)]
        vol_obj = median(x for t, x in zip(todas, S["BTC"]["vol30"]) if x and inicio <= t <= corte)
        fila = {"nombre": nombre}
        for per, (x, y) in {"entrenamiento": (inicio, corte), "fuera_de_muestra": (corte, fin), "completo": (inicio, fin)}.items():
            fila[per] = i2.simular(cfg, datos, S, tickers, coste, x, y, vol_obj)
        resultados.append(fila)
        e, o = fila["entrenamiento"], fila["fuera_de_muestra"]
        log(f"{nombre}: IS Sharpe {e.get('sharpe', 0):.2f} CAGR {e.get('cagr', 0):.0%} DD {e.get('max_dd', 0):.0%} · "
            f"OOS Sharpe {o.get('sharpe', 0):.2f} CAGR {o.get('cagr', 0):.0%} DD {o.get('max_dd', 0):.0%}")
    b, c = resultados[1], resultados[2]
    c["mejor_que_b"] = all(c[p].get("sharpe", -9) >= b[p].get("sharpe", -9) for p in ("entrenamiento", "fuera_de_muestra"))
    out = {"generado": int(time.time()), "segundos": round(time.time() - t0), "metodo_velas_0600": metodo,
           "universo": {"n": len(tickers), "monedas": tickers}, "resultados": resultados,
           "regla": "C se adopta si su Sharpe es igual o mejor que B en entrenamiento y fuera de muestra"}
    with open("resultados3.json", "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    log("Listo en", round(time.time() - t0), "s")


if __name__ == "__main__":
    main()

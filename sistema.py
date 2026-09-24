"""
Sistema de trading validado (ver investigacion.py y la pestaña «Evidencia» del panel).

Dos bloques, ambos con velas DIARIAS cerradas (00:00 UTC):

NÚCLEO (50 % del capital) · Tendencia BTC y ETH
  - Dentro si cierre > SMA50 y SMA50 > SMA200. Fuera en caso contrario.
  - 25 % del capital en cada una cuando están dentro. Sin stop (así se validó).

SATÉLITE (50 % del capital) · Ruptura Donchian 55/20 en altcoins líquidas de Revolut X
  - Solo se abren entradas si BTC cierra por encima de su SMA200 (filtro de régimen).
  - Entrada: cierre por encima del máximo de los 55 días anteriores.
  - Máximo 5 posiciones de 10 % del capital cada una; si hay más candidatas, se eligen
    las que rompen con más fuerza (cierre / máximo 55 d).
  - Stop inicial: cierre − 2 ATR(14). Trailing: máximo desde la entrada − 3 ATR.
  - Salida: cierre por debajo del mínimo de los 20 días anteriores, o precio en vivo ≤ stop.
"""
import indicadores as ind

PESO_NUCLEO = 0.25          # por moneda (BTC, ETH)
PESO_SATELITE = 0.10        # por posición
MAX_SATELITE = 5
ENTRADA_N, SALIDA_N = 55, 20
STOP_ATR, TRAIL_ATR = 2.0, 3.0
NUCLEO = ("BTC", "ETH")


def preparar(v):
    c, h, l = v["c"], v["h"], v["l"]
    return {"sma50": ind.sma(c, 50), "sma200": ind.sma(c, 200), "atr": ind.atr(h, l, c, 14),
            "max55": ind.maximo_previo(h, ENTRADA_N), "min20": ind.minimo_previo(l, SALIDA_N)}


def regimen_btc(v_btc, s_btc):
    c, m = v_btc["c"][-1], s_btc["sma200"][-1]
    return {"alcista": bool(m and c > m), "btc": c, "sma200": m,
            "distancia": (c / m - 1) if m else None}


def nucleo_dentro(v, s):
    c, a, b = v["c"][-1], s["sma50"][-1], s["sma200"][-1]
    return bool(a and b and c > a and a > b)


def estado_moneda(v, s):
    """Resumen técnico diario de una moneda para el panel."""
    c = v["c"][-1]
    mx, mn, atr = s["max55"][-1], s["min20"][-1], s["atr"][-1]
    s50, s200 = s["sma50"][-1], s["sma200"][-1]
    return {
        "cierre": c, "max55": mx, "min20": mn, "atr": atr,
        "atr_pct": (atr / c) if (atr and c) else None,
        "dist_max55": (c / mx - 1) if mx else None,
        "sobre_sma50": bool(s50 and c > s50), "sobre_sma200": bool(s200 and c > s200),
        "ruptura": bool(mx and c > mx),
        "tendencia": "alcista" if (s50 and s200 and c > s50 > s200) else
                     "bajista" if (s50 and s200 and c < s50 < s200) else "mixta",
    }


def candidatas_satelite(datos, series, universo, ocupadas):
    """Monedas que rompen su máximo de 55 días al cierre, ordenadas por fuerza."""
    out = []
    for t in universo:
        if t in NUCLEO or t in ocupadas or t not in datos:
            continue
        v, s = datos[t], series[t]
        c, mx, a = v["c"][-1], s["max55"][-1], s["atr"][-1]
        if mx and a and c > mx:
            out.append({"ticker": t, "fuerza": c / mx, "cierre": c, "atr": a, "stop": c - STOP_ATR * a})
    return sorted(out, key=lambda x: -x["fuerza"])


def actualizar_trailing(pos, v, s):
    """Tras cada cierre diario: sube el stop con el máximo alcanzado."""
    pos["maximo"] = max(pos.get("maximo", 0), v["h"][-1])
    a = s["atr"][-1]
    if a:
        pos["stop"] = max(pos["stop"], pos["maximo"] - TRAIL_ATR * a)
    return pos


def salida_diaria(pos, v, s):
    mn = s["min20"][-1]
    return bool(mn and v["c"][-1] < mn)

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


def niveles_vivos(v):
    """Niveles que decidirán el PRÓXIMO cierre (incluyen la última vela cerrada)."""
    return {"ruptura": max(v["h"][-ENTRADA_N:]), "salida": min(v["l"][-SALIDA_N:])}


def recomendacion(t, v, s, precio, pos, alcista, libres):
    """Acción concreta para una moneda con el precio en vivo de Revolut X.

    accion: MANTENER · VENDER? (alerta) · POSIBLE COMPRA · VIGILAR · NO COMPRAR · SIN HUECO
    La compra/venta real solo se confirma con el cierre diario; esto indica qué esperar.
    """
    if not precio:
        return None
    nv = niveles_vivos(v)
    s50, s200 = s["sma50"][-1], s["sma200"][-1]
    d = lambda x: (precio / x - 1) if x else None  # noqa: E731
    if t in NUCLEO:
        if pos:
            if s50 and precio < s50:
                return {"accion": "ALERTA", "texto": "Bajo la SMA50: si cierra por debajo, se vende al cierre", "nivel": s50, "dist": d(s50)}
            return {"accion": "MANTENER", "texto": "Tendencia intacta. Sale si cierra bajo la SMA50", "nivel": s50, "dist": d(s50)}
        if s50 and s200 and precio > s50 and s50 > s200:
            return {"accion": "POSIBLE COMPRA", "texto": "Cumple la tendencia en vivo; se confirma al cierre", "nivel": s50, "dist": d(s50)}
        return {"accion": "NO COMPRAR", "texto": "Sin tendencia (necesita cierre > SMA50 y SMA50 > SMA200)", "nivel": s50, "dist": d(s50)}
    if pos:
        st = pos.get("stop")
        if st and precio <= st * 1.03:
            return {"accion": "ALERTA", "texto": "A menos de un 3 % del stop", "nivel": st, "dist": d(st)}
        if precio < nv["salida"]:
            return {"accion": "ALERTA", "texto": "Bajo el mínimo de 20 días: si cierra así, se vende", "nivel": nv["salida"], "dist": d(nv["salida"])}
        return {"accion": "MANTENER", "texto": "Stop dinámico activo", "nivel": st, "dist": d(st) if st else None}
    if not alcista:
        return {"accion": "NO COMPRAR", "texto": "Régimen bajista: no se abren altcoins", "nivel": nv["ruptura"], "dist": d(nv["ruptura"])}
    if precio > nv["ruptura"]:
        if libres <= 0:
            return {"accion": "SIN HUECO", "texto": "Rompe, pero el satélite está lleno", "nivel": nv["ruptura"], "dist": d(nv["ruptura"])}
        return {"accion": "POSIBLE COMPRA", "texto": "Rompe en vivo el máximo de 55 días; se confirma si cierra por encima",
                "nivel": nv["ruptura"], "dist": d(nv["ruptura"])}
    if precio >= nv["ruptura"] * 0.95:
        return {"accion": "VIGILAR", "texto": "Cerca del máximo de 55 días", "nivel": nv["ruptura"], "dist": d(nv["ruptura"])}
    return {"accion": "NO COMPRAR", "texto": "Sin ruptura", "nivel": nv["ruptura"], "dist": d(nv["ruptura"])}


# ------------------------------------------------------------------ gestión y ejecución
# NO validado con histórico: solo reduce riesgo o afina la ejecución, nunca abre operaciones.
RIESGO_MAX = 0.01            # pérdida máxima por operación si salta el stop (1 % del capital)
EXPOSICION_MAX = 0.80        # aviso si la cartera invertida supera el 80 %
CAIDA_BTC_1H = -0.04         # BTC cae más de un 4 % en una vela de 1 h
VOL_X_PROTECCION = 2.0       # volumen de la vela de 1 h frente a la media de 24 h
PERSEGUIR_24H = 0.30         # subida en 24 h a partir de la cual no se paga por encima del cierre


def tamano_por_riesgo(peso, precio, stop):
    """Tamaño para que el stop cueste como mucho RIESGO_MAX del capital (nunca mayor que el del sistema)."""
    if not stop or not precio or stop >= precio:
        return peso
    return min(peso, RIESGO_MAX / (1 - stop / precio))


def precio_limite(bid, cierre, cambio_24h):
    """No perseguir: como mucho un 1 % sobre el cierre de la señal; tras subidas > 30 % en 24 h, el propio cierre."""
    tope = cierre * (1.0 if (cambio_24h or 0) > PERSEGUIR_24H else 1.01)
    return min(bid, tope) if bid else tope


def nivel_ruptura_entrada(v, s, t_entrada_s):
    """Máximo de 55 días que rompió la moneda en el cierre que generó la entrada."""
    ms, idx = t_entrada_s * 1000, None
    for i, ct in enumerate(v["ct"]):
        if ct <= ms:
            idx = i
    return s["max55"][idx] if idx is not None else None


def proteccion_horaria(v1h, nivel):
    """Última vela de 1 h cerrada por debajo de `nivel` con volumen > 2× la media de las 24 anteriores."""
    if not nivel or len(v1h["c"]) < 25:
        return False
    media = sum(v1h["v"][-25:-1]) / 24
    return v1h["c"][-1] < nivel and media > 0 and v1h["v"][-1] > VOL_X_PROTECCION * media

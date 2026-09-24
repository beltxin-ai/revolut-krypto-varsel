"""
Estrategia de confluencia para swing corto (horas a pocos días).

Marco temporal: velas de 4 h cerradas para la señal, diario para el filtro de
tendencia. El precio en vivo (cada 15 min) solo se usa para stops, objetivos,
sobreextensión y P/L.

Siete pilares, cada uno de -2 a +2:
  P1 Tendencia diaria        (EMA50/EMA200 diarias, fuerza ADX)
  P2 Tendencia 4 h           (EMA20/EMA50 de 4 h)
  P3 Momento 4 h             (MACD, RSI)
  P4 Volumen y flujo         (ruptura con volumen relativo, OBV)
  P5 Fuerza relativa vs BTC  (rentabilidad 7 d frente a BTC)
  P6 Régimen de mercado      (BTC vs EMA200, Fear & Greed, dominancia)   [solo en vivo]
  P7 Derivados               (funding, interés abierto, Hyperliquid)     [solo en vivo]

Nota ponderada = suma(peso·pilar) / suma(peso·2) · 100  →  -100 … +100
"""
from bisect import bisect_right

import indicadores as ind

PESOS = {"P1": 1.0, "P2": 1.25, "P3": 1.0, "P4": 1.0, "P5": 0.75, "P6": 1.0, "P7": 0.75}
NOMBRES = {"P1": "Tendencia diaria", "P2": "Tendencia 4 h", "P3": "Momento", "P4": "Volumen y flujo",
           "P5": "Fuerza vs BTC", "P6": "Régimen de mercado", "P7": "Derivados"}

UMBRAL_COMPRA = 45
UMBRAL_COMPRA_RIESGO = 60     # si el régimen de mercado es negativo
UMBRAL_VENTA = -35
MAX_EXTENSION_ATR = 3.0       # (precio - EMA20 4h) / ATR 4h
STOP_ATR = 2.0
TRAIL_ATR = 3.0
COMISION_IDA_VUELTA = 0.0018  # Revolut X taker 0,09 % × 2


def clip(x, a=-2, b=2):
    return max(a, min(b, x))


def pct(a, b):
    return (a / b - 1) * 100 if (a and b) else None


def preparar(v4, vD):
    """Calcula todas las series una vez. v4/vD: dict con t, ct, o, h, l, c, v (listas)."""
    s = {"v4": v4, "vD": vD}
    c, h, l, vol = v4["c"], v4["h"], v4["l"], v4["v"]
    s["ema20"], s["ema50"] = ind.ema(c, 20), ind.ema(c, 50)
    s["rsi"] = ind.rsi(c, 14)
    _, _, s["hist"] = ind.macd(c)
    s["atr"] = ind.atr(h, l, c, 14)
    s["dmax20"] = ind.maximo_previo(h, 20)
    s["min10"] = ind.minimo_previo(l, 10)
    s["volma20"] = ind.sma(vol, 20) if any(vol) else [None] * len(c)
    s["obv"] = ind.obv(c, vol) if any(vol) else None
    if vD and vD["c"]:
        cd = vD["c"]
        s["d_ema50"], s["d_ema200"] = ind.ema(cd, 50), ind.ema(cd, 200)
        s["d_adx"] = ind.adx(vD["h"], vD["l"], cd, 14)
        s["d_rsi"] = ind.rsi(cd, 14)
    return s


def indice_diario(s, t_cierre_4h):
    """Última vela diaria cerrada antes del cierre de la vela de 4 h."""
    vD = s["vD"]
    if not vD or not vD["ct"]:
        return None
    j = bisect_right(vD["ct"], t_cierre_4h) - 1
    return j if j >= 0 else None


# ---------------------------------------------------------------- pilares
def p1_tendencia_diaria(s, j):
    if j is None:
        return None, "Sin velas diarias."
    c = s["vD"]["c"][j]
    e50, e200, adx = s["d_ema50"][j], s["d_ema200"][j], s["d_adx"][j]
    if e50 is None:
        return None, "Historial diario insuficiente."
    txt = []
    if e200 is not None:
        v = (1 if c > e200 else -1) + (1 if e50 > e200 else -1)
        txt.append(f"Cierre diario {'por encima' if c > e200 else 'por debajo'} de la EMA200"
                   f" y EMA50 {'>' if e50 > e200 else '<'} EMA200.")
    else:
        v = 1 if c > e50 else -1
        txt.append(f"Menos de 200 días de historial; cierre {'sobre' if c > e50 else 'bajo'} la EMA50.")
    if adx is not None:
        if adx < 18:
            v = v / 2
            txt.append(f"ADX {adx:.0f}: mercado lateral, la tendencia pesa la mitad.")
        else:
            txt.append(f"ADX {adx:.0f}: {'tendencia fuerte' if adx >= 25 else 'tendencia moderada'}.")
    return clip(v), " ".join(txt)


def p2_tendencia_4h(s, i):
    c = s["v4"]["c"][i]
    e20, e50 = s["ema20"][i], s["ema50"][i]
    if e50 is None:
        return None, "Historial de 4 h insuficiente."
    if c > e50 and e20 > e50:
        return 2, "Precio sobre la EMA50 de 4 h y EMA20 > EMA50: tendencia alcista de corto plazo."
    if c < e50 and e20 < e50:
        return -2, "Precio bajo la EMA50 de 4 h y EMA20 < EMA50: tendencia bajista de corto plazo."
    return 0, "Medias de 4 h cruzadas: sin tendencia clara."


def p3_momento(s, i):
    r, h, h1 = s["rsi"][i], s["hist"][i], s["hist"][i - 1] if i > 0 else None
    if r is None or h is None or h1 is None:
        return None, "Sin datos de momento."
    v, txt = 0, []
    if h > 0 and h > h1:
        v += 1
        txt.append("MACD positivo y acelerando.")
    elif h < 0 and h < h1:
        v -= 1
        txt.append("MACD negativo y empeorando.")
    else:
        txt.append("MACD girándose.")
    if r > 78:
        v -= 1
        txt.append(f"RSI {r:.0f}: sobrecompra.")
    elif 50 <= r <= 70:
        v += 1
        txt.append(f"RSI {r:.0f}: impulso sano.")
    elif r < 40:
        v -= 1
        txt.append(f"RSI {r:.0f}: impulso débil.")
    else:
        txt.append(f"RSI {r:.0f}.")
    return clip(v), " ".join(txt)


def p4_volumen(s, i):
    vol, c = s["v4"]["v"], s["v4"]["c"]
    ma = s["volma20"][i]
    if not ma or s["obv"] is None:
        return None, "La fuente de precio no da volumen."
    rvol = vol[i] / ma if ma else 0
    dmax = s["dmax20"][i]
    v, txt = 0, []
    if dmax and c[i] > dmax and rvol >= 1.5:
        v += 2
        txt.append(f"Ruptura del máximo de 20 velas con volumen {rvol:.1f}× la media.")
    elif rvol >= 1.2 and c[i] > c[i - 1]:
        v += 1
        txt.append(f"Vela alcista con volumen {rvol:.1f}× la media.")
    elif rvol >= 1.2 and c[i] < c[i - 1]:
        v -= 1
        txt.append(f"Vela bajista con volumen {rvol:.1f}× la media (presión vendedora).")
    else:
        txt.append(f"Volumen {rvol:.1f}× la media.")
    if i >= 10:
        d_obv = s["obv"][i] - s["obv"][i - 10]
        d_p = c[i] - c[i - 10]
        if d_p > 0 and d_obv < 0:
            v -= 1
            txt.append("Sube el precio pero el OBV cae: distribución.")
        elif d_p > 0 and d_obv > 0:
            v += 0.5
            txt.append("OBV acompaña la subida.")
        elif d_p < 0 and d_obv > 0:
            v += 0.5
            txt.append("Cae el precio pero el OBV sube: acumulación.")
    return clip(v), " ".join(txt), rvol


def p5_fuerza_relativa(s, i, btc4):
    """btc4: dict t -> cierre de BTC en 4 h."""
    t, c = s["v4"]["t"], s["v4"]["c"]
    if i < 42 or not btc4:
        return None, "Sin datos para comparar con BTC."
    b0, b1 = btc4.get(t[i - 42]), btc4.get(t[i])
    if not b0 or not b1:
        return None, "Sin datos para comparar con BTC."
    dif = pct(c[i], c[i - 42]) - pct(b1, b0)
    if dif > 15:
        v = 2
    elif dif > 5:
        v = 1
    elif dif < -15:
        v = -2
    elif dif < -5:
        v = -1
    else:
        v = 0
    return v, f"En 7 días lo hace {abs(dif):.1f} puntos {'mejor' if dif >= 0 else 'peor'} que BTC."


def p6_regimen(reg, es_btc=False):
    if not reg:
        return None, "Sin datos de mercado."
    v, txt = 0, []
    if reg.get("btc_sobre_ema200") is True:
        v += 1
        txt.append("BTC sobre su EMA200 diaria.")
    elif reg.get("btc_sobre_ema200") is False:
        v -= 1
        txt.append("BTC bajo su EMA200 diaria (mercado bajista).")
    fg = reg.get("fear_greed")
    if fg is not None:
        if fg >= 80:
            v -= 1
            txt.append(f"Fear & Greed {fg}: euforia, riesgo de corrección.")
        elif fg <= 20:
            txt.append(f"Fear & Greed {fg}: miedo extremo (históricamente buena zona de compra a medio plazo).")
        else:
            txt.append(f"Fear & Greed {fg}.")
    dd = reg.get("dominancia_7d")
    if dd is not None and not es_btc:
        if dd > 1:
            v -= 1
            txt.append(f"Dominancia de BTC +{dd:.1f} pp en 7 días: el dinero sale de las altcoins.")
        elif dd < -1:
            v += 1
            txt.append(f"Dominancia de BTC {dd:.1f} pp en 7 días: rotación hacia altcoins.")
    return clip(v), " ".join(txt)


def p7_derivados(der, cambio_24h):
    if not der:
        return None, "Sin mercado de futuros en Hyperliquid."
    v, txt = 0, []
    fa = der.get("funding_anual")
    if fa is not None:
        if fa > 100:
            v -= 2
            txt.append(f"Funding {fa:.0f} % anual: largos muy apalancados, riesgo de barrida.")
        elif fa > 40:
            v -= 1
            txt.append(f"Funding {fa:.0f} % anual: mercado cargado de largos.")
        elif fa < 0 and (cambio_24h or 0) > 0:
            v += 1
            txt.append(f"Funding negativo ({fa:.0f} % anual) con el precio subiendo: combustible para un short squeeze.")
        else:
            txt.append(f"Funding {fa:.0f} % anual: neutral.")
    oi = der.get("oi_24h")
    if oi is not None and cambio_24h is not None:
        if oi > 10 and cambio_24h > 0:
            v += 1
            txt.append(f"Interés abierto +{oi:.0f} % con el precio al alza: entra dinero nuevo.")
        elif oi > 10 and cambio_24h < 0:
            v -= 1
            txt.append(f"Interés abierto +{oi:.0f} % con el precio a la baja: se acumulan cortos.")
        elif oi < -10 and cambio_24h > 0:
            txt.append(f"Interés abierto {oi:.0f} % con subida: cierre de cortos, subida menos sólida.")
    return clip(v), " ".join(txt)


# ---------------------------------------------------------------- evaluación
def evaluar(s, i, btc4=None, regimen=None, derivados=None, cambio_24h=None, es_btc=False):
    """Evalúa la vela de 4 h cerrada i. Devuelve nota, pilares y datos técnicos."""
    j = indice_diario(s, s["v4"]["ct"][i])
    pil = {}
    r = p1_tendencia_diaria(s, j); pil["P1"] = r[:2]
    r = p2_tendencia_4h(s, i); pil["P2"] = r[:2]
    r = p3_momento(s, i); pil["P3"] = r[:2]
    r = p4_volumen(s, i); pil["P4"] = r[:2]
    rvol = r[2] if len(r) > 2 else None
    pil["P5"] = p5_fuerza_relativa(s, i, btc4) if not es_btc else (None, "Es BTC.")
    if regimen is not None:
        pil["P6"] = p6_regimen(regimen, es_btc)
    if derivados is not None or regimen is not None:
        pil["P7"] = p7_derivados(derivados, cambio_24h)
    num = sum(PESOS[k] * v for k, (v, _) in pil.items() if v is not None)
    den = sum(PESOS[k] * 2 for k, (v, _) in pil.items() if v is not None)
    nota = round(100 * num / den) if den else 0
    cobertura = sum(1 for v, _ in pil.values() if v is not None)
    return {
        "nota": nota, "pilares": pil, "cobertura": cobertura,
        "atr": s["atr"][i], "ema20": s["ema20"][i], "ema50": s["ema50"][i],
        "rsi": s["rsi"][i], "rvol": rvol, "min10": s["min10"][i], "cierre": s["v4"]["c"][i],
        "d_rsi": s["d_rsi"][j] if (j is not None and "d_rsi" in s) else None,
    }


def decidir(ev, precio, en_posicion=False, stop_activo=None, liquida=True):
    """Convierte la evaluación en señal. precio = precio vivo (misma moneda que las velas)."""
    nota, pil = ev["nota"], ev["pilares"]
    p1 = pil["P1"][0] if pil["P1"][0] is not None else 0
    p2 = pil["P2"][0] if pil["P2"][0] is not None else 0
    p3 = pil["P3"][0] if pil["P3"][0] is not None else 0
    p6 = pil.get("P6", (None,))[0]
    atr, e20 = ev["atr"], ev["ema20"]
    ext = (precio - e20) / atr if (atr and e20) else 0
    motivo = []

    if en_posicion and stop_activo and precio <= stop_activo:
        return "VENDER", ["El precio ha tocado el stop."], ext
    if nota <= UMBRAL_VENTA:
        return "VENDER", [f"Nota {nota} ≤ {UMBRAL_VENTA}."], ext
    if p2 <= -1 and p3 <= -1:
        return "VENDER", ["Tendencia y momento de 4 h se han girado a la baja."], ext

    umbral = UMBRAL_COMPRA_RIESGO if (p6 is not None and p6 < 0) else UMBRAL_COMPRA
    condiciones = nota >= umbral and p2 >= 1 and p1 >= 0
    if condiciones and not en_posicion:
        if not liquida:
            return "NEUTRAL", ["Cumple la señal, pero la liquidez es demasiado baja."], ext
        if ext > MAX_EXTENSION_ATR:
            return "ESPERAR", [f"Señal válida pero el precio está {ext:.1f} ATR sobre la EMA20: espera un retroceso."], ext
        motivo.append(f"Nota {nota} ≥ {umbral}, tendencia 4 h alcista y diaria no bajista.")
        return "COMPRAR", motivo, ext
    if en_posicion:
        return "MANTENER", ["La tendencia sigue en pie."], ext
    return "NEUTRAL", [], ext


def niveles(ev, precio, maximo_desde_entrada=None):
    """Stop, objetivos y tamaño. Todo en la moneda de las velas."""
    atr = ev["atr"]
    if not atr or not precio:
        return None
    stop = precio - STOP_ATR * atr
    if ev.get("min10"):
        stop = max(stop, ev["min10"] - 0.25 * atr) if ev["min10"] < precio else stop
    stop = min(stop, precio * 0.995)
    riesgo = precio - stop
    riesgo_pct = riesgo / precio
    trail = (maximo_desde_entrada or precio) - TRAIL_ATR * atr
    return {
        "entrada": precio, "stop": stop, "riesgo_pct": riesgo_pct,
        "t1": precio + 1.5 * riesgo, "t2": precio + 3 * riesgo,
        "trailing": trail,
        "tamano_max": min(1.0, 0.01 / riesgo_pct) if riesgo_pct > 0 else None,
        "atr_pct": atr / precio,
    }


# ---------------------------------------------------------------- backtest
def backtest(s, btc4, desde=210):
    """Simula la estrategia en velas de 4 h cerradas (pilares P1–P5; sin P6/P7,
    que no tienen historial). Entrada a la apertura de la vela siguiente.
    Salidas: stop, trailing de 3 ATR, o señal de venta al cierre."""
    v4 = s["v4"]
    o, h, l, c = v4["o"], v4["h"], v4["l"], v4["c"]
    n = len(c)
    ops, pos = [], None
    for i in range(desde, n - 1):
        if s["ema50"][i] is None or s["atr"][i] is None:
            continue
        if pos:
            if l[i] <= pos["stop"]:
                salida = o[i] if o[i] < pos["stop"] else pos["stop"]
                ops.append(_cerrar(pos, salida, i, "stop"))
                pos = None
                continue
            pos["max"] = max(pos["max"], h[i])
            pos["stop"] = max(pos["stop"], pos["max"] - TRAIL_ATR * s["atr"][i])
        ev = evaluar(s, i, btc4)
        senal, _, _ = decidir(ev, c[i], en_posicion=bool(pos), stop_activo=pos["stop"] if pos else None)
        if pos and senal == "VENDER":
            ops.append(_cerrar(pos, o[i + 1], i + 1, "señal"))
            pos = None
        elif not pos and senal == "COMPRAR":
            entrada = o[i + 1]
            nv = niveles(ev, c[i])
            if nv:
                stop = entrada - (c[i] - nv["stop"])
                pos = {"entrada": entrada, "stop": stop, "max": entrada, "i": i + 1}
    if pos:
        ops.append(_cerrar(pos, c[-1], n - 1, "abierta"))
    return resumen(ops, c[desde] if n > desde else None, c[-1] if c else None)


def _cerrar(pos, salida, i, motivo):
    r = salida / pos["entrada"] - 1 - COMISION_IDA_VUELTA
    return {"ret": r, "velas": i - pos["i"], "motivo": motivo}


def resumen(ops, p0=None, p1=None):
    cerradas = [x for x in ops if x["motivo"] != "abierta"]
    g = [x["ret"] for x in cerradas if x["ret"] > 0]
    p = [-x["ret"] for x in cerradas if x["ret"] <= 0]
    total = 1.0
    for x in cerradas:
        total *= 1 + x["ret"]
    return {
        "operaciones": len(cerradas),
        "aciertos": (len(g) / len(cerradas)) if cerradas else None,
        "media": (sum(x["ret"] for x in cerradas) / len(cerradas)) if cerradas else None,
        "profit_factor": (sum(g) / sum(p)) if p and g else (None if not g else 99.0),
        "compuesto": total - 1 if cerradas else None,
        "comprar_y_mantener": (p1 / p0 - 1) if (p0 and p1) else None,
        "velas_medias": (sum(x["velas"] for x in cerradas) / len(cerradas)) if cerradas else None,
    }

#!/usr/bin/env python3
"""
Investigación de estrategias con validación fuera de muestra (walk-forward simple).

- Universo: pares USD de Revolut X con suficiente liquidez (volumen y spread reales).
- Historia: velas diarias de Binance (~1000 días) y 4 h (~500 días).
- Costes por lado: comisión taker Revolut X 0,09 % + medio spread real del par + 0,05 % de deslizamiento.
- Periodo de entrenamiento (60 %) para elegir parámetros entre pocas opciones fijadas de antemano;
  el 40 % final solo se usa para medir (fuera de muestra).

Salida: resultados.json
"""
import json
import math
import sys
import time
from statistics import mean, pstdev

import fuentes as f
import indicadores as ind
import estrategia as est

REVX = "https://revx.revolut.com/api"
COMISION = 0.0009
DESLIZ = 0.0005
MIN_VOL_USD = 100_000        # volumen 24 h mínimo en Revolut X
MAX_SPREAD = 0.006           # spread máximo aceptado (0,6 %)
log = lambda *a: print(*a, flush=True)


# ------------------------------------------------------------------ datos
def universo_revolut_x():
    try:
        t = f.pedir(f"{REVX}/1.0/public/tickers?region=EEA")["data"]
    except Exception as e:  # noqa: BLE001
        log("Revolut X tickers no disponible:", e)
        return None
    out = {}
    for x in t:
        if not x["symbol"].endswith("/USD"):
            continue
        try:
            bid, ask, mid = float(x["bid"]), float(x["ask"]), float(x["mid"])
            qv = float(x.get("quote_volume_24h") or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if mid <= 0 or ask < bid:
            continue
        out[x["symbol"].split("/")[0]] = {"spread": (ask - bid) / mid, "vol_usd": qv}
    return out


def velas_binance(par, marco, total):
    """Pagina hacia atrás hasta 'total' velas cerradas."""
    filas, fin = [], None
    while len(filas) < total:
        url = f"{f.BINANCE}/klines?symbol={par}&interval={marco}&limit=1000" + (f"&endTime={fin}" if fin else "")
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
    v = {"t": [], "ct": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    for k in filas:
        v["t"].append(int(k[0])); v["ct"].append(int(k[6]))
        v["o"].append(float(k[1])); v["h"].append(float(k[2])); v["l"].append(float(k[3]))
        v["c"].append(float(k[4])); v["v"].append(float(k[7]))
    return v


# ------------------------------------------------------------------ métricas
def metricas(curva, dias_ano=365):
    """curva: lista de valores de cartera diarios."""
    if len(curva) < 3:
        return {}
    rets = [curva[i] / curva[i - 1] - 1 for i in range(1, len(curva))]
    años = len(rets) / dias_ano
    cagr = (curva[-1] / curva[0]) ** (1 / años) - 1 if años > 0 and curva[-1] > 0 else -1
    sd = pstdev(rets)
    sharpe = (mean(rets) / sd * math.sqrt(dias_ano)) if sd else 0
    neg = [r for r in rets if r < 0]
    sortino = (mean(rets) / pstdev(neg) * math.sqrt(dias_ano)) if len(neg) > 2 and pstdev(neg) else 0
    pico, dd = curva[0], 0
    for x in curva:
        pico = max(pico, x)
        dd = min(dd, x / pico - 1)
    return {"total": curva[-1] / curva[0] - 1, "cagr": cagr, "sharpe": sharpe, "sortino": sortino, "max_dd": dd}


def stats_ops(ops):
    if not ops:
        return {"operaciones": 0}
    g = [x for x in ops if x > 0]
    p = [-x for x in ops if x <= 0]
    return {"operaciones": len(ops), "aciertos": len(g) / len(ops), "media": mean(ops),
            "profit_factor": (sum(g) / sum(p)) if p else None}


# ------------------------------------------------------------------ simulador de cartera diario
class Cartera:
    """Posiciones de igual peso (1/K del capital al entrar). Ejecución a la apertura siguiente."""

    def __init__(self, K, coste):
        self.K, self.coste = K, coste
        self.caja, self.pos, self.ops, self.curva, self.exp = 1.0, {}, [], [], []

    def valor(self, precios):
        return self.caja + sum(p["uds"] * precios.get(t, p["ult"]) for t, p in self.pos.items())

    def comprar(self, t, precio, eq, stop=None):
        if t in self.pos or len(self.pos) >= self.K:
            return
        c = self.coste[t]
        importe = min(self.caja, eq / self.K)
        if importe <= eq * 0.02:
            return
        uds = importe * (1 - c) / precio
        self.caja -= importe
        self.pos[t] = {"uds": uds, "entrada": precio, "ult": precio, "stop": stop, "max": precio, "coste_in": importe}

    def vender(self, t, precio):
        p = self.pos.pop(t, None)
        if not p:
            return
        neto = p["uds"] * precio * (1 - self.coste[t])
        self.caja += neto
        self.ops.append(neto / p["coste_in"] - 1)


def alinear(datos, tickers):
    """Devuelve lista de fechas comunes (t de apertura diaria) e índices por moneda."""
    fechas = sorted({t for tk in tickers for t in datos[tk]["t"]})
    idx = {tk: {t: i for i, t in enumerate(datos[tk]["t"])} for tk in tickers}
    return fechas, idx


def simular(nombre, datos, tickers, btc, coste, regla, K, desde, hasta):
    """regla(dia_i, fecha, contexto) -> (lista de (ticker, stop) a comprar, set de tickers a vender).
    Señales al cierre del día i; ejecución a la apertura del día i+1."""
    fechas, idx = alinear(datos, tickers)
    fechas = [x for x in fechas if desde <= x <= hasta]
    car = Cartera(K, coste)
    for n, fecha in enumerate(fechas[:-1]):
        sig = fechas[n + 1]
        cierres = {tk: datos[tk]["c"][idx[tk][fecha]] for tk in tickers if fecha in idx[tk]}
        eq = car.valor(cierres)
        car.curva.append(eq)
        car.exp.append(len(car.pos) / K)
        # stops intradía del día siguiente (se comprueban con el mínimo del día siguiente)
        comprar, vender = regla(fecha, idx, car, cierres)
        aperturas = {tk: datos[tk]["o"][idx[tk][sig]] for tk in tickers if sig in idx[tk]}
        for tk in list(vender):
            if tk in car.pos and tk in aperturas:
                car.vender(tk, aperturas[tk])
        for tk, stop in comprar:
            if tk in aperturas:
                car.comprar(tk, aperturas[tk], eq, stop)
        # gestión del día siguiente: stop tocado
        for tk in list(car.pos):
            if sig not in idx[tk]:
                continue
            j = idx[tk][sig]
            p = car.pos[tk]
            p["ult"] = datos[tk]["c"][j]
            if p["stop"] and datos[tk]["l"][j] <= p["stop"]:
                salida = min(datos[tk]["o"][j], p["stop"])
                car.vender(tk, salida)
    return {"nombre": nombre, **metricas(car.curva), **stats_ops(car.ops),
            "exposicion": mean(car.exp) if car.exp else 0}


# ------------------------------------------------------------------ estrategias diarias
def prep_diario(datos, tickers):
    S = {}
    for tk in tickers:
        v = datos[tk]
        c, h, l = v["c"], v["h"], v["l"]
        S[tk] = {"sma50": ind.sma(c, 50), "sma100": ind.sma(c, 100), "sma200": ind.sma(c, 200),
                 "atr": ind.atr(h, l, c, 14), "rsi3": ind.rsi(c, 3), "sma5": ind.sma(c, 5),
                 "dmax": {n: ind.maximo_previo(h, n) for n in (20, 55)},
                 "dmin": {n: ind.minimo_previo(l, n) for n in (10, 20)}}
    return S


def regimen_btc(datos, S, fecha, idx, sma="sma200"):
    j = idx["BTC"].get(fecha)
    if j is None:
        return False
    m = S["BTC"][sma][j]
    return bool(m and datos["BTC"]["c"][j] > m)


def regla_donchian(datos, S, tickers, entrada, salida, filtro):
    def regla(fecha, idx, car, cierres):
        comprar, vender = [], set()
        ok = regimen_btc(datos, S, fecha, idx) if filtro else True
        cand = []
        for tk in tickers:
            j = idx[tk].get(fecha)
            if j is None:
                continue
            c = datos[tk]["c"][j]
            if tk in car.pos:
                p = car.pos[tk]
                p["max"] = max(p["max"], datos[tk]["h"][j])
                a = S[tk]["atr"][j]
                if a:
                    p["stop"] = max(p["stop"] or 0, p["max"] - 3 * a)
                mn = S[tk]["dmin"][salida][j]
                if mn and c < mn:
                    vender.add(tk)
                continue
            mx, a = S[tk]["dmax"][entrada][j], S[tk]["atr"][j]
            if ok and mx and a and c > mx:
                cand.append((c / mx, tk, c - 2 * a))
        cand.sort(reverse=True)
        comprar = [(tk, st) for _, tk, st in cand]
        return comprar, vender
    return regla


def regla_momentum(datos, S, tickers, lookback, K, filtro, cada=7):
    estado = {"n": 0}

    def regla(fecha, idx, car, cierres):
        estado["n"] += 1
        if estado["n"] % cada:
            return [], set()
        ok = regimen_btc(datos, S, fecha, idx) if filtro else True
        rank = []
        for tk in tickers:
            j = idx[tk].get(fecha)
            if j is None or j < lookback:
                continue
            c0 = datos[tk]["c"][j - lookback]
            rank.append((datos[tk]["c"][j] / c0 - 1, tk))
        rank.sort(reverse=True)
        top = {tk for r, tk in rank[:K] if r > 0} if ok else set()
        vender = {tk for tk in car.pos if tk not in top}
        comprar = [(tk, None) for tk in [t for _, t in rank[:K]] if tk in top and tk not in car.pos]
        return comprar, vender
    return regla


def regla_reversion(datos, S, tickers, umbral, filtro):
    def regla(fecha, idx, car, cierres):
        comprar, vender = [], set()
        ok = regimen_btc(datos, S, fecha, idx) if filtro else True
        cand = []
        for tk in tickers:
            j = idx[tk].get(fecha)
            if j is None:
                continue
            c = datos[tk]["c"][j]
            if tk in car.pos:
                p = car.pos[tk]
                p["dias"] = p.get("dias", 0) + 1
                s5 = S[tk]["sma5"][j]
                if (s5 and c > s5) or p["dias"] >= 7:
                    vender.add(tk)
                continue
            r3, s100, a = S[tk]["rsi3"][j], S[tk]["sma100"][j], S[tk]["atr"][j]
            if ok and r3 is not None and s100 and a and c > s100 and r3 < umbral:
                cand.append((r3, tk, c - 2.5 * a))
        cand.sort()
        return [(tk, st) for _, tk, st in cand], vender
    return regla


def regla_tendencia_btc_eth(datos, S, tickers):
    def regla(fecha, idx, car, cierres):
        comprar, vender = [], set()
        for tk in tickers:
            j = idx[tk].get(fecha)
            if j is None:
                continue
            c, s50, s200 = datos[tk]["c"][j], S[tk]["sma50"][j], S[tk]["sma200"][j]
            dentro = bool(s50 and s200 and c > s50 and s50 > s200)
            if tk in car.pos and not dentro:
                vender.add(tk)
            elif tk not in car.pos and dentro:
                comprar.append((tk, None))
        return comprar, vender
    return regla


def regla_comprar_mantener(tickers):
    def regla(fecha, idx, car, cierres):
        return [(tk, None) for tk in tickers if tk not in car.pos], set()
    return regla


# ------------------------------------------------------------------ principal
def main():
    t0 = time.time()
    rx = universo_revolut_x()
    revol = f.revolut_mercado()
    bn = f.binance_simbolos()
    candidatos = []
    for m in revol.values():
        tk = m["ticker"]
        if tk in {"USDC", "USDT", "EURC", "DAI"} or f"{tk}USDT" not in bn:
            continue
        info = (rx or {}).get(tk)
        if rx is not None:
            if not info or info["vol_usd"] < MIN_VOL_USD or info["spread"] > MAX_SPREAD:
                continue
        candidatos.append((tk, info["spread"] if info else 0.003))
    if "BTC" not in [c[0] for c in candidatos]:
        candidatos.append(("BTC", 0.0002))
    log(f"Universo: {len(candidatos)} monedas líquidas en Revolut X con par en Binance")

    datos, coste = {}, {}
    for tk, spread in candidatos:
        try:
            v = velas_binance(f"{tk}USDT", "1d", 1000)
            if len(v["c"]) >= 260:
                datos[tk] = v
                coste[tk] = COMISION + spread / 2 + DESLIZ
        except Exception as e:  # noqa: BLE001
            log("datos", tk, e)
    tickers = sorted(datos)
    log(f"Con historia suficiente: {len(tickers)}")
    S = prep_diario(datos, tickers)

    todas = sorted({t for tk in tickers for t in datos[tk]["t"]})
    inicio = todas[210]                      # deja calentar SMA200
    corte = todas[210 + int((len(todas) - 210) * 0.6)]
    fin = todas[-1]
    periodos = {"entrenamiento": (inicio, corte), "fuera_de_muestra": (corte, fin), "completo": (inicio, fin)}
    fecha = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))

    candidatas = []
    for filtro in (False, True):
        for e, s in ((20, 10), (55, 20)):
            candidatas.append((f"Ruptura Donchian {e}/{s} días" + (" + filtro BTC" if filtro else ""),
                               "donchian", lambda e=e, s=s, filtro=filtro: regla_donchian(datos, S, tickers, e, s, filtro), 5))
        for lb in (14, 28):
            candidatas.append((f"Momentum relativo {lb} d, top 5, semanal" + (" + filtro BTC" if filtro else ""),
                               "momentum", lambda lb=lb, filtro=filtro: regla_momentum(datos, S, tickers, lb, 5, filtro), 5))
        for u in (10, 20):
            candidatas.append((f"Reversión RSI(3)<{u} en tendencia" + (" + filtro BTC" if filtro else ""),
                               "reversion", lambda u=u, filtro=filtro: regla_reversion(datos, S, tickers, u, filtro), 5))
    bench = [
        ("BTC comprar y mantener", "bench", lambda: regla_comprar_mantener(["BTC"]), 1, ["BTC"]),
        ("Cesta igual peso comprar y mantener", "bench", lambda: regla_comprar_mantener(tickers), len(tickers), tickers),
    ]
    if "ETH" in tickers:
        bench.append(("Tendencia BTC+ETH (SMA50>SMA200)", "bench",
                      lambda: regla_tendencia_btc_eth(datos, S, ["BTC", "ETH"]), 2, ["BTC", "ETH"]))

    resultados = []
    for nombre, familia, fabrica, K in candidatas:
        fila = {"nombre": nombre, "familia": familia}
        for per, (a, b) in periodos.items():
            fila[per] = simular(nombre, datos, tickers, "BTC", coste, fabrica(), K, a, b)
        resultados.append(fila)
        log(f"{nombre}: IS Sharpe {fila['entrenamiento'].get('sharpe', 0):.2f} · OOS Sharpe {fila['fuera_de_muestra'].get('sharpe', 0):.2f}")
    for nombre, familia, fabrica, K, tks in bench:
        fila = {"nombre": nombre, "familia": familia}
        for per, (a, b) in periodos.items():
            fila[per] = simular(nombre, datos, tks, "BTC", coste, fabrica(), K, a, b)
        resultados.append(fila)
        log(f"{nombre}: OOS Sharpe {fila['fuera_de_muestra'].get('sharpe', 0):.2f}")

    # selección honesta: la mejor de cada familia SEGÚN ENTRENAMIENTO, medida fuera de muestra
    seleccion = {}
    for fam in ("donchian", "momentum", "reversion"):
        mejores = sorted([r for r in resultados if r["familia"] == fam],
                         key=lambda r: r["entrenamiento"].get("sharpe", -9), reverse=True)
        if mejores:
            seleccion[fam] = mejores[0]["nombre"]

    # estrategia actual (4 h) con costes reales, en velas de 4 h
    actual = []
    for tk in tickers:
        try:
            v4 = velas_binance(f"{tk}USDT", "4h", 3000)
            d = datos[tk]
            s = est.preparar(v4, d)
            btc4 = None
            r_ops = []
            orig = est.COMISION_IDA_VUELTA
            est.COMISION_IDA_VUELTA = 2 * coste[tk]
            r = est.backtest(s, btc4)
            est.COMISION_IDA_VUELTA = orig
            if r["operaciones"]:
                actual.append(r)
        except Exception as e:  # noqa: BLE001
            log("4h", tk, e)
    n = sum(r["operaciones"] for r in actual)
    actual_res = {"operaciones": n,
                  "media": (sum(r["media"] * r["operaciones"] for r in actual) / n) if n else None,
                  "aciertos": (sum(r["aciertos"] * r["operaciones"] for r in actual) / n) if n else None}

    out = {
        "generado": int(time.time()), "segundos": round(time.time() - t0),
        "universo": {"monedas": tickers, "n": len(tickers), "revolut_x_disponible": rx is not None,
                     "coste_por_lado": {tk: round(coste[tk], 5) for tk in tickers},
                     "filtros": {"min_vol_usd": MIN_VOL_USD, "max_spread": MAX_SPREAD}},
        "periodos": {k: [fecha(a), fecha(b)] for k, (a, b) in periodos.items()},
        "resultados": resultados, "seleccion_por_entrenamiento": seleccion,
        "estrategia_actual_4h": actual_res,
    }
    with open("resultados.json", "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    log("Listo en", round(time.time() - t0), "s")


if __name__ == "__main__":
    main()

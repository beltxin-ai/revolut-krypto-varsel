#!/usr/bin/env python3
"""
Radar Cripto · motor principal. Se ejecuta cada 15 minutos.

1. Universo y precio en NOK: Revolut.
2. Velas con volumen: Binance → OKX (validadas contra el precio de Revolut) → Revolut.
3. Contexto: BTC, Fear & Greed, dominancia (CoinGecko), derivados (Hyperliquid).
4. Estrategia de confluencia (estrategia.py) → COMPRAR / MANTENER / ESPERAR / NEUTRAL / VENDER.
5. Posiciones virtuales para medir resultados reales de las señales.
6. Backtest diario en velas de 4 h.
7. Notificaciones: nueva COMPRA confirmada y VENTA / stop de posiciones abiertas.
"""
import json
import os
import pickle
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import estrategia as est
import fuentes as f

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
PANEL_URL = os.getenv("PANEL_URL", "").rstrip("/")
MIN_VOLUM_NOK = float(os.getenv("MIN_VOLUM_NOK", "5000000"))
CONFIRMACIONES = int(os.getenv("CONFIRMACIONES", "2"))  # revisiones seguidas antes de dar COMPRAR
DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent / "data"))
CACHE_DIR = Path(os.getenv("CACHE_DIR", Path(__file__).parent / "cache"))
ESTABLES = {"USDC", "USDT", "DAI", "EURC", "PYUSD", "RLUSD", "USDE", "FDUSD", "TUSD", "USDS", "USD1"}

log = print


def ruta_estado():
    return DATA_DIR / "state" / "estado.json"


# ------------------------------------------------------------------ utilidades
def es(num, dec=2):
    if num is None:
        return "–"
    t = f"{num:,.{dec}f}" if abs(num) >= 1 else f"{num:.4g}"
    return t.replace(",", "X").replace(".", ",").replace("X", ".")


def nok(p):
    return f"{es(p)} NOK" if p is not None else "–"


def enviar(titulo, texto, click=None, prioridad=4, tags="chart_with_upwards_trend"):
    if not NTFY_TOPIC:
        log("[ntfy] sin tema:", titulo)
        return
    cuerpo = {"topic": NTFY_TOPIC, "title": titulo, "message": texto, "priority": prioridad,
              "tags": tags.split(",")}
    if click:
        cuerpo["click"] = click
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(cuerpo).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        log("[ntfy]", r.status, titulo)


def cargar_json(p, defecto):
    try:
        return json.loads(Path(p).read_text())
    except Exception:  # noqa: BLE001
        return defecto


def enlace(t):
    return f"{PANEL_URL}/?moneda={urllib.parse.quote(t)}" if PANEL_URL else None


# ------------------------------------------------------------------ contexto de mercado
def precio_btc_usd():
    for url, leer in (
        (f"{f.BINANCE}/ticker/price?symbol=BTCUSDT", lambda d: float(d["price"])),
        (f"{f.OKX}/market/ticker?instId=BTC-USDT", lambda d: float(d["data"][0]["last"])),
    ):
        try:
            return leer(f.pedir(url))
        except Exception as e:  # noqa: BLE001
            log("precio BTC:", e)
    try:
        return f.hyperliquid()["BTC"]["precio"]
    except Exception:  # noqa: BLE001
        return None


def actualizar_contexto(estado, ahora):
    """Fear & Greed y dominancia cada hora; guarda historial de dominancia 8 días."""
    ctx = estado.setdefault("contexto", {})
    fok = estado.setdefault("fuentes", {})
    if ahora - ctx.get("t_hora", 0) >= 3600:
        try:
            ctx["fear_greed"] = f.fear_greed()
            fok["Fear & Greed"] = "OK"
        except Exception as e:  # noqa: BLE001
            fok["Fear & Greed"] = f"no disponible: {str(e)[:60]}"
        try:
            g = f.coingecko_global()
            hist = ctx.setdefault("dominancia_hist", [])
            hist.append([ahora, g["dominancia_btc"]])
            ctx["dominancia_hist"] = [x for x in hist if ahora - x[0] < 8 * 86400]
            ctx["dominancia"] = g["dominancia_btc"]
            ctx["cambio_mcap_24h"] = g["cambio_mcap_24h"]
            fok["CoinGecko"] = "OK"
        except Exception as e:  # noqa: BLE001
            fok["CoinGecko"] = f"no disponible: {str(e)[:60]}"
        ctx["t_hora"] = ahora
    dh = ctx.get("dominancia_hist", [])
    viejo = [x for x in dh if ahora - x[0] >= 6.5 * 86400]
    ctx["dominancia_7d"] = (dh[-1][1] - viejo[-1][1]) if (viejo and dh) else None
    return ctx


def derivados(estado, ahora):
    fok = estado.setdefault("fuentes", {})
    try:
        hl = f.hyperliquid()
        fok["Hyperliquid"] = f"OK ({len(hl)} perpetuos)"
    except Exception as e:  # noqa: BLE001
        fok["Hyperliquid"] = f"no disponible: {str(e)[:60]}"
        return {}
    hist = estado.setdefault("oi_hist", {})
    for t, d in hl.items():
        h = hist.setdefault(t, [])
        if not h or ahora - h[-1][0] >= 3600:
            h.append([ahora, d["oi_usd"]])
        hist[t] = [x for x in h if ahora - x[0] < 27 * 3600]
        viejo = [x for x in hist[t] if ahora - x[0] >= 23 * 3600]
        d["oi_24h"] = ((d["oi_usd"] / viejo[0][1] - 1) * 100) if (viejo and viejo[0][1]) else None
    return hl


# ------------------------------------------------------------------ velas con caché
def velas_moneda(fila, fuente_velas, cache, usdnok, ahora_ms):
    t = fila["ticker"]
    c = cache.get(t)
    if c and c["v"]["4h"]["ct"] and c["v"]["1d"]["ct"]:
        fresco = (ahora_ms < c["v"]["4h"]["ct"][-1] + f.H4 + 90_000
                  and ahora_ms < c["v"]["1d"]["ct"][-1] + f.D1 + 90_000)
        if fresco and (c["fuente"] != "Revolut" or ahora_ms - c.get("t", 0) < 3600_000):
            return c
    nombre, v, desv = fuente_velas.obtener(fila, usdnok, ahora_ms)
    if v:
        cache[t] = {"fuente": nombre, "v": v, "desv": desv, "t": ahora_ms}
    else:
        v = fuente_velas.respaldo_revolut(fila, ahora_ms)
        if not v:
            return None
        cache[t] = {"fuente": "Revolut", "v": v, "desv": 0.0, "t": ahora_ms}
    return cache[t]


# ------------------------------------------------------------------ principal
def main():
    t0 = time.time()
    ahora = int(t0)
    ahora_ms = ahora * 1000
    estado = cargar_json(ruta_estado(), {})
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cache = pickle.loads((CACHE_DIR / "velas.pkl").read_bytes())
    except Exception:  # noqa: BLE001
        cache = {}

    fok = estado.setdefault("fuentes", {})
    mercado = f.revolut_mercado()
    fok["Revolut"] = f"OK ({len(mercado)} monedas)"

    btc_usd = precio_btc_usd()
    usdnok = (mercado["BTC"]["pris"] / btc_usd) if (btc_usd and "BTC" in mercado) else estado.get("usdnok")
    estado["usdnok"] = usdnok

    fv = f.Velas(log)
    fok.update(fv.estado)
    ctx = actualizar_contexto(estado, ahora)
    hl = derivados(estado, ahora)

    # ---- BTC primero: régimen de mercado y referencia de fuerza relativa
    btc = velas_moneda(mercado["BTC"], fv, cache, usdnok, ahora_ms) if "BTC" in mercado else None
    btc_s = est.preparar(btc["v"]["4h"], btc["v"]["1d"]) if btc else None
    btc4 = dict(zip(btc["v"]["4h"]["t"], btc["v"]["4h"]["c"])) if btc else {}
    regimen = {"fear_greed": (ctx.get("fear_greed") or {}).get("valor"),
               "dominancia_7d": ctx.get("dominancia_7d")}
    if btc_s and btc_s.get("d_ema200") and btc_s["d_ema200"][-1]:
        cd = btc["v"]["1d"]["c"]
        regimen["btc_sobre_ema200"] = cd[-1] > btc_s["d_ema200"][-1]
        e50 = btc_s["d_ema50"]
        regimen["btc_ema50_sube"] = bool(e50[-1] and e50[-6] and e50[-1] > e50[-6])

    # ---- velas del resto en paralelo
    universo = [m for m in mercado.values() if m["ticker"] not in ESTABLES]
    f.en_paralelo(lambda m: velas_moneda(m, fv, cache, usdnok, ahora_ms), universo, hilos=8)
    fuentes_usadas = {}
    for m in universo:
        c = cache.get(m["ticker"])
        if c:
            fuentes_usadas[c["fuente"]] = fuentes_usadas.get(c["fuente"], 0) + 1

    pos = estado.setdefault("posiciones", {})
    cerradas = estado.setdefault("cerradas", [])
    pend = estado.setdefault("pendientes", {})
    filas, detalles = [], {}

    for m in universo:
        s_, t = m["symbol"], m["ticker"]
        c = cache.get(t)
        fila = {k: m.get(k) for k in ("ticker", "pris", "h1", "d1", "w1", "volum", "mcap")}
        fila.update({"senal": "SIN DATOS", "nota": None, "fuente": c["fuente"] if c else None})
        if not c or len(c["v"]["4h"]["c"]) < 30:
            filas.append(fila)
            continue
        # NOK por unidad de vela (USDT en exchanges, NOK en Revolut), ajustado al precio de Revolut
        conv = 1.0 if c["fuente"] == "Revolut" else (usdnok or 0) / (1 + (c.get("desv") or 0))
        if not conv:
            filas.append(fila)
            continue
        try:
            s = est.preparar(c["v"]["4h"], c["v"]["1d"])
            i = len(c["v"]["4h"]["c"]) - 1
            ev = est.evaluar(s, i, btc4, regimen, hl.get(t), m.get("d1"), es_btc=(t == "BTC"))
        except Exception as e:  # noqa: BLE001
            log("evaluación", t, e)
            filas.append(fila)
            continue
        precio = m["pris"] / conv if m.get("pris") else ev["cierre"]
        p = pos.get(s_)
        liquida = (m.get("volum") or 0) >= MIN_VOLUM_NOK
        senal, motivo, ext = est.decidir(ev, precio, en_posicion=bool(p),
                                         stop_activo=(p["stop"] / conv) if p else None, liquida=liquida)

        # confirmación: COMPRAR debe repetirse CONFIRMACIONES revisiones seguidas
        pe = pend.get(s_, {"senal": None, "n": 0})
        pe = {"senal": senal, "n": pe["n"] + 1 if pe["senal"] == senal else 1}
        pend[s_] = pe
        nv = est.niveles(ev, precio)
        nv_nok = {k: (v * conv if (k in ("entrada", "stop", "t1", "t2", "trailing") and v) else v)
                  for k, v in (nv or {}).items()}
        if senal == "COMPRAR" and pe["n"] < CONFIRMACIONES and not p:
            motivo = motivo + [f"Pendiente de confirmar ({pe['n']}/{CONFIRMACIONES})."]

        if senal == "COMPRAR" and not p and pe["n"] >= CONFIRMACIONES and nv:
            p = pos[s_] = {"ticker": t, "entrada": m["pris"], "stop": nv_nok["stop"],
                           "stop_inicial": nv_nok["stop"], "t1": nv_nok["t1"], "t2": nv_nok["t2"],
                           "maximo": m["pris"], "t": ahora, "nota": ev["nota"], "fuente": c["fuente"],
                           "riesgo_pct": nv["riesgo_pct"]}
            try:
                enviar(f"🟢 COMPRAR {t} · nota {ev['nota']}",
                       f"Entrada ~{nok(m['pris'])}\n"
                       f"Stop {nok(p['stop'])} (−{es(nv['riesgo_pct'] * 100, 1)} %)\n"
                       f"Objetivos {nok(p['t1'])} · {nok(p['t2'])}\n"
                       f"Tamaño máx. {es(min(100, (nv['tamano_max'] or 0) * 100), 0)} % del capital (riesgo 1 %)\n"
                       f"{ev['pilares']['P2'][1]}",
                       click=enlace(t), tags="green_circle")
            except Exception as e:  # noqa: BLE001
                log("ntfy", e)
        elif p:
            p["maximo"] = max(p["maximo"], m["pris"])
            if ev["atr"]:
                p["stop"] = max(p["stop"], p["maximo"] - est.TRAIL_ATR * ev["atr"] * conv)
            p["t1_alcanzado"] = bool(p.get("t1_alcanzado") or m["pris"] >= p["t1"])
            p["ret_actual"] = m["pris"] / p["entrada"] - 1
            tocado = m["pris"] <= p["stop"]
            if senal == "VENDER" or tocado:
                ret = m["pris"] / p["entrada"] - 1
                cerradas.insert(0, dict(p, salida=m["pris"], t_salida=ahora,
                                        ret=ret - est.COMISION_IDA_VUELTA,
                                        motivo="stop" if tocado else "señal"))
                pos.pop(s_, None)
                try:
                    enviar(f"🔴 VENDER {t} · {'stop' if tocado else 'señal'}",
                           f"Precio {nok(m['pris'])} · entrada {nok(p['entrada'])}\n"
                           f"Resultado {'+' if ret >= 0 else ''}{es(ret * 100, 1)} % (antes de comisiones)\n"
                           f"{(motivo or [''])[0]}",
                           click=enlace(t), tags="red_circle")
                except Exception as e:  # noqa: BLE001
                    log("ntfy", e)
                senal = "VENDER"

        fila.update({"senal": senal, "nota": ev["nota"], "rsi": ev["rsi"], "rvol": ev["rvol"],
                     "ext": ext, "cobertura": ev["cobertura"],
                     "pil": {k: v[0] for k, v in ev["pilares"].items()},
                     "funding": (hl.get(t) or {}).get("funding_anual"),
                     "en_posicion": s_ in pos, "liquida": liquida})
        filas.append(fila)
        v4, n = c["v"]["4h"], 150
        detalles[t] = {
            "ticker": t, "symbol": s_, "fila": fila, "motivo": motivo,
            "pilares": {k: {"v": v[0], "texto": v[1], "nombre": est.NOMBRES[k], "peso": est.PESOS[k]}
                        for k, v in ev["pilares"].items()},
            "niveles": nv_nok, "fuente": c["fuente"], "desviacion": c.get("desv"),
            "derivados": hl.get(t), "posicion": pos.get(s_), "actualizado": ahora,
            "grafica": {"t": v4["t"][-n:], "c": [x * conv for x in v4["c"][-n:]],
                        "ema20": [x * conv if x else None for x in s["ema20"][-n:]],
                        "ema50": [x * conv if x else None for x in s["ema50"][-n:]]},
        }

    # ---- backtest una vez al día (UTC)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dia = time.strftime("%Y-%m-%d", time.gmtime(ahora))
    bt = cargar_json(DATA_DIR / "backtest.json", {})
    if estado.get("backtest_dia") != dia or not bt:
        bt = hacer_backtest(cache, btc4, universo)
        bt["dia"] = dia
        estado["backtest_dia"] = dia
        (DATA_DIR / "backtest.json").write_text(json.dumps(bt, ensure_ascii=False))
    for t, d in detalles.items():
        d["backtest"] = (bt.get("monedas") or {}).get(t)

    # ---- guardar
    estado["cerradas"] = cerradas[:300]
    estado["pendientes"] = {k: v for k, v in pend.items() if k in mercado}
    ruta_estado().parent.mkdir(parents=True, exist_ok=True)
    ruta_estado().write_text(json.dumps(estado, ensure_ascii=False))
    (CACHE_DIR / "velas.pkl").write_bytes(pickle.dumps(cache))
    (DATA_DIR / "monedas").mkdir(parents=True, exist_ok=True)
    for t, d in detalles.items():
        (DATA_DIR / "monedas" / f"{t}.json").write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")))

    real = est.resumen([{"ret": x["ret"], "velas": 0, "motivo": x["motivo"]} for x in cerradas])
    (DATA_DIR / "data.json").write_text(json.dumps({
        "actualizado": ahora, "duracion_s": round(time.time() - t0),
        "usdnok": usdnok, "regimen": regimen,
        "contexto": {k: v for k, v in ctx.items() if k != "dominancia_hist"},
        "fuentes": fok, "fuentes_velas": fuentes_usadas,
        "filas": filas, "posiciones": list(pos.values()), "cerradas": cerradas[:100],
        "resultado_real": real, "backtest": bt.get("global"), "backtest_dia": bt.get("dia"),
        "parametros": {"min_volumen_nok": MIN_VOLUM_NOK, "umbral_compra": est.UMBRAL_COMPRA,
                       "umbral_venta": est.UMBRAL_VENTA, "confirmaciones": CONFIRMACIONES,
                       "comision": est.COMISION_IDA_VUELTA},
    }, ensure_ascii=False, separators=(",", ":")))
    cuenta = {}
    for x in filas:
        cuenta[x["senal"]] = cuenta.get(x["senal"], 0) + 1
    log(f"OK {len(filas)} monedas en {time.time() - t0:.0f}s · {cuenta} · velas {fuentes_usadas}")


def hacer_backtest(cache, btc4, universo):
    validas, por_moneda = [], {}
    for m in universo:
        c = cache.get(m["ticker"])
        if not c or c["fuente"] == "Revolut" or len(c["v"]["4h"]["c"]) < 300:
            continue
        try:
            r = est.backtest(est.preparar(c["v"]["4h"], c["v"]["1d"]), btc4)
        except Exception as e:  # noqa: BLE001
            log("backtest", m["ticker"], e)
            continue
        por_moneda[m["ticker"]] = r
        if r["operaciones"]:
            validas.append(r)
    n = sum(r["operaciones"] for r in validas)
    glob = {"monedas": len(por_moneda), "operaciones": n}
    if n:
        glob["aciertos"] = sum((r["aciertos"] or 0) * r["operaciones"] for r in validas) / n
        glob["media"] = sum((r["media"] or 0) * r["operaciones"] for r in validas) / n
        pfs = sorted(r["profit_factor"] for r in validas if r["profit_factor"] is not None)
        glob["profit_factor_mediana"] = pfs[len(pfs) // 2] if pfs else None
        glob["monedas_rentables"] = sum(1 for r in validas if (r["compuesto"] or 0) > 0) / len(validas)
        glob["supera_comprar_y_mantener"] = sum(
            1 for r in validas if r["compuesto"] is not None and r["comprar_y_mantener"] is not None
            and r["compuesto"] > r["comprar_y_mantener"]) / len(validas)
    return {"global": glob, "monedas": por_moneda}


if __name__ == "__main__":
    if "--test" in sys.argv:
        enviar("✅ Notificación de prueba", "Tu radar cripto está conectado al móvil.", prioridad=3,
               tags="white_check_mark")
    else:
        main()

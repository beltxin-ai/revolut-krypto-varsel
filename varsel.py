#!/usr/bin/env python3
"""
Radar Cripto · motor para Revolut X. Se ejecuta cada 15 minutos.

- Precios en vivo, spread y volumen: Revolut X (pares USD, región EEA).
- Velas diarias para las señales: Binance (validadas contra el precio de Revolut X).
- Señales (sistema.py) al cierre diario UTC; stops vigilados cada 15 min con el bid de Revolut X.
- Cartera modelo con ejecución realista: compra al ask, venta al bid, comisión 0,09 %.
- Notificaciones: ENTRADA y SALIDA / STOP.
"""
import json
import os
import pickle
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import fuentes as f
import sistema as sis

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
PANEL_URL = os.getenv("PANEL_URL", "").rstrip("/")
DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent / "data"))
CACHE_DIR = Path(os.getenv("CACHE_DIR", Path(__file__).parent / "cache"))
REVX = "https://revx.revolut.com/api"
COMISION = 0.0009
MIN_VOL_USD = float(os.getenv("MIN_VOL_USD", "100000"))
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.006"))
ESTABLES = {"USDC", "USDT", "EURC", "DAI", "USDE", "PYUSD", "RLUSD", "FDUSD", "TUSD", "USDS", "USD1"}
D1 = 86400 * 1000
H1 = 3600 * 1000
log = print


# ------------------------------------------------------------------ utilidades
def fmt(x, dec=None):
    if x is None:
        return "–"
    if dec is None:
        dec = 2 if abs(x) >= 1 else 6
    t = f"{x:,.{dec}f}" if abs(x) >= 1 else f"{x:.4g}"
    return t.replace(",", "X").replace(".", ",").replace("X", ".")


def usd(x):
    return f"{fmt(x)} $"


def enviar(titulo, texto, click=None, prioridad=4, tags="chart_with_upwards_trend"):
    if not NTFY_TOPIC:
        log("[ntfy] sin tema:", titulo)
        return
    cuerpo = {"topic": NTFY_TOPIC, "title": titulo, "message": texto, "priority": prioridad, "tags": tags.split(",")}
    if click:
        cuerpo["click"] = click
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(cuerpo).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        log("[ntfy]", r.status, titulo)


def avisar(*a, **k):
    try:
        enviar(*a, **k)
    except Exception as e:  # noqa: BLE001
        log("ntfy falló:", e)


def cargar(p, defecto):
    try:
        return json.loads(Path(p).read_text())
    except Exception:  # noqa: BLE001
        return defecto


def enlace(t=None):
    return (f"{PANEL_URL}/?moneda={urllib.parse.quote(t)}" if t else PANEL_URL) if PANEL_URL else None


def dia_utc(ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


# ------------------------------------------------------------------ datos
def revolut_x():
    d = f.pedir(f"{REVX}/1.0/public/tickers?region=EEA")["data"]
    out = {}
    for x in d:
        if not x["symbol"].endswith("/USD"):
            continue
        try:
            bid, ask, mid = float(x["bid"]), float(x["ask"]), float(x["mid"])
        except (TypeError, ValueError, KeyError):
            continue
        if mid <= 0 or ask < bid:
            continue
        t = x["symbol"].split("/")[0]
        out[t] = {"bid": bid, "ask": ask, "mid": mid, "spread": (ask - bid) / mid,
                  "vol_usd": float(x.get("quote_volume_24h") or 0),
                  "cambio_24h": (float(x["price_change_24h"]) / (mid - float(x["price_change_24h"])))
                  if x.get("price_change_24h") not in (None, "") and mid != float(x["price_change_24h"]) else None}
    return out


def velas_diarias(t, cache, ahora_ms):
    c = cache.get(t)
    if c and c["ct"] and ahora_ms < c["ct"][-1] + D1 + 5 * 60_000:
        return c
    try:
        v = f.solo_cerradas(f.binance_velas(f"{t}USDT", "1d", 400), ahora_ms)
    except Exception as e:  # noqa: BLE001
        log("velas", t, e)
        return c
    if len(v["c"]) >= 60:
        cache[t] = v
    return cache.get(t)


# ------------------------------------------------------------------ cartera modelo
def valorar(est, rx):
    """Valor de la cartera modelo (capital inicial 1,0) a precios bid de Revolut X."""
    caja = est["caja"]
    abierto = 0.0
    for bloque in ("nucleo", "satelite"):
        for t, p in est[bloque].items():
            px = (rx.get(t) or {}).get("bid") or p["ultimo"]
            abierto += p["unidades"] * px * (1 - COMISION)
    return caja + abierto


def comprar(est, bloque, t, rx, peso, extra):
    q = rx.get(t)
    if not q:
        return None
    equity = valorar(est, rx)
    importe = min(est["caja"], equity * peso)
    if importe < equity * 0.02:
        return None
    precio = q["ask"]
    uds = importe * (1 - COMISION) / precio
    est["caja"] -= importe
    p = {"ticker": t, "entrada": precio, "unidades": uds, "importe": importe, "t": int(time.time()),
         "ultimo": precio, "maximo": precio, "peso": peso, **extra}
    est[bloque][t] = p
    return p


def vender(est, bloque, t, rx, motivo):
    p = est[bloque].pop(t, None)
    if not p:
        return None
    precio = (rx.get(t) or {}).get("bid") or p["ultimo"]
    neto = p["unidades"] * precio * (1 - COMISION)
    est["caja"] += neto
    cerr = dict(p, salida=precio, t_salida=int(time.time()), ret=neto / p["importe"] - 1,
                motivo=motivo, bloque=bloque)
    est["cerradas"].insert(0, cerr)
    return cerr


# ------------------------------------------------------------------ principal
def main():
    t0 = time.time()
    ahora_ms = int(t0 * 1000)
    est = cargar(DATA_DIR / "state" / "cartera.json", None) or {
        "caja": 1.0, "nucleo": {}, "satelite": {}, "cerradas": [], "curva": [], "ult_dia": None, "inicio": int(t0)}
    if not est.get("senales"):     # posiciones abiertas antes de existir el historial de señales
        est["senales"] = [{"t": p["t"], "dia": est.get("ult_dia"), "tipo": "entrada", "ticker": p["ticker"],
                           "sistema": p.get("sistema"), "precio": p["entrada"], "stop": p.get("stop"), "peso": p.get("peso")}
                          for b in ("nucleo", "satelite") for p in est[b].values()]
    fuentes = {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cache = pickle.loads((CACHE_DIR / "diario.pkl").read_bytes())
    except Exception:  # noqa: BLE001
        cache = {}

    rx = revolut_x()
    fuentes["Revolut X"] = f"OK ({len(rx)} pares USD)"
    try:
        bpx = {x["symbol"]: float(x["price"]) for x in f.pedir(f"{f.BINANCE}/ticker/price") if x["symbol"].endswith("USDT")}
        fuentes["Binance"] = f"OK ({len(bpx)} pares)"
    except Exception as e:  # noqa: BLE001
        bpx = {}
        fuentes["Binance"] = f"no disponible: {str(e)[:60]}"
    # validación cruzada: mismo activo si el precio en vivo de Binance y Revolut X difiere < 3 %
    bn = {f"{t}USDT" for t, q in rx.items() if bpx.get(f"{t}USDT") and abs(bpx[f"{t}USDT"] / q["mid"] - 1) < 0.03}
    if not bpx:
        bn = {f"{t}USDT" for t in cache}

    # universo: pares líquidos de Revolut X con datos diarios
    universo = sorted(t for t, q in rx.items()
                      if t not in ESTABLES and f"{t}USDT" in bn
                      and ((q["vol_usd"] >= MIN_VOL_USD and q["spread"] <= MAX_SPREAD) or t in sis.NUCLEO))
    datos, series = {}, {}
    f.en_paralelo(lambda t: velas_diarias(t, cache, ahora_ms), universo, hilos=6)
    for t in universo:
        v = cache.get(t)
        if not v:
            continue
        datos[t], series[t] = v, sis.preparar(v)
    for t in list(est["nucleo"]) + list(est["satelite"]):   # nunca perder de vista una posición abierta
        if t not in datos and cache.get(t):
            datos[t], series[t] = cache[t], sis.preparar(cache[t])

    if "BTC" not in datos:
        raise RuntimeError("Sin velas diarias de BTC: no se puede calcular el régimen")
    reg = sis.regimen_btc(datos["BTC"], series["BTC"])
    dia = dia_utc(datos["BTC"]["t"][-1])        # última vela diaria cerrada
    eventos = []

    # ---- 1) proceso diario (una vez por vela diaria cerrada)
    if est["ult_dia"] != dia:
        log("Nuevo cierre diario:", dia)
        # núcleo
        for t in sis.NUCLEO:
            if t not in datos:
                continue
            dentro = sis.nucleo_dentro(datos[t], series[t])
            if t in est["nucleo"] and not dentro:
                c = vender(est, "nucleo", t, rx, "tendencia rota (cierre bajo SMA50 o SMA50 < SMA200)")
                if c:
                    eventos.append(("salida", c))
            elif t not in est["nucleo"] and dentro:
                p = comprar(est, "nucleo", t, rx, sis.PESO_NUCLEO, {"stop": None, "sistema": "Núcleo"})
                if p:
                    eventos.append(("entrada", p))
        # satélite: trailing y salidas por mínimo de 20 días
        for t in list(est["satelite"]):
            if t not in datos:
                continue
            p = sis.actualizar_trailing(est["satelite"][t], datos[t], series[t])
            if sis.salida_diaria(p, datos[t], series[t]):
                c = vender(est, "satelite", t, rx, "cierre bajo el mínimo de 20 días")
                if c:
                    eventos.append(("salida", c))
        # satélite: entradas
        if reg["alcista"]:
            libres = sis.MAX_SATELITE - len(est["satelite"])
            for cand in sis.candidatas_satelite(datos, series, universo, set(est["satelite"]))[:max(0, libres)]:
                t = cand["ticker"]
                if rx[t]["bid"] <= cand["stop"]:
                    continue    # desde el cierre ya ha caído hasta el stop: la ruptura ha fallado
                # niveles en USDT de Binance ≈ USD de Revolut X (validado arriba, < 3 % de diferencia)
                p = comprar(est, "satelite", t, rx, sis.PESO_SATELITE,
                            {"stop": cand["stop"], "stop_inicial": cand["stop"],
                             "sistema": "Satélite", "fuerza": cand["fuerza"],
                             "nivel_ruptura": series[t]["max55"][-1]})
                if p:
                    p["maximo"] = datos[t]["h"][-1]
                    eventos.append(("entrada", p))
        est["ult_dia"] = dia
        est["curva"].append([int(t0), valorar(est, rx), rx.get("BTC", {}).get("mid")])

    # ---- 2) vigilancia intradía de stops (cada 15 min)
    for t in list(est["satelite"]):
        p = est["satelite"][t]
        q = rx.get(t)
        if not q:
            continue
        p["ultimo"] = q["bid"]
        if p.get("stop") and q["bid"] <= p["stop"]:
            c = vender(est, "satelite", t, rx, "stop tocado")
            if c:
                eventos.append(("stop", c))
    for t, p in est["nucleo"].items():
        if rx.get(t):
            p["ultimo"] = rx[t]["bid"]

    # ---- 2b) capa horaria (NO validada): protección con la última vela de 1 h cerrada
    alertas = []
    btc_1h = None
    try:
        vb = f.solo_cerradas(f.binance_velas("BTCUSDT", "1h", 30), ahora_ms)
        btc_1h = vb["c"][-1] / vb["o"][-1] - 1
        if btc_1h <= sis.CAIDA_BTC_1H and est["satelite"]:
            alertas.append({"tipo": "btc", "ticker": "BTC", "t": vb["t"][-1],
                            "texto": f"BTC {fmt(btc_1h * 100, 1)} % en 1 h: reducir a la mitad las altcoins"})
    except Exception as e:  # noqa: BLE001
        log("velas 1h BTC", e)
    for t, p in est["satelite"].items():
        if not p.get("nivel_ruptura") and t in datos:
            p["nivel_ruptura"] = sis.nivel_ruptura_entrada(datos[t], series[t], p["t"])
        try:
            v1 = f.solo_cerradas(f.binance_velas(f"{t}USDT", "1h", 30), ahora_ms)
        except Exception as e:  # noqa: BLE001
            log("velas 1h", t, e)
            continue
        if sis.proteccion_horaria(v1, p.get("nivel_ruptura")):
            alertas.append({"tipo": "ruptura", "ticker": t, "t": v1["t"][-1], "nivel": p["nivel_ruptura"],
                            "texto": "Vela de 1 h bajo el nivel de ruptura con volumen > 2× la media: reducir a la mitad"})
    avisadas = {k: v for k, v in est.get("avisos_h", {}).items() if t0 - v < 2 * 86400}
    nuevas_alertas = []
    for a in alertas:
        k = f"{a['tipo']}-{a['ticker']}-{a['t']}"
        if k not in avisadas:
            avisadas[k] = int(t0)
            nuevas_alertas.append(a)
    est["avisos_h"] = avisadas

    # ---- 3) historial de señales y latidos (prueba de que la revisión se ejecuta)
    for tipo, p in eventos:
        est.setdefault("senales", []).insert(0, {
            "t": int(t0), "dia": dia, "tipo": tipo, "ticker": p["ticker"], "sistema": p.get("sistema"),
            "precio": p["entrada"] if tipo == "entrada" else p.get("salida"), "stop": p.get("stop"),
            "peso": p.get("peso"), "motivo": p.get("motivo"), "ret": p.get("ret")})
    est["senales"] = est.get("senales", [])[:60]
    est["latidos"] = ([int(t0)] + est.get("latidos", []))[:96]    # últimas 24 h a 15 min

    # ---- 4) notificaciones
    equity = valorar(est, rx)
    for tipo, p in eventos:
        t = p["ticker"]
        if tipo == "entrada":
            riesgo = (1 - p["stop"] / p["entrada"]) if p.get("stop") else None
            q = rx.get(t, {})
            lim = sis.precio_limite(q.get("bid"), datos[t]["c"][-1], q.get("cambio_24h"))
            sug = sis.tamano_por_riesgo(p["peso"], lim, p.get("stop"))
            avisar(f"🟢 COMPRAR {t} · {p['sistema']}",
                   f"Precio Revolut X ~{usd(p['entrada'])} (ask)\n"
                   f"Tamaño del sistema: {fmt(p['peso'] * 100, 0)} % de tu capital para trading\n"
                   + (f"Tamaño para arriesgar 1 %: {fmt(sug * 100, 1)} % (no validado)\n" if sug < p["peso"] - 1e-6 else "")
                   + f"Límite sugerido: {usd(lim)} (no validado)\n"
                   + (f"Stop: {usd(p['stop'])} (−{fmt(riesgo * 100, 1)} %). Sube solo con el trailing.\n" if riesgo else
                      "Sin stop fijo: sales cuando se rompa la tendencia diaria.\n")
                   + "Usa orden limitada en Revolut X (comisión maker 0 %).",
                   click=enlace(t), tags="green_circle")
        else:
            avisar(f"🔴 VENDER {t} · {'STOP' if tipo == 'stop' else p['sistema']}",
                   f"Motivo: {p['motivo']}\nPrecio ~{usd(p['salida'])} (bid) · entrada {usd(p['entrada'])}\n"
                   f"Resultado neto: {'+' if p['ret'] >= 0 else ''}{fmt(p['ret'] * 100, 1)} %",
                   click=enlace(t), tags="red_circle", prioridad=5 if tipo == "stop" else 4)

    for a in nuevas_alertas:
        avisar(f"⚠️ PROTEGER {a['ticker']}", a["texto"] + "\nProtección no validada: el sistema no vende; tú decides.",
               click=enlace(a["ticker"]), tags="warning", prioridad=4)

    # ---- 5) datos para el panel
    filas = []
    libres = sis.MAX_SATELITE - len(est["satelite"])
    for t in sorted(datos):
        e = sis.estado_moneda(datos[t], series[t])
        q = rx.get(t, {})
        rec = sis.recomendacion(t, datos[t], series[t], q.get("mid"), est["nucleo"].get(t) or est["satelite"].get(t),
                                reg["alcista"], libres)
        pos = est["nucleo"].get(t) or est["satelite"].get(t)
        gest = None
        if pos and pos.get("stop") and q.get("bid"):
            dist = max(0.0, 1 - pos["stop"] / q["bid"])
            gest = {"riesgo": pos["peso"] * dist, "sugerido": sis.tamano_por_riesgo(pos["peso"], q["bid"], pos["stop"])}
        filas.append({"ticker": t, "precio": q.get("mid"), "spread": q.get("spread"), "vol_usd": q.get("vol_usd"),
                      "cambio_24h": q.get("cambio_24h"), **e, "rec": rec, "gestion": gest,
                      "nucleo": t in sis.NUCLEO,
                      "en_cartera": t in est["nucleo"] or t in est["satelite"],
                      "grafica": {"t": datos[t]["t"][-120:], "c": datos[t]["c"][-120:],
                                  "sma50": [x for x in series[t]["sma50"][-120:]],
                                  "max55": [x for x in series[t]["max55"][-120:]],
                                  "min20": [x for x in series[t]["min20"][-120:]]}})
    candidatas = sis.candidatas_satelite(datos, series, universo, set(est["satelite"]))
    cerca = sorted((x for x in filas if not x["nucleo"] and not x["en_cartera"] and x["dist_max55"] is not None
                    and -0.05 <= x["dist_max55"] <= 0), key=lambda x: -x["dist_max55"])

    ordenes = []      # señales de entrada de las últimas 24 h con la posición aún abierta
    for s_ in est["senales"]:
        p = est["nucleo"].get(s_["ticker"]) or est["satelite"].get(s_["ticker"])
        if s_["tipo"] != "entrada" or t0 - s_["t"] > 86400 or not p or s_["ticker"] not in datos:
            continue
        q = rx.get(s_["ticker"], {})
        lim = sis.precio_limite(q.get("bid"), datos[s_["ticker"]]["c"][-1], q.get("cambio_24h"))
        ordenes.append({"ticker": s_["ticker"], "sistema": p.get("sistema"), "limite": lim, "stop": p.get("stop"),
                        "peso": p["peso"], "sugerido": sis.tamano_por_riesgo(p["peso"], lim, p.get("stop")),
                        "precio": q.get("mid"), "cambio_24h": q.get("cambio_24h"), "t": s_["t"]})
    exposicion = 1 - est["caja"] / equity if equity else 0

    curva = est["curva"][-400:]
    est["cerradas"] = est["cerradas"][:300]
    ops = [c["ret"] for c in est["cerradas"]]
    ganadas = [x for x in ops if x > 0]
    perdidas = [-x for x in ops if x <= 0]
    resumen = {"valor": equity, "rentabilidad": equity - 1, "operaciones": len(ops),
               "aciertos": len(ganadas) / len(ops) if ops else None,
               "media": sum(ops) / len(ops) if ops else None,
               "profit_factor": (sum(ganadas) / sum(perdidas)) if (ganadas and perdidas) else None,
               "desde": est["inicio"],
               "btc_desde": (rx["BTC"]["mid"] / curva[0][2] - 1) if (curva and curva[0][2] and "BTC" in rx) else None}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "state").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "state" / "cartera.json").write_text(json.dumps(est, ensure_ascii=False))
    (CACHE_DIR / "diario.pkl").write_bytes(pickle.dumps(cache))
    (DATA_DIR / "radar.json").write_text(json.dumps({
        "actualizado": int(t0), "duracion_s": round(time.time() - t0), "ult_cierre": dia,
        "regimen": reg, "fuentes": fuentes, "universo_n": len(datos),
        "filtros": {"min_vol_usd": MIN_VOL_USD, "max_spread": MAX_SPREAD},
        "cartera": {"nucleo": list(est["nucleo"].values()), "satelite": list(est["satelite"].values()),
                    "caja": est["caja"], "resumen": resumen, "curva": curva, "cerradas": est["cerradas"][:100]},
        "candidatas_hoy": candidatas[:10], "cerca_de_ruptura": cerca[:15],
        "eventos": [{"tipo": tp, "ticker": p["ticker"]} for tp, p in eventos],
        "senales": est["senales"][:30], "ordenes": ordenes, "alertas_h": alertas, "btc_1h": btc_1h,
        "exposicion": exposicion, "gestion": {"riesgo_max": sis.RIESGO_MAX, "exposicion_max": sis.EXPOSICION_MAX,
                                              "caida_btc_1h": sis.CAIDA_BTC_1H, "vol_x": sis.VOL_X_PROTECCION}, "latidos": est["latidos"], "intervalo_s": int(os.getenv("INTERVALO_SEG", "900")),
        "filas": filas,
        "sistema": {"peso_nucleo": sis.PESO_NUCLEO, "peso_satelite": sis.PESO_SATELITE, "max_satelite": sis.MAX_SATELITE,
                    "entrada": sis.ENTRADA_N, "salida": sis.SALIDA_N, "stop_atr": sis.STOP_ATR, "trail_atr": sis.TRAIL_ATR},
    }, ensure_ascii=False, separators=(",", ":")))
    log(f"OK {len(datos)} monedas · régimen {'alcista' if reg['alcista'] else 'bajista'} · "
        f"cartera {fmt((equity - 1) * 100, 1)} % · eventos {len(eventos)} · {time.time() - t0:.0f}s")


if __name__ == "__main__":
    if "--test" in sys.argv:
        enviar("✅ Notificación de prueba", "Tu radar de Revolut X está conectado al móvil.", prioridad=3,
               tags="white_check_mark")
    else:
        main()

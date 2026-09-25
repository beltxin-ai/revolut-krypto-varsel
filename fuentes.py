"""
Fuentes de datos. Todas públicas y sin clave.

  Revolut      universo de monedas y precio en NOK (el precio al que operas)
  Binance      velas de 4 h y diarias con volumen (data-api.binance.vision)
  OKX          respaldo de velas si Binance no responde o no tiene la moneda
  Revolut      último respaldo de velas (sin volumen)
  Hyperliquid  funding e interés abierto de futuros perpetuos
  alternative.me  índice Fear & Greed
  CoinGecko    dominancia de BTC

Cada fuente se valida: las velas solo se usan si su último precio,
convertido a NOK, coincide con el de Revolut (±4 %).
"""
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
REVOLUT = "https://www.revolut.com/api/crypto/explore"
BINANCE = "https://data-api.binance.vision/api/v3"
OKX = "https://www.okx.com/api/v5"
HL = "https://api.hyperliquid.xyz/info"
H4 = 4 * 3600 * 1000
D1 = 24 * 3600 * 1000


def pedir(url, datos=None, intentos=2, timeout=20):
    err = None
    for k in range(intentos):
        try:
            cab = {"User-Agent": UA, "Accept": "application/json", "Accept-Language": "es-ES,es;q=0.9"}
            cuerpo = None
            if datos is not None:
                cuerpo = json.dumps(datos).encode()
                cab["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=cuerpo, headers=cab)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            err = e
            time.sleep(1.5 * (k + 1))
    raise RuntimeError(f"{url[:90]}: {err}")


def ticker(symbol):
    return symbol.split(":")[-1]


def pct(s):
    try:
        return float(str(s).replace("%", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ Revolut
def revolut_mercado(fiat="NOK", pais="NO"):
    datos = {}
    for rng, campo in (("1d", "d1"), ("1h", "h1"), ("1w", "w1")):
        for m in pedir(f"{REVOLUT}?fiatCurrency={fiat}&countryCode={pais}&range={rng}"):
            s = m["symbol"]
            fila = datos.setdefault(s, {
                "symbol": s, "ticker": ticker(s), "pris": m.get("currentRate"),
                "volum": (m.get("tradingVolume") or {}).get("amount", 0) / 100,  # viene en céntimos
                "mcap": (m.get("marketCap") or {}).get("amount", 0) / 100,
            })
            fila[campo] = pct(m.get("priceChange"))
    return datos


def revolut_detalles(symbol, fiat="NOK"):
    d = pedir(f"{REVOLUT}/{urllib.parse.quote(symbol, safe='')}/details?fiatCurrency={fiat}")
    for k in ("tradingVolume", "marketCap"):
        if isinstance(d.get(k), dict):
            d[k]["amount"] = d[k].get("amount", 0) / 100
    return d


def _velas_vacias():
    return {"t": [], "ct": [], "o": [], "h": [], "l": [], "c": [], "v": []}


def revolut_velas(symbol, marco, fiat="NOK"):
    """Respaldo sin volumen. marco '4h' (agregando 1 h de 1 semana) o '1d' (1 año)."""
    enc = urllib.parse.quote(symbol, safe="")
    if marco == "1d":
        g = pedir(f"{REVOLUT}/{enc}/chart?fiatCurrency={fiat}&interval=1d&range=1y")
        paso = D1
    else:
        g = pedir(f"{REVOLUT}/{enc}/chart?fiatCurrency={fiat}&interval=1h&range=1w")
        paso = 3600 * 1000
    pts = [(int(p["start"]), float(p["rate"])) for p in g.get("points", []) if p.get("rate")]
    if marco == "4h":  # agrupar horas en bloques de 4 h
        grupos = {}
        for t, r in pts:
            grupos.setdefault(t - t % H4, []).append(r)
        pts2 = sorted(grupos.items())
        v = _velas_vacias()
        for t, rs in pts2:
            v["t"].append(t); v["ct"].append(t + H4 - 1)
            v["o"].append(rs[0]); v["h"].append(max(rs)); v["l"].append(min(rs)); v["c"].append(rs[-1]); v["v"].append(0.0)
        return v
    v = _velas_vacias()
    prev = None
    for t, r in pts:
        v["t"].append(t); v["ct"].append(t + paso - 1)
        v["o"].append(prev if prev else r); v["h"].append(max(r, prev or r)); v["l"].append(min(r, prev or r))
        v["c"].append(r); v["v"].append(0.0)
        prev = r
    return v


# ------------------------------------------------------------------ Binance
def binance_simbolos():
    return {x["symbol"] for x in pedir(f"{BINANCE}/ticker/price") if x["symbol"].endswith("USDT")}


def binance_velas(par, marco, limite, tz=None):
    """tz: desplazamiento horario de Binance (p. ej. -6 → velas diarias de 06:00 a 06:00 UTC)."""
    datos = pedir(f"{BINANCE}/klines?symbol={par}&interval={marco}&limit={limite}" + (f"&timeZone={tz}" if tz else ""))
    v = _velas_vacias()
    for k in datos:
        v["t"].append(int(k[0])); v["ct"].append(int(k[6]))
        v["o"].append(float(k[1])); v["h"].append(float(k[2])); v["l"].append(float(k[3]))
        v["c"].append(float(k[4])); v["v"].append(float(k[7]))  # volumen en USDT
    return v


# ------------------------------------------------------------------ OKX
def okx_simbolos():
    d = pedir(f"{OKX}/public/instruments?instType=SPOT")
    return {x["instId"] for x in d.get("data", []) if x["instId"].endswith("-USDT")}


def okx_velas(inst, marco, limite):
    bar = {"1h": "1H", "4h": "4H", "1d": "1Dutc"}[marco]
    paso = {"1h": 3600 * 1000, "4h": H4, "1d": D1}[marco]
    filas, despues = [], None
    while len(filas) < limite:
        url = f"{OKX}/market/history-candles?instId={inst}&bar={bar}&limit=100" + (f"&after={despues}" if despues else "")
        d = pedir(url).get("data", [])
        if not d:
            break
        filas += d
        despues = d[-1][0]
        if len(d) < 100:
            break
    filas.sort(key=lambda k: int(k[0]))
    v = _velas_vacias()
    for k in filas[-limite:]:
        v["t"].append(int(k[0])); v["ct"].append(int(k[0]) + paso - 1)
        v["o"].append(float(k[1])); v["h"].append(float(k[2])); v["l"].append(float(k[3]))
        v["c"].append(float(k[4])); v["v"].append(float(k[7]))  # volumen en USDT
    return v


def solo_cerradas(v, ahora_ms):
    """Quita la vela en formación."""
    while v["ct"] and v["ct"][-1] > ahora_ms:
        for k in v:
            v[k].pop()
    return v


# ------------------------------------------------------------------ Hyperliquid
def hyperliquid():
    """Devuelve {TICKER: {funding_anual, oi_usd, precio}}."""
    meta, ctxs = pedir(HL, {"type": "metaAndAssetCtxs"})
    out = {}
    for a, c in zip(meta["universe"], ctxs):
        nombre = a["name"]
        mult = 1
        if nombre.startswith("k") and nombre[1:].isupper():  # kPEPE = 1000 PEPE
            nombre, mult = nombre[1:], 1000
        try:
            px = float(c.get("markPx") or 0) / mult
            out[nombre] = {
                "funding_anual": float(c.get("funding") or 0) * 24 * 365 * 100,
                "oi_usd": float(c.get("openInterest") or 0) * mult * px,
                "precio": px,
            }
        except (TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------------ Sentimiento
def fear_greed():
    d = pedir("https://api.alternative.me/fng/?limit=1")
    x = d["data"][0]
    return {"valor": int(x["value"]), "texto": x.get("value_classification")}


def coingecko_global():
    d = pedir("https://api.coingecko.com/api/v3/global")["data"]
    return {"dominancia_btc": d["market_cap_percentage"].get("btc"),
            "cambio_mcap_24h": d.get("market_cap_change_percentage_24h_usd")}


# ------------------------------------------------------------------ Selección de velas
class Velas:
    """Obtiene velas por moneda probando Binance → OKX → Revolut y valida el precio."""

    def __init__(self, log=print):
        self.log = log
        self.estado = {}
        try:
            self.bn = binance_simbolos()
            self.estado["Binance"] = f"OK ({len(self.bn)} pares)"
        except Exception as e:  # noqa: BLE001
            self.bn = set()
            self.estado["Binance"] = f"no disponible: {str(e)[:80]}"
        try:
            self.okx = okx_simbolos()
            self.estado["OKX"] = f"OK ({len(self.okx)} pares)"
        except Exception as e:  # noqa: BLE001
            self.okx = set()
            self.estado["OKX"] = f"no disponible: {str(e)[:80]}"

    def candidatos(self, t):
        c = []
        if f"{t}USDT" in self.bn:
            c.append(("Binance", lambda m, n: binance_velas(f"{t}USDT", m, n)))
        if f"{t}-USDT" in self.okx:
            c.append(("OKX", lambda m, n: okx_velas(f"{t}-USDT", m, n)))
        return c

    def obtener(self, fila, usdnok, ahora_ms, marcos=(("4h", 500), ("1d", 400))):
        """Devuelve (fuente, {marco: velas}, desviacion_precio) o (None, None, None)."""
        t = fila["ticker"]
        for nombre, f in self.candidatos(t):
            try:
                v4 = f("4h", 5)
                ultimo = v4["c"][-1] if v4["c"] else None
                if not ultimo or not usdnok or not fila.get("pris"):
                    continue
                desv = (ultimo * usdnok) / fila["pris"] - 1
                if abs(desv) > 0.04:
                    continue  # otra moneda con el mismo ticker, o precio no fiable
                out = {m: solo_cerradas(f(m, n), ahora_ms) for m, n in marcos}
                return nombre, out, desv
            except Exception as e:  # noqa: BLE001
                self.log(f"{nombre} {t}: {e}")
        return None, None, None

    def respaldo_revolut(self, fila, ahora_ms):
        try:
            return {"4h": solo_cerradas(revolut_velas(fila["symbol"], "4h"), ahora_ms),
                    "1d": solo_cerradas(revolut_velas(fila["symbol"], "1d"), ahora_ms)}
        except Exception as e:  # noqa: BLE001
            self.log(f"Revolut velas {fila['ticker']}: {e}")
            return None


def en_paralelo(func, elementos, hilos=8):
    with ThreadPoolExecutor(max_workers=hilos) as ex:
        return list(ex.map(func, elementos))

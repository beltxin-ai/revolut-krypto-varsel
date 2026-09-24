#!/usr/bin/env python3
"""
Revolut kryptovarsel
Sjekker alle kryptovalutaer i Revolut (Norge) og sender push-varsel via ntfy
når en mynt stiger mer enn terskelen på 24 t eller 1 t.

Kun standardbiblioteket. Kjøres av GitHub Actions hvert 15. minutt.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# ---------- Innstillinger (kan overstyres med miljøvariabler) ----------
TERSKEL_24T = float(os.getenv("TERSKEL_24T", "20"))      # % stigning på 24 t
TERSKEL_1T = float(os.getenv("TERSKEL_1T", "20"))        # % stigning på 1 t
MIN_VOLUM_NOK = float(os.getenv("MIN_VOLUM_NOK", "1000000"))  # min. handelsvolum 24 t
PAUSE_TIMER = float(os.getenv("PAUSE_TIMER", "12"))      # ikke varsle samme mynt igjen før ...
EKSTRA_PP = float(os.getenv("EKSTRA_PP", "15"))          # ... med mindre den har steget så mange %-poeng til
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
PANEL_URL = os.getenv("PANEL_URL", "").rstrip("/")
FIAT = "NOK"
LAND = "NO"
STABLECOINS = {"USDC", "USDT", "DAI", "EURC", "PYUSD", "RLUSD", "USDE", "FDUSD", "TUSD"}

ROT = Path(__file__).parent
STATUS_FIL = ROT / "state" / "status.json"
DOCS = ROT / "docs"

REVOLUT = "https://www.revolut.com/api/crypto/explore"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


def hent_json(url, forsok=3):
    feil = None
    for i in range(forsok):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json",
                "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            feil = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Klarte ikke å hente {url}: {feil}")


def ticker(symbol):
    """'X:8:ALEO' -> 'ALEO'"""
    return symbol.split(":")[-1]


def pst(s):
    try:
        return float(str(s).replace("%", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def hent_revolut():
    """Returnerer {symbol: {...}} med endring 1t/24t/7d, pris, volum, markedsverdi."""
    data = {}
    for rng, felt in (("1d", "d1"), ("1h", "h1"), ("1w", "w1")):
        url = f"{REVOLUT}?fiatCurrency={FIAT}&countryCode={LAND}&range={rng}"
        for m in hent_json(url):
            s = m["symbol"]
            rad = data.setdefault(s, {
                "symbol": s,
                "ticker": ticker(s),
                "pris": m.get("currentRate"),
                # Revolut oppgir beløp i øre -> del på 100
                "volum": (m.get("tradingVolume") or {}).get("amount", 0) / 100,
                "mcap": (m.get("marketCap") or {}).get("amount", 0) / 100,
            })
            rad[felt] = pst(m.get("priceChange"))
    return data


def hent_coingecko_reserve(tickere):
    """Reserve hvis Revolut blokkerer: CoinGecko topp 1000, matchet på ticker."""
    ut = {}
    for side in range(1, 5):
        q = urllib.parse.urlencode({
            "vs_currency": "nok", "order": "market_cap_desc", "per_page": 250,
            "page": side, "price_change_percentage": "1h,24h,7d"})
        for c in hent_json(f"https://api.coingecko.com/api/v3/coins/markets?{q}"):
            t = c["symbol"].upper()
            if t in tickere and t not in ut:  # høyest markedsverdi vinner ved like tickere
                ut[t] = {
                    "symbol": t, "ticker": t, "pris": c.get("current_price"),
                    "volum": c.get("total_volume") or 0, "mcap": c.get("market_cap") or 0,
                    "h1": c.get("price_change_percentage_1h_in_currency"),
                    "d1": c.get("price_change_percentage_24h_in_currency"),
                    "w1": c.get("price_change_percentage_7d_in_currency"),
                }
        time.sleep(3)
    return ut


def les_status():
    if STATUS_FIL.exists():
        try:
            return json.loads(STATUS_FIL.read_text())
        except json.JSONDecodeError:
            pass
    return {"sist_varslet": {}, "historikk": [], "tickere": [], "feil_varslet": False}


def send_ntfy(tittel, tekst, klikk=None, prioritet="high", tags="chart_with_upwards_trend"):
    if not NTFY_TOPIC:
        print("[ntfy] NTFY_TOPIC mangler – hopper over:", tittel)
        return
    prio = {"low": 2, "default": 3, "high": 4, "urgent": 5}.get(prioritet, 3)
    body = {"topic": NTFY_TOPIC, "title": tittel, "message": tekst,
            "priority": prio, "tags": tags.split(",")}
    if klikk:
        body["click"] = klikk
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        print("[ntfy]", r.status, tittel)


def fmt_nok(x):
    if x is None:
        return "–"
    if x >= 1e9:
        return f"{x/1e9:.1f} mrd kr"
    if x >= 1e6:
        return f"{x/1e6:.1f} mill kr"
    return f"{x:,.0f} kr".replace(",", " ")


def fmt_pris(p):
    if p is None:
        return "–"
    return f"{p:,.2f} kr".replace(",", " ") if p >= 1 else f"{p:.6g} kr"


def hent_detaljer(symbol):
    """Graf (7 d, timesoppløsning) og beskrivelse til analysepanelet."""
    enc = urllib.parse.quote(symbol, safe="")
    ut = {"symbol": symbol, "ticker": ticker(symbol), "oppdatert": int(time.time())}
    try:
        ut["graf_7d"] = hent_json(f"{REVOLUT}/{enc}/chart?fiatCurrency={FIAT}&interval=1h&range=1w")
    except Exception as e:  # noqa: BLE001
        print("graf feilet", symbol, e)
    try:
        ut["graf_1d"] = hent_json(f"{REVOLUT}/{enc}/chart?fiatCurrency={FIAT}&interval=5m&range=1d&countryCode={LAND}")
    except Exception as e:  # noqa: BLE001
        print("graf 1d feilet", symbol, e)
    try:
        d = hent_json(f"{REVOLUT}/{enc}/details?fiatCurrency={FIAT}")
        for k in ("tradingVolume", "marketCap"):
            if isinstance(d.get(k), dict):
                d[k]["amount"] = d[k].get("amount", 0) / 100
        ut["detaljer"] = d
    except Exception as e:  # noqa: BLE001
        print("detaljer feilet", symbol, e)
    return ut


def main():
    status = les_status()
    na = int(time.time())
    kilde = "revolut"
    try:
        marked = hent_revolut()
        status["tickere"] = sorted({v["ticker"] for v in marked.values()})
        if status.get("feil_varslet"):
            send_ntfy("Kryptovarsel virker igjen", "Revolut-data hentes normalt igjen.",
                      prioritet="low", tags="white_check_mark")
            status["feil_varslet"] = False
    except Exception as e:  # noqa: BLE001
        print("Revolut feilet:", e)
        kilde = "coingecko"
        if not status["tickere"]:
            raise
        marked = hent_coingecko_reserve(set(status["tickere"]))
        if not status.get("feil_varslet"):
            send_ntfy("Kryptovarsel: bruker reservekilde",
                      "Revolut svarer ikke fra serveren. Bruker CoinGecko i mellomtiden.",
                      prioritet="low", tags="warning")
            status["feil_varslet"] = True

    nye = []
    for s, m in marked.items():
        if m["ticker"] in STABLECOINS or m["volum"] < MIN_VOLUM_NOK:
            continue
        d1, h1 = m.get("d1"), m.get("h1")
        treff_24 = d1 is not None and d1 >= TERSKEL_24T
        treff_1 = h1 is not None and h1 >= TERSKEL_1T
        if not (treff_24 or treff_1):
            continue
        maal = max(d1 or 0, h1 or 0)
        forrige = status["sist_varslet"].get(s)
        if forrige:
            innenfor_pause = na - forrige["tid"] < PAUSE_TIMER * 3600
            if innenfor_pause and maal < forrige["verdi"] + EKSTRA_PP:
                continue
        nye.append((s, m, treff_24, treff_1, maal))

    nye.sort(key=lambda x: -x[4])
    DOCS.joinpath("mynter").mkdir(parents=True, exist_ok=True)

    for s, m, t24, t1, maal in nye:
        grunn = []
        if t24:
            grunn.append(f"+{m['d1']:.1f} % på 24 t")
        if t1:
            grunn.append(f"+{m['h1']:.1f} % på 1 t")
        tekst = (f"{' og '.join(grunn)}\n"
                 f"Pris: {fmt_pris(m['pris'])}\n"
                 f"1 t: {m.get('h1') or 0:+.1f} % · 7 d: {m.get('w1') or 0:+.1f} %\n"
                 f"Volum 24 t: {fmt_nok(m['volum'])} · Markedsverdi: {fmt_nok(m['mcap'])}")
        klikk = f"{PANEL_URL}/?mynt={urllib.parse.quote(m['ticker'])}" if PANEL_URL else None
        try:
            send_ntfy(f"🚀 {m['ticker']} {grunn[0]}", tekst, klikk=klikk)
        except Exception as e:  # noqa: BLE001
            print("ntfy feilet:", e)
            continue
        status["sist_varslet"][s] = {"tid": na, "verdi": maal}
        status["historikk"].insert(0, {
            "tid": na, "symbol": s, "ticker": m["ticker"], "pris": m["pris"],
            "h1": m.get("h1"), "d1": m.get("d1"), "w1": m.get("w1"),
            "volum": m["volum"], "mcap": m["mcap"], "kilde": kilde})
        if kilde == "revolut":
            (DOCS / "mynter" / f"{m['ticker']}.json").write_text(
                json.dumps(hent_detaljer(s), ensure_ascii=False))

    status["historikk"] = status["historikk"][:200]
    # rydd gamle pauser (> 7 dager)
    status["sist_varslet"] = {k: v for k, v in status["sist_varslet"].items()
                              if na - v["tid"] < 7 * 86400}

    STATUS_FIL.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FIL.write_text(json.dumps(status, ensure_ascii=False, indent=1))

    # Data til analysepanelet
    oversikt = sorted(marked.values(), key=lambda x: -(x.get("d1") or -999))
    (DOCS / "data.json").write_text(json.dumps({
        "oppdatert": na, "kilde": kilde,
        "innstillinger": {"terskel_24t": TERSKEL_24T, "terskel_1t": TERSKEL_1T,
                          "min_volum_nok": MIN_VOLUM_NOK, "pause_timer": PAUSE_TIMER},
        "antall": len(marked),
        "varsler": status["historikk"][:100],
        "marked": oversikt,
    }, ensure_ascii=False))
    print(f"OK – {len(marked)} mynter fra {kilde}, {len(nye)} nye varsler.")


if __name__ == "__main__":
    if "--test" in sys.argv:
        send_ntfy("✅ Testvarsel", "Kryptovarselet ditt er koblet til mobilen.",
                  prioritet="default", tags="white_check_mark")
    else:
        main()

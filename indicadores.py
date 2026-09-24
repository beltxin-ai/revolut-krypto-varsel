"""Indicadores técnicos como series completas (listas), sin dependencias externas.

Todas las funciones devuelven una lista del mismo largo que la entrada;
las posiciones sin datos suficientes valen None.
"""


def ema(v, n):
    out = [None] * len(v)
    if len(v) < n:
        return out
    k = 2 / (n + 1)
    e = sum(v[:n]) / n
    out[n - 1] = e
    for i in range(n, len(v)):
        e = v[i] * k + e * (1 - k)
        out[i] = e
    return out


def sma(v, n):
    out = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi(v, n=14):
    """RSI de Wilder."""
    out = [None] * len(v)
    if len(v) <= n:
        return out
    g = p = 0.0
    for i in range(1, n + 1):
        d = v[i] - v[i - 1]
        g += max(d, 0)
        p += max(-d, 0)
    g, p = g / n, p / n
    out[n] = 100.0 if p == 0 else 100 - 100 / (1 + g / p)
    for i in range(n + 1, len(v)):
        d = v[i] - v[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        p = (p * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if p == 0 else 100 - 100 / (1 + g / p)
    return out


def macd(v, rapida=12, lenta=26, senal=9):
    """Devuelve (linea, senal, histograma)."""
    ef, el = ema(v, rapida), ema(v, lenta)
    linea = [a - b if (a is not None and b is not None) else None for a, b in zip(ef, el)]
    validos = [x for x in linea if x is not None]
    s_val = ema(validos, senal)
    sen = [None] * (len(linea) - len(validos)) + s_val
    hist = [a - b if (a is not None and b is not None) else None for a, b in zip(linea, sen)]
    return linea, sen, hist


def true_range(h, l, c):
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    return tr


def atr(h, l, c, n=14):
    """ATR de Wilder."""
    tr = true_range(h, l, c)
    out = [None] * len(c)
    if len(c) < n:
        return out
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def adx(h, l, c, n=14):
    """ADX de Wilder (fuerza de la tendencia, 0-100)."""
    L = len(c)
    out = [None] * L
    if L < 2 * n + 1:
        return out
    tr = true_range(h, l, c)
    pdm = [0.0] + [max(h[i] - h[i - 1], 0) if (h[i] - h[i - 1]) > (l[i - 1] - l[i]) else 0.0 for i in range(1, L)]
    mdm = [0.0] + [max(l[i - 1] - l[i], 0) if (l[i - 1] - l[i]) > (h[i] - h[i - 1]) else 0.0 for i in range(1, L)]
    atr_s = sum(tr[1:n + 1])
    p_s = sum(pdm[1:n + 1])
    m_s = sum(mdm[1:n + 1])
    dx = []
    for i in range(n + 1, L):
        atr_s = atr_s - atr_s / n + tr[i]
        p_s = p_s - p_s / n + pdm[i]
        m_s = m_s - m_s / n + mdm[i]
        pdi = 100 * p_s / atr_s if atr_s else 0
        mdi = 100 * m_s / atr_s if atr_s else 0
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0)
        if len(dx) == n:
            a = sum(dx) / n
            out[i] = a
        elif len(dx) > n:
            a = (out[i - 1] * (n - 1) + dx[-1]) / n
            out[i] = a
    return out


def obv(c, v):
    out = [0.0]
    for i in range(1, len(c)):
        if c[i] > c[i - 1]:
            out.append(out[-1] + v[i])
        elif c[i] < c[i - 1]:
            out.append(out[-1] - v[i])
        else:
            out.append(out[-1])
    return out


def maximo_previo(h, n):
    """Máximo de las n velas ANTERIORES (canal de Donchian, sin incluir la actual)."""
    out = [None] * len(h)
    for i in range(n, len(h)):
        out[i] = max(h[i - n:i])
    return out


def minimo_previo(l, n):
    out = [None] * len(l)
    for i in range(n, len(l)):
        out[i] = min(l[i - n:i])
    return out

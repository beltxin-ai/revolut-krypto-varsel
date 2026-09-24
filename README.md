# Radar Revolut X

Sistema de trading para **Revolut X** validado fuera de muestra, con cartera modelo, panel y notificaciones al móvil (ntfy). Revisión cada 15 minutos.

## Sistema (`sistema.py`)

- **Núcleo (50 %)**: BTC y ETH, 25 % cada una, cuando el cierre diario está sobre la SMA50 y la SMA50 sobre la SMA200.
- **Satélite (50 %)**: hasta 5 altcoins líquidas de Revolut X (10 % cada una). Entrada con cierre sobre el máximo de 55 días, solo si BTC está sobre su SMA200. Stop 2 ATR con trailing de 3 ATR; salida con cierre bajo el mínimo de 20 días.
- Decisiones con la vela diaria cerrada (00:00 UTC); stops vigilados cada 15 min con el bid de Revolut X.

## Evidencia (`investigacion.py`)

Workflow «Investigación de estrategias»: 12 variantes + 3 referencias sobre las monedas líquidas de Revolut X, ~5 años de velas diarias, costes reales (0,09 % + medio spread + 0,05 %), parámetros elegidos en entrenamiento y medidos fuera de muestra. Resultados en la rama `investigacion`.

Limitaciones: sesgo de supervivencia, un solo mercado bajista grande en la muestra, spread actual aplicado a todo el periodo, caídas máximas de −26 % (núcleo) y −46 % (satélite) fuera de muestra.

## Fuentes

| Fuente | Uso |
|---|---|
| Revolut X API pública (EEA) | Universo, precio bid/ask, spread y volumen |
| Binance (data-api.binance.vision) | Velas diarias (validadas contra Revolut X, < 3 %) |

## Notificaciones

- 🟢 **COMPRAR**: precio, tamaño y stop.
- 🔴 **VENDER / STOP**: motivo y resultado.

## Ajustes

Variables de Actions: `MIN_VOL_USD` (volumen mínimo 24 h en Revolut X, por defecto 100 000) y `MAX_SPREAD` (0,006). Secret `NTFY_TOPIC`.

> Herramienta de análisis, no asesoramiento financiero.

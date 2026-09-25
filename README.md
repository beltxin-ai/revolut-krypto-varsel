# Radar Revolut X

Sistema de trading para **Revolut X** validado fuera de muestra, con cartera modelo, panel y notificaciones al móvil (ntfy). Revisión cada 15 minutos.

## Sistema (`sistema.py`)

- **Núcleo (50 %)**: BTC y ETH, 25 % cada una, cuando el cierre diario está sobre la SMA50 y la SMA50 sobre la SMA200.
- **Satélite (50 %)**: hasta 5 altcoins líquidas de Revolut X (10 % cada una). Entrada con cierre sobre el máximo de 55 días, solo si BTC está sobre su SMA200. Stop 2 ATR con trailing de 3 ATR; salida con cierre bajo el mínimo de 20 días.
- Decisiones con la vela diaria cerrada (00:00 UTC); stops vigilados cada 15 min con el bid de Revolut X.

## Evidencia (`investigacion.py`)

Workflow «Investigación de estrategias»: 12 variantes + 3 referencias sobre las monedas líquidas de Revolut X, ~5 años de velas diarias, costes reales (0,09 % + medio spread + 0,05 %), parámetros elegidos en entrenamiento y medidos fuera de muestra. Resultados en la rama `investigacion`.

Limitaciones: sesgo de supervivencia, un solo mercado bajista grande en la muestra, spread actual aplicado a todo el periodo, caídas máximas de −26 % (núcleo) y −46 % (satélite) fuera de muestra.

## Fiabilidad operativa

- **Vigilante** (`vigilante.yml`, cada 30 min): si el radar lleva más de 45 min sin publicar, lo relanza y avisa «⛔ Radar parado».
- **Fuente de reserva**: si Binance falla, las velas se piden a OKX.
- **Parte diario** al móvil (~07:00 hora de Noruega): revisiones hechas, fuentes, régimen, cartera y señales.
- **Pruebas** (`pruebas.yml`) en cada cambio de `main`.

## Fases 2 y 3 (`investigacion2.py`)

Nueve variantes sobre la cartera completa. Solo se adopta lo que mejora Sharpe y Calmar en entrenamiento **y** fuera de muestra:
adoptado el **control de volatilidad** (entradas × min(1, 50 % / vol. BTC 30 d)). Descartados: entrada intradía, cortacircuitos,
momentum como tercer bloque y el tamaño por riesgo (este último queda como sugerencia opcional). Resultados en la rama `investigacion2`.

## Fuentes

| Fuente | Uso |
|---|---|
| Revolut X API pública (EEA) | Universo, precio bid/ask, spread y volumen |
| Binance (data-api.binance.vision) | Velas diarias y horarias (validadas contra Revolut X, < 3 %) |
| OKX | Reserva si Binance falla |

## Notificaciones

- 🟢 **COMPRAR**: precio, tamaño y stop.
- 🔴 **VENDER / STOP**: motivo y resultado.

## Ajustes

Variables de Actions: `MIN_VOL_USD` (volumen mínimo 24 h en Revolut X, por defecto 100 000) y `MAX_SPREAD` (0,006). Secret `NTFY_TOPIC`.

> Herramienta de análisis, no asesoramiento financiero.

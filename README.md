# Radar Cripto · Revolut

Análisis técnico de todas las criptos de Revolut cada 15 minutos, con señal **COMPRAR / MANTENER / ESPERAR / NEUTRAL / VENDER**, niveles (entrada, stop, objetivos, tamaño) y notificaciones al móvil (ntfy).

## Fuentes

| Fuente | Uso |
|---|---|
| Revolut | Universo de monedas y precio en NOK (el precio al que operas) |
| Binance (data-api.binance.vision) | Velas de 4 h y diarias con volumen |
| OKX | Respaldo de velas |
| Hyperliquid | Funding e interés abierto de perpetuos |
| alternative.me | Índice Fear & Greed |
| CoinGecko | Dominancia de BTC |

Las velas de un exchange solo se usan si su precio en NOK difiere menos de un 4 % del de Revolut.

## Estrategia (`estrategia.py`)

Swing corto (horas a pocos días) con velas de 4 h cerradas y filtro diario. Siete pilares de −2 a +2: tendencia diaria, tendencia 4 h, momento, volumen y flujo, fuerza relativa frente a BTC, régimen de mercado y derivados. Nota ponderada de −100 a +100.

- **COMPRAR**: nota ≥ 45 (≥ 60 si el mercado es bajista), tendencia 4 h alcista, diaria no bajista, precio a menos de 3 ATR de la EMA20, liquidez suficiente, confirmada en 2 revisiones seguidas.
- **VENDER**: nota ≤ −35, o tendencia y momento 4 h bajistas, o stop tocado.
- **Stop** 2 ATR (o bajo el mínimo reciente), trailing de 3 ATR. **Tamaño**: riesgo máximo 1 % del capital.

Cada COMPRAR confirmado abre una posición virtual para medir resultados reales. Un backtest diario en velas de 4 h (con comisiones de Revolut X) muestra si la estrategia tiene ventaja.

## Notificaciones

- 🟢 **COMPRAR** confirmado, con entrada, stop, objetivos y tamaño.
- 🔴 **VENDER** o stop de una posición abierta.

## Ajustes

En **Settings → Secrets and variables → Actions → Variables**: `MIN_VOLUM_NOK` (volumen mínimo en 24 h, por defecto 5 000 000). El secret `NTFY_TOPIC` es el tema de ntfy del móvil.

> Herramienta de análisis, no asesoramiento financiero. Reglas no garantizadas; el backtest no incluye régimen ni derivados.

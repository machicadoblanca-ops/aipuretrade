# IA para MetaTrader (MT4/MT5)
## Especificación ordenada: SMC + Order Blocks + velas (15m, 4h, 1d)

Este documento define, de forma práctica, **qué datos enviar a la IA** y **qué respuesta debe devolver** para proponer órdenes con lógica SMC.

---

## Implementación real en Python (OpenAI)

Se incluye `signal_engine.py` con este flujo real:

1. Analiza mercado con IA.
2. Guarda análisis y setups en **SQLite**.
3. Evalúa activación de setups.
4. Ejecuta **solo MARKET orders** (nunca limit/stop pendientes).

### Ejecución una vez

```bash
cp .env.example .env
# edita .env con tu api key y (opcional) ruta de DB
set -a; source .env; set +a
python signal_engine.py --input example_payload.json --db signals.db --output signal.json --model gpt-5-mini --once
```

En Windows CMD:

```bat
copy .env.example .env
REM edita .env y agrega OPENAI_API_KEY=tu_api_key
py signal_engine.py --input example_payload.json --db signals.db --output signal.json --model gpt-5-mini --once
```

<<<<<<< codex/locate-api-key-storage-jbb9o8
Si defines todo en `.env`, puedes correr solo:

```bat
py signal_engine.py
```

=======
>>>>>>> main
Para ver trazas detalladas de todo el flujo (debug):

```bat
py signal_engine.py --input example_payload.json --db signals.db --output signal.json --model gpt-5-mini --once --debug
```

<<<<<<< codex/locate-api-key-storage-jbb9o8
Con `--debug` verás detalles de: carga de `.env`, parámetros de arranque, normalización de setups IA, decisiones de activación, guardado en DB y (si aplica) envío a MT5.

### Modo portable (mover carpeta sin romper rutas)

El script ahora resuelve rutas relativas respecto al directorio donde está `signal_engine.py` (no respecto al directorio actual de la consola):

- `--input example_payload.json`
- `--db signals.db`
- `--output signal.json`
- archivo `.env`

Así puedes mover toda la carpeta del proyecto a otro disco/ruta y seguirá encontrando archivos relativos correctamente.

=======
>>>>>>> main
### Ejecución continua (análisis 15m + revisión 1m)

```bash
set -a; source .env; set +a
python signal_engine.py --input example_payload.json --db signals.db --analysis-every-minutes 15 --review-every-minutes 1 --model gpt-5-mini
```

### Variables de entorno (`.env`)

`signal_engine.py` intenta cargar automáticamente un archivo `.env` (si está instalado `python-dotenv`).

Variables soportadas:

- `OPENAI_API_KEY` (requerida)
- `OPENAI_MODEL` (opcional, default `gpt-5-mini`)
- `INPUT_JSON_PATH` (opcional, reemplaza `--input`)
- `OUTPUT_JSON_PATH` (opcional, reemplaza `--output`)
- `ANALYSIS_EVERY_MINUTES` (opcional, reemplaza `--analysis-every-minutes`)
- `REVIEW_EVERY_MINUTES` (opcional, reemplaza `--review-every-minutes`)
- `RUN_ONCE` (opcional, `true/false`, reemplaza `--once`)
- `DEBUG` (opcional, `true/false`, reemplaza `--debug`)
- `EXECUTE_REAL_MT5` (opcional, `true/false`, reemplaza `--execute-real-mt5`)
- `SIGNALS_DB_PATH` (opcional, default `signals.db`)

Para ejecución real en MT5 (opcional):

- `MT5_LOGIN`
- `MT5_PASSWORD`
- `MT5_SERVER`
- `MT5_PATH` (opcional)
- `MT5_VOLUME` (opcional, default `0.01`)
- `MT5_MAGIC` (opcional)
- `MT5_DEVIATION` (opcional)

Significado de estos parámetros de ejecución MT5:

- `MT5_VOLUME`: tamaño de lote por orden (ej. `0.01` = micro lote en muchos brokers). A mayor volumen, mayor exposición/riesgo por trade.
- `MT5_MAGIC`: identificador numérico de la estrategia/EA en MT5. Sirve para distinguir tus órdenes de otras (manuales u otros robots).
- `MT5_DEVIATION`: desviación máxima permitida en puntos para ejecutar una orden market (tolerancia al slippage). Si el precio se mueve más que este valor, MT5 puede rechazar la ejecución.

> Todas estas variables se leen en Python con `os.getenv(...)` al iniciar el script.

### Ejecución real en MT5

> Si usas `--execute-real-mt5`, el script **exige login/password/server** y hace login en MT5 antes de enviar MARKET orders.
> Para este modo necesitas instalar `MetaTrader5` y tener la terminal abierta/sesión permitida para trading.

```bash
pip install MetaTrader5
```

```bash
set -a; source .env; set +a
python signal_engine.py \
  --input example_payload.json \
  --once \
  --execute-real-mt5 \
  --mt5-server "$MT5_SERVER" \
  --mt5-path "$MT5_PATH"
```

El envío de orden real se hace como `TRADE_ACTION_DEAL` con `ORDER_TYPE_BUY`/`ORDER_TYPE_SELL` (market), no pending orders.

### Error común: "Faltan credenciales de MT5"

Si ejecutas con `--execute-real-mt5`, debes pasar credenciales por `.env` o por CLI.

Ejemplo en Windows CMD:

```bat
set MT5_LOGIN=12345678
set MT5_PASSWORD=tu_password
set MT5_SERVER=Nombre-Del-Server
py signal_engine.py --input example_payload.json --once --execute-real-mt5
```

Ejemplo por CLI directo:

```bat
py signal_engine.py --input example_payload.json --once --execute-real-mt5 --mt5-login 12345678 --mt5-password tu_password --mt5-server Nombre-Del-Server
```

<<<<<<< codex/locate-api-key-storage-jbb9o8
### Error común: `Responses.create() got an unexpected keyword argument 'response_format'`

Ese error viene de una diferencia de versiones del SDK de OpenAI. El script ya está preparado para funcionar sin `response_format`, parsear JSON de forma compatible y, si `responses` no está disponible, usar fallback a `chat.completions`.

Recomendado:

```bat
py -m pip install -U openai
```

=======
>>>>>>> main
### Persistencia en SQLite

Tablas principales:

- `analyses`: análisis de cada ciclo (payload + resumen + sesgos).
- `order_setups`: propuestas de orden, condición de activación, estado (`PENDING`/`EXECUTED`).
- `executions`: historial de ejecuciones MARKET disparadas.

Campos de activación en `order_setups`:

- `activation_condition` (texto: cierre con cuerpo de vela, sweep, etc.)
- `activation_type` (`candle_close`, `wick_rejection`, `break_retest`, `immediate`, ...)
- `trigger_timeframe` (`m15`, `h4`, `d1`)

### Consultas rápidas

```bash
sqlite3 signals.db "SELECT setup_id, action, status, activation_type, activation_condition FROM order_setups ORDER BY created_at_utc DESC LIMIT 20;"
sqlite3 signals.db "SELECT execution_id, setup_id, side, execution_price, executed_at_utc FROM executions ORDER BY execution_id DESC LIMIT 20;"
```

---

## Índice
1. [Implementación real en Python (OpenAI)](#implementación-real-en-python-openai)
2. [Objetivo](#objetivo)
3. [Flujo resumido](#flujo-resumido)
4. [INPUT: datos que MetaTrader envía a la IA](#input-datos-que-metatrader-envía-a-la-ia)
5. [OUTPUT: datos que la IA devuelve](#output-datos-que-la-ia-devuelve)
6. [Cómo elegir el mejor Order Block](#cómo-elegir-el-mejor-order-block)
7. [Afinación del precio de entrada (`000`, `00`, mitad de vela)](#afinación-del-precio-de-entrada-000-00-mitad-de-vela)
8. [Reglas de decisión multi-timeframe](#reglas-de-decisión-multi-timeframe)
9. [Integración en MT4/MT5](#integración-en-mt4mt5)
10. [Sugerencias prácticas (mi recomendación)](#sugerencias-prácticas-mi-recomendación)
11. [Pseudocódigo MQL5](#pseudocódigo-mql5)

---

## Objetivo

- Trabajar con contexto de **3 temporalidades**: `m15`, `h4`, `d1`.
- Usar **Smart Money Concepts (SMC)**: BOS/CHoCH, liquidez, FVG, Order Blocks.
- Proponer órdenes con precio afinado por:
  - niveles psicológicos `000` y `00`
  - mitad de la vela extrema (máximo/mínimo reciente).

---

## Flujo resumido

1. MetaTrader arma payload con velas + SMC + riesgo.
2. IA evalúa sesgo `d1/h4`, timing `m15` y ranking de OB.
3. IA devuelve orden (`BUY_LIMIT`, `SELL_LIMIT`, etc.) o `NO_TRADE`.
4. EA/indicador valida riesgo y ejecuta/dibuja señal.

---

## INPUT: datos que MetaTrader envía a la IA

### Campos clave

- Contexto general: `symbol`, `timestamp_utc`, `spread_points`, `session`, `news_risk`
- Riesgo de cuenta: `balance`, `equity`, `risk_per_trade_pct`, `max_open_trades`
- Refinamiento: `round_levels`, `use_extreme_candle_midpoint`, `tick_size`
- Por timeframe (`m15`, `h4`, `d1`):
  - `candles` (OHLC + timestamp + `candle_type`)
  - `extreme_candle` (si aplica)
  - `indicators` (SMA/EMA/RSI/ATR/ADX)
  - `smc` (`structure`, `liquidity`, `fvg`, `orderblocks`, etc.)

### Ejemplo de INPUT

```json
{
  "symbol": "EURUSD",
  "timestamp_utc": "2026-03-12T12:45:00Z",
  "spread_points": 12,
  "session": "LONDON",
  "news_risk": "LOW",
  "account": {
    "balance": 10000,
    "equity": 9950,
    "risk_per_trade_pct": 1.0,
    "max_open_trades": 2
  },
  "execution_refinement": {
    "round_levels": ["000", "00"],
    "use_extreme_candle_midpoint": true,
    "tick_size": 0.00001
  },
  "timeframes": {
    "m15": {
      "candles": [
        {"t":"2026-03-12T12:15:00Z","o":1.0820,"h":1.0826,"l":1.0818,"c":1.0823,"candle_type":"bullish"},
        {"t":"2026-03-12T12:30:00Z","o":1.0823,"h":1.0829,"l":1.0820,"c":1.0827,"candle_type":"bullish_engulfing"}
      ],
      "extreme_candle": {"kind":"max","high":1.0829,"low":1.0820,"midpoint":1.08245},
      "indicators": {"sma20":1.0820,"sma50":1.0816,"ema20":1.0821,"ema50":1.0817,"rsi14":58,"atr14":0.0009},
      "smc": {
        "structure": {"bos":"BULLISH","choch":"NONE"},
        "liquidity": {"equal_highs":1.0830,"equal_lows":1.0812,"swept":"sell_side"},
        "fvg": [{"type":"bullish","high":1.0821,"low":1.0819,"status":"open"}],
        "orderblocks": [
          {"id":"m15_ob_1","type":"bullish","high":1.0824,"low":1.0819,"mitigated":false,"displacement_score":82,"freshness":0.9},
          {"id":"m15_ob_2","type":"bullish","high":1.0818,"low":1.0813,"mitigated":true,"displacement_score":55,"freshness":0.3}
        ]
      }
    },
    "h4": {
      "candles": [{"t":"2026-03-12T08:00:00Z","o":1.0760,"h":1.0835,"l":1.0748,"c":1.0822,"candle_type":"bullish"}],
      "indicators": {"sma50":1.0787,"sma200":1.0720,"ema50":1.0790,"ema200":1.0725,"rsi14":61},
      "smc": {
        "structure": {"bos":"BULLISH","choch":"NONE"},
        "orderblocks": [{"id":"h4_ob_1","type":"bullish","high":1.0805,"low":1.0788,"mitigated":false,"displacement_score":88,"freshness":0.95}],
        "premium_discount":"DISCOUNT"
      }
    },
    "d1": {
      "candles": [{"t":"2026-03-11T00:00:00Z","o":1.0700,"h":1.0850,"l":1.0680,"c":1.0820,"candle_type":"bullish_pinbar"}],
      "indicators": {"sma50":1.0726,"sma200":1.0602,"ema50":1.0730,"ema200":1.0610,"adx14":24},
      "smc": {
        "structure": {"bos":"BULLISH","choch":"NONE"},
        "orderblocks": [{"id":"d1_ob_1","type":"bullish","high":1.0755,"low":1.0718,"mitigated":false,"displacement_score":91,"freshness":0.97}],
        "dealing_range":{"high":1.0850,"low":1.0680}
      }
    }
  }
}
```

### Tipos de vela recomendados

- `bullish`, `bearish`
- `doji`, `pinbar`, `inside_bar`
- `bullish_engulfing`, `bearish_engulfing`

---

## OUTPUT: datos que la IA devuelve

La IA debe devolver:
- una orden accionable (`BUY_LIMIT`, `SELL_STOP`, etc.) o `NO_TRADE`
- el **Order Block seleccionado**
- el detalle de **refinamiento de precio**

### Contrato recomendado

- `action`, `entry`, `sl`, `tp1`, `tp2`
- `confidence`, `rr`, `expiry_minutes`
- `selected_orderblock`
- `price_refinement`
- `smc_context`
- `timeframe_bias`
- `reason`

### Ejemplo de OUTPUT

```json
{
  "symbol": "EURUSD",
  "action": "BUY_LIMIT",
  "entry": 1.08250,
  "sl": 1.08090,
  "tp1": 1.08420,
  "tp2": 1.08610,
  "confidence": 81,
  "rr": 1.9,
  "expiry_minutes": 180,
  "selected_orderblock": {
    "id": "m15_ob_1",
    "type": "bullish",
    "high": 1.0824,
    "low": 1.0819,
    "score": 89
  },
  "price_refinement": {
    "base_entry": 1.08245,
    "nearest_round_level": 1.08250,
    "round_level_type": "00",
    "extreme_candle_midpoint": 1.08245,
    "final_entry": 1.08250
  },
  "smc_context": {
    "d1": "BOS alcista",
    "h4": "descuento + OB fresco",
    "m15": "sweep de liquidez + FVG bullish"
  },
  "timeframe_bias": {"m15": "alcista", "h4": "alcista", "d1": "alcista"},
  "reason": "Mitigación en mejor OB m15 alineado con BOS alcista en h4/d1 y afinado a nivel 00"
}
```

---

## Cómo elegir el mejor Order Block

### Score base

`score_ob = 0.30*freshness + 0.25*displacement + 0.20*alignment_htf + 0.15*liquidity_sweep + 0.10*fvg_confluence`

### Reglas

1. Priorizar OB no mitigado y reciente.
2. Alinear con sesgo de `d1` y `h4`.
3. Favorecer sweep de liquidez previo.
4. Favorecer confluencia con FVG.
5. Invalidar si spread/noticia es desfavorable o si `rr < 1.5`.

---

## Afinación del precio de entrada (`000`, `00`, mitad de vela)

1. Tomar `base_ob` (borde o 50% del OB).
2. Calcular `midpoint_extreme = (high + low) / 2` de la vela extrema (máx/mín).
3. Buscar `nearest_round_level` terminado en `000` o `00`.
4. Combinar por confluencia y redondear a `tick_size`.

### Fórmula sugerida

`entry_refined = 0.50*base_ob + 0.30*midpoint_extreme + 0.20*nearest_round_level`

---

## Reglas de decisión multi-timeframe

1. **D1 (régimen):** BOS/CHoCH + SMA50/200.
2. **H4 (dirección):** largos/cortos + premium/discount.
3. **M15 (timing):** vela + sweep + retorno a OB/FVG.
4. **Refinamiento:** aplicar `000/00` + midpoint de vela extrema.
5. **Riesgo:** bloquear si `confidence < 70` o `rr < 1.5`.

---

## Integración en MT4/MT5

- **Indicador:** dibuja OB elegido, FVG, nivel refinado y flecha.
- **EA:** ejecuta orden solo con validaciones finales.

Canales de conexión:
- `WebRequest` (API externa)
- `FileRead` (archivo local)
- Python/DLL (modelo local)

---

## Sugerencias prácticas (mi recomendación)

1. **Empieza en modo "solo señales" 2-4 semanas** antes de autoejecución.
2. **Guarda cada decisión** (`payload`, `response`, resultado) para reentrenar la IA.
3. **Añade `kill_switch` diario** (ej. -2R o 3 pérdidas seguidas) para cortar riesgo.
4. **Evita sobreoperar**: máximo 1-2 setups por sesión por símbolo.
5. **Valida calidad del OB** con umbral mínimo (ej. `score_ob >= 70`).
6. **Mide por R múltiplos**, no solo winrate (esperanza matemática real).
7. **Haz walk-forward mensual**: recalibrar pesos del score y del `entry_refined`.

### Métricas mínimas a monitorear

- Winrate
- Profit factor
- Expectancy por trade
- Máximo drawdown
- % operaciones filtradas por `NO_TRADE`
- Desviación entre `entry_refined` y ejecución real (slippage)

---

## Pseudocódigo MQL5

```text
OnNewBar(M15):
  payload = buildInputWithCandlesAndSMC(symbol, M15, H4, D1)
  signal  = callAI(payload)

  if signal.action != NO_TRADE
     and signal.confidence >= 70
     and signal.rr >= 1.5
     and signal.selected_orderblock.id != ""
     and signal.price_refinement.final_entry > 0
     and withinRiskLimits():
        drawSMC(signal)
        // en EA: placeOrder(signal)
```

> La IA mejora el contexto y la disciplina, pero no garantiza ganancias.

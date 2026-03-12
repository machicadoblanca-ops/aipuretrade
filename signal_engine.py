#!/usr/bin/env python3
"""Motor IA SMC para MetaTrader con OpenAI + SQLite + ejecución MARKET-only.

Flujos:
1) Una corrida:
   python signal_engine.py --input example_payload.json --db signals.db --output signal.json

2) Modo producción:
   - análisis IA cada 15 minutos
   - revisión/ejecución de setups cada 1 minuto
   python signal_engine.py --input example_payload.json --db signals.db --analysis-every-minutes 15 --review-every-minutes 1
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


DEBUG = False


def _debug(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG {_utc_now()}] {msg}")


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _resolve_path(path_value: str) -> str:
    """Resuelve rutas relativas contra el directorio del script (modo portable)."""
    path = Path(path_value)
    if path.is_absolute():
        return str(path)
    return str((_script_dir() / path).resolve())


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}

SYSTEM_PROMPT = """
Eres un motor de trading cuantitativo para MetaTrader (MT4/MT5).
Analiza payload multi-timeframe (m15, h4, d1) con SMC, tipos de velas,
order blocks y refinamiento de entrada.

Devuelve SOLO JSON válido con estructura:
{
  "symbol": str,
  "analysis_id": str,
  "timeframe_bias": {"m15": str, "h4": str, "d1": str},
  "market_summary": str,
  "order_setups": [
    {
      "setup_id": str,
      "priority": int,
      "action": "BUY_LIMIT"|"BUY_STOP"|"SELL_LIMIT"|"SELL_STOP"|"MARKET_BUY"|"MARKET_SELL"|"NO_TRADE",
      "entry": number,
      "sl": number,
      "tp1": number,
      "tp2": number,
      "confidence": number,
      "rr": number,
      "expiry_minutes": number,
      "selected_orderblock": {"id": str, "type": str, "high": number, "low": number, "score": number},
      "price_refinement": {
        "base_entry": number,
        "nearest_round_level": number,
        "round_level_type": "000"|"00",
        "extreme_candle_midpoint": number,
        "final_entry": number
      },
      "activation_condition": str,
      "activation_type": "candle_close"|"wick_rejection"|"break_retest"|"liquidity_sweep"|"immediate"|"other",
      "trigger_timeframe": "m15"|"h4"|"d1",
      "reason": str
    }
  ]
}

Reglas:
- Entrega entre 1 y 3 setups ordenados por prioridad.
- Si no hay setup, usa un setup con action=NO_TRADE.
- Para setups activos: confidence >= 70 y rr >= 1.5.
- Incluye activation_condition textual (ej: cierre con cuerpo de vela encima/debajo de nivel).
- No agregues texto fuera del JSON.
""".strip()

ALLOWED_ACTIONS = {
    "BUY_LIMIT",
    "BUY_STOP",
    "SELL_LIMIT",
    "SELL_STOP",
    "MARKET_BUY",
    "MARKET_SELL",
    "NO_TRADE",
}
ALLOWED_ACTIVATION_TYPES = {"candle_close", "wick_rejection", "break_retest", "liquidity_sweep", "immediate", "other"}


def _load_dotenv_if_available() -> None:
    """Carga .env (con python-dotenv si existe, o parser simple de fallback)."""
    # Compatibilidad defensiva: si una copia vieja no tiene _script_dir, no debe crashear.
    try:
        script_base = _script_dir()
    except Exception:
        script_base = Path(__file__).resolve().parent
    dotenv_script = script_base / ".env"
    dotenv_cwd = Path(".env").resolve()
    dotenv_target = dotenv_script if dotenv_script.exists() else dotenv_cwd

    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        _debug("python-dotenv no disponible; intentando parser interno de .env")
        if not dotenv_target.exists():
            _debug("No existe archivo .env; continúo con variables de entorno del sistema")
            return
        with open(dotenv_target, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        _debug(f".env cargado con parser interno desde {dotenv_target}")
        return
    load_dotenv(dotenv_path=dotenv_target)
    _debug(f".env cargado con python-dotenv desde {dotenv_target}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _latest_close(payload: Dict[str, Any], timeframe: str) -> float:
    candles = (((payload.get("timeframes") or {}).get(timeframe) or {}).get("candles") or [])
    if not candles:
        return 0.0
    last = candles[-1]
    return _safe_float(last.get("c"), 0.0)


def init_db(db_path: str) -> None:
    _debug(f"Inicializando/validando esquema SQLite en db={db_path}")
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                analysis_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                model TEXT NOT NULL,
                market_summary TEXT,
                timeframe_bias_json TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS order_setups (
                setup_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                priority INTEGER NOT NULL,
                action TEXT NOT NULL,
                entry REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                confidence REAL,
                rr REAL,
                expiry_minutes INTEGER,
                selected_orderblock_json TEXT NOT NULL,
                price_refinement_json TEXT NOT NULL,
                activation_condition TEXT NOT NULL,
                activation_type TEXT NOT NULL,
                trigger_timeframe TEXT NOT NULL,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                activation_price REAL,
                activated_at_utc TEXT,
                executed_at_utc TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at_utc TEXT NOT NULL,
                FOREIGN KEY (analysis_id) REFERENCES analyses(analysis_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS executions (
                execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_id TEXT NOT NULL,
                analysis_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                execution_price REAL,
                execution_note TEXT,
                executed_at_utc TEXT NOT NULL,
                FOREIGN KEY (setup_id) REFERENCES order_setups(setup_id)
            )
            """
        )
        conn.commit()
        _debug("Esquema SQLite listo")
    finally:
        conn.close()


def _to_market_only(action: str) -> str:
    if action in {"BUY_LIMIT", "BUY_STOP", "MARKET_BUY"}:
        return "MARKET_BUY"
    if action in {"SELL_LIMIT", "SELL_STOP", "MARKET_SELL"}:
        return "MARKET_SELL"
    return "NO_TRADE"


def validate_and_normalize(result: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    _debug(f"Validando y normalizando respuesta IA para symbol={symbol}")
    result.setdefault("symbol", symbol)
    result.setdefault("analysis_id", f"analysis_{int(datetime.now().timestamp())}")
    result.setdefault("market_summary", "")
    result.setdefault("timeframe_bias", {})

    setups = result.get("order_setups") or []
    if not isinstance(setups, list) or not setups:
        _debug("La IA no devolvió order_setups válidos; creando NO_TRADE por defecto")
        setups = [{"setup_id": f"{result['analysis_id']}_1", "priority": 1, "action": "NO_TRADE", "activation_condition": "Sin setup"}]

    normalized: List[Dict[str, Any]] = []
    for idx, setup in enumerate(setups[:3], start=1):
        setup_id = str(setup.get("setup_id") or f"{result['analysis_id']}_{idx}")
        action = str(setup.get("action", "NO_TRADE"))
        if action not in ALLOWED_ACTIONS:
            _debug(f"Setup {setup_id}: action inválida '{action}', se fuerza NO_TRADE")
            action = "NO_TRADE"

        confidence = _safe_float(setup.get("confidence"), 0.0)
        rr = _safe_float(setup.get("rr"), 0.0)
        if action != "NO_TRADE" and (confidence < 70 or rr < 1.5):
            _debug(f"Setup {setup_id}: confidence/rr insuficiente (confidence={confidence}, rr={rr}), se fuerza NO_TRADE")
            action = "NO_TRADE"

        action = _to_market_only(action)  # requisito usuario: solo MARKET

        activation_type = str(setup.get("activation_type", "other"))
        if activation_type not in ALLOWED_ACTIVATION_TYPES:
            _debug(f"Setup {setup_id}: activation_type inválido '{activation_type}', se fuerza other")
            activation_type = "other"

        trigger_tf = str(setup.get("trigger_timeframe", "m15"))
        if trigger_tf not in {"m15", "h4", "d1"}:
            _debug(f"Setup {setup_id}: trigger_timeframe inválido '{trigger_tf}', se fuerza m15")
            trigger_tf = "m15"

        price_refinement = setup.get("price_refinement") or {}
        final_entry = _safe_float(price_refinement.get("final_entry"), 0.0)
        entry = _safe_float(setup.get("entry"), 0.0)
        if entry == 0.0 and final_entry > 0:
            entry = final_entry

        normalized.append(
            {
                "setup_id": setup_id,
                "priority": _safe_int(setup.get("priority"), idx),
                "action": action,
                "entry": entry,
                "sl": _safe_float(setup.get("sl"), 0.0),
                "tp1": _safe_float(setup.get("tp1"), 0.0),
                "tp2": _safe_float(setup.get("tp2"), 0.0),
                "confidence": confidence,
                "rr": rr,
                "expiry_minutes": _safe_int(setup.get("expiry_minutes"), 60),
                "selected_orderblock": setup.get("selected_orderblock") or {},
                "price_refinement": price_refinement,
                "activation_condition": str(setup.get("activation_condition", "Sin condición")),
                "activation_type": activation_type,
                "trigger_timeframe": trigger_tf,
                "reason": str(setup.get("reason", "")),
            }
        )

    normalized.sort(key=lambda x: x["priority"])
    _debug(f"Normalización completada: setups={len(normalized)}")
    result["order_setups"] = normalized
    return result


def generate_signal(payload: Dict[str, Any], model: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Falta OPENAI_API_KEY en variables de entorno. "
            "Configúrala en .env (OPENAI_API_KEY=...) o en Windows CMD: set OPENAI_API_KEY=tu_api_key"
        )

    from openai import OpenAI  # lazy import para permitir tests sin dependencia instalada

    client = OpenAI(api_key=api_key)
    _debug(f"Generando señal con modelo={model}, symbol={payload.get('symbol')}")
    user_prompt = (
        "Analiza y genera setups para este payload. "
        "Responde estrictamente con JSON válido, sin markdown ni texto adicional:\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    output_text = ""
    try:
        _debug("Intentando client.responses.create")
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        output_text = str(getattr(response, "output_text", "") or "").strip()
    except Exception:
        # Compatibilidad con SDK antiguos que no soportan Responses API.
        _debug("Fallo en Responses API; usando fallback client.chat.completions.create")
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
        )
        output_text = str((completion.choices[0].message.content if completion.choices else "") or "").strip()

    if not output_text:
        raise RuntimeError("La API devolvió respuesta vacía.")

    try:
        parsed = json.loads(output_text)
    except json.JSONDecodeError:
        # Compatibilidad SDK/modelos: intenta extraer primer bloque JSON del texto.
        _debug("Respuesta no era JSON puro; intentando extraer bloque JSON")
        start = output_text.find("{")
        end = output_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError("La respuesta de OpenAI no contiene JSON válido.")
        parsed = json.loads(output_text[start : end + 1])

    return validate_and_normalize(parsed, symbol=str(payload.get("symbol", "UNKNOWN")))


def store_analysis(db_path: str, payload: Dict[str, Any], result: Dict[str, Any], model: str) -> None:
    _debug(f"Guardando análisis en SQLite db={db_path}, analysis_id={result.get('analysis_id')}")
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        now_utc = _utc_now()
        analysis_id = result["analysis_id"]
        symbol = result.get("symbol") or payload.get("symbol", "UNKNOWN")

        cur.execute(
            """
            INSERT OR REPLACE INTO analyses (
                analysis_id, symbol, created_at_utc, payload_json, model, market_summary, timeframe_bias_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis_id,
                symbol,
                now_utc,
                json.dumps(payload, ensure_ascii=False),
                model,
                result.get("market_summary", ""),
                json.dumps(result.get("timeframe_bias", {}), ensure_ascii=False),
            ),
        )

        for setup in result["order_setups"]:
            cur.execute(
                """
                INSERT OR REPLACE INTO order_setups (
                    setup_id, analysis_id, priority, action, entry, sl, tp1, tp2,
                    confidence, rr, expiry_minutes, selected_orderblock_json,
                    price_refinement_json, activation_condition, activation_type,
                    trigger_timeframe, reason, status, is_active, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    setup["setup_id"], analysis_id, setup["priority"], setup["action"], setup["entry"], setup["sl"],
                    setup["tp1"], setup["tp2"], setup["confidence"], setup["rr"], setup["expiry_minutes"],
                    json.dumps(setup["selected_orderblock"], ensure_ascii=False),
                    json.dumps(setup["price_refinement"], ensure_ascii=False),
                    setup["activation_condition"], setup["activation_type"], setup["trigger_timeframe"],
                    setup["reason"], "PENDING", 1, now_utc,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _should_activate(action: str, activation_type: str, entry: float, current_close: float) -> bool:
    if action == "NO_TRADE":
        _debug("_should_activate: action=NO_TRADE -> False")
        return False
    if activation_type == "immediate":
        _debug("_should_activate: activation_type=immediate -> True")
        return True
    if current_close <= 0:
        _debug(f"_should_activate: current_close inválido ({current_close}) -> False")
        return False
    if action == "MARKET_BUY":
        ok = current_close >= entry if entry > 0 else False
        _debug(f"_should_activate BUY: current_close={current_close} entry={entry} -> {ok}")
        return ok
    if action == "MARKET_SELL":
        ok = current_close <= entry if entry > 0 else False
        _debug(f"_should_activate SELL: current_close={current_close} entry={entry} -> {ok}")
        return ok
    _debug(f"_should_activate: action no contemplada ({action}) -> False")
    return False


def _build_mt5_config(args: argparse.Namespace) -> Dict[str, Any] | None:
    if not args.execute_real_mt5:
        _debug("MT5 real execution deshabilitado")
        return None

    _debug("MT5 real execution habilitado; validando credenciales")

    missing = []
    if not args.mt5_login:
        missing.append("mt5_login/MT5_LOGIN")
    if not args.mt5_password:
        missing.append("mt5_password/MT5_PASSWORD")
    if not args.mt5_server:
        missing.append("mt5_server/MT5_SERVER")

    if missing:
        raise RuntimeError(
            "Faltan credenciales de MT5 para ejecutar órdenes reales: "
            + ", ".join(missing)
            + "\n\n"
            + "Opciones para solucionarlo:\n"
            + "1) Variables de entorno (.env): MT5_LOGIN, MT5_PASSWORD, MT5_SERVER\n"
            + "2) Por CLI: --mt5-login <login> --mt5-password <password> --mt5-server <server>\n"
            + "3) En Windows CMD (temporal):\n"
            + "   set MT5_LOGIN=12345678\n"
            + "   set MT5_PASSWORD=tu_password\n"
            + "   set MT5_SERVER=Nombre-Del-Server\n"
            + "   py signal_engine.py --input example_payload.json --once --execute-real-mt5"
        )

    return {
        "login": int(args.mt5_login),
        "password": str(args.mt5_password),
        "server": str(args.mt5_server),
        "path": str(args.mt5_path) if args.mt5_path else None,
        "volume": float(args.mt5_volume),
        "magic": int(args.mt5_magic),
        "deviation": int(args.mt5_deviation),
    }


def _init_mt5_client(mt5_config: Dict[str, Any]):
    _debug(f"Inicializando MT5 (server={mt5_config.get('server')}, path={mt5_config.get('path')})")
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:
        raise RuntimeError("No está instalado MetaTrader5. Instala el paquete para ejecución real.") from exc

    initialized = mt5.initialize(path=mt5_config["path"]) if mt5_config.get("path") else mt5.initialize()
    if not initialized:
        raise RuntimeError(f"No se pudo inicializar MT5: {mt5.last_error()}")

    logged = mt5.login(
        mt5_config["login"],
        password=mt5_config["password"],
        server=mt5_config["server"],
    )
    if not logged:
        last_error = mt5.last_error()
        mt5.shutdown()
        raise RuntimeError(f"Falló login MT5: {last_error}")

    return mt5


def _send_market_order_mt5(
    mt5: Any,
    symbol: str,
    side: str,
    volume: float,
    sl: float,
    tp: float,
    deviation: int,
    magic: int,
    comment: str,
) -> Tuple[bool, str, float]:
    _debug(f"Enviando orden MT5 MARKET side={side}, symbol={symbol}, volume={volume}, sl={sl}, tp={tp}, deviation={deviation}")
    info = mt5.symbol_info(symbol)
    if info is None:
        return False, f"Símbolo no encontrado en MT5: {symbol}", 0.0

    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            return False, f"No se pudo habilitar símbolo en MT5: {symbol}", 0.0

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False, f"No hay tick para símbolo {symbol}", 0.0

    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    price = float(tick.ask if side == "BUY" else tick.bid)

    filling_type = mt5.ORDER_FILLING_IOC
    symbol_filling_mode = getattr(info, "filling_mode", None)
    if symbol_filling_mode == mt5.SYMBOL_FILLING_FOK:
        filling_type = mt5.ORDER_FILLING_FOK
    elif symbol_filling_mode == mt5.SYMBOL_FILLING_RETURN:
        filling_type = mt5.ORDER_FILLING_RETURN

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "deviation": deviation,
        "magic": magic,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_type,
    }

    if sl > 0:
        request["sl"] = sl
    if tp > 0:
        request["tp"] = tp

    result = mt5.order_send(request)
    if result is None:
        return False, f"order_send devolvió None. last_error={mt5.last_error()}", price

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return False, f"retcode={result.retcode}, comment={getattr(result, 'comment', '')}", price

    return True, f"ticket={getattr(result, 'order', 0)}", float(getattr(result, "price", price) or price)


def evaluate_and_execute_setups(db_path: str, payload: Dict[str, Any], mt5_config: Dict[str, Any] | None = None) -> int:
    conn = sqlite3.connect(db_path)
    executed = 0
    mt5 = None
    try:
        if mt5_config:
            mt5 = _init_mt5_client(mt5_config)

        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.setup_id, s.analysis_id, a.symbol, s.action, s.entry, s.sl, s.tp1, s.activation_type, s.trigger_timeframe
            FROM order_setups s
            JOIN analyses a ON a.analysis_id = s.analysis_id
            WHERE s.is_active = 1 AND s.status = 'PENDING'
            ORDER BY s.created_at_utc ASC, s.priority ASC
            """
        )
        rows = cur.fetchall()
        _debug(f"Setups pendientes encontrados={len(rows)}")

        for setup_id, analysis_id, symbol, action, entry, sl, tp1, activation_type, trigger_tf in rows:
            current_close = _latest_close(payload, trigger_tf)
            _debug(
                f"Evaluando setup_id={setup_id}, action={action}, trigger_tf={trigger_tf}, entry={entry}, current_close={current_close}, activation_type={activation_type}"
            )
            if not _should_activate(action, activation_type, _safe_float(entry), current_close):
                _debug(f"Setup {setup_id} NO activado")
                continue

            now_utc = _utc_now()
            side = "BUY" if action == "MARKET_BUY" else "SELL"
            note = f"Activado por {activation_type}. close={current_close}"
            execution_price = current_close

            if mt5:
                ok, mt5_note, execution_price = _send_market_order_mt5(
                    mt5=mt5,
                    symbol=symbol,
                    side=side,
                    volume=float(mt5_config["volume"]),
                    sl=_safe_float(sl),
                    tp=_safe_float(tp1),
                    deviation=int(mt5_config["deviation"]),
                    magic=int(mt5_config["magic"]),
                    comment=f"{setup_id}|{activation_type}",
                )
                note = f"{note}. mt5={mt5_note}"
                if not ok:
                    _debug(f"Setup {setup_id} activado pero falló envío MT5: {mt5_note}")
                    cur.execute(
                        """
                        UPDATE order_setups
                        SET status='FAILED', is_active=0, activation_price=?, activated_at_utc=?
                        WHERE setup_id=?
                        """,
                        (execution_price, now_utc, setup_id),
                    )
                    continue

            _debug(f"Setup {setup_id} ejecutado. side={side}, price={execution_price}")

            cur.execute(
                """
                UPDATE order_setups
                SET status='EXECUTED', is_active=0, activation_price=?, activated_at_utc=?, executed_at_utc=?
                WHERE setup_id=?
                """,
                (execution_price, now_utc, now_utc, setup_id),
            )
            cur.execute(
                """
                INSERT INTO executions (setup_id, analysis_id, symbol, side, execution_price, execution_note, executed_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (setup_id, analysis_id, symbol, side, execution_price, note, now_utc),
            )
            executed += 1

        conn.commit()
    finally:
        if mt5 is not None:
            mt5.shutdown()
        conn.close()
    return executed


def run_cycle(
    input_path: str,
    db_path: str,
    model: str,
    mt5_config: Dict[str, Any] | None = None,
    output: str | None = None,
) -> Tuple[Dict[str, Any], int]:
    _debug(f"Iniciando ciclo input={input_path}, db={db_path}, model={model}")
    payload = _load_json(input_path)
    _debug(f"Payload cargado. symbol={payload.get('symbol')} timeframes={list((payload.get('timeframes') or {}).keys())}")
    result = generate_signal(payload, model)
    store_analysis(db_path, payload, result, model)
    executed = evaluate_and_execute_setups(db_path, payload, mt5_config=mt5_config)
    _debug(f"Ciclo finalizado. analysis_id={result.get('analysis_id')} executed_market_orders={executed}")

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result, executed


def main() -> None:
    global DEBUG
    _load_dotenv_if_available()

    parser = argparse.ArgumentParser(description="SMC engine: IA cada 15m + revisión cada 1m + SQLite + market-only")
    parser.add_argument("--input", default=os.getenv("INPUT_JSON_PATH"), help="JSON de mercado actualizado por MT")
    parser.add_argument("--db", default=os.getenv("SIGNALS_DB_PATH", "signals.db"), help="Ruta SQLite")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5-mini"), help="Modelo OpenAI")
    parser.add_argument("--output", default=os.getenv("OUTPUT_JSON_PATH"), help="Salida JSON de análisis")
    parser.add_argument("--analysis-every-minutes", type=int, default=int(os.getenv("ANALYSIS_EVERY_MINUTES", "15")), help="Frecuencia de análisis IA")
    parser.add_argument("--review-every-minutes", type=int, default=int(os.getenv("REVIEW_EVERY_MINUTES", "1")), help="Frecuencia de revisión/ejecución")
    parser.add_argument("--once", action="store_true", default=_env_bool("RUN_ONCE", False), help="Ejecuta un ciclo único (analiza y revisa una vez)")
    parser.add_argument("--debug", action="store_true", default=_env_bool("DEBUG", False), help="Activa logs detallados de depuración")
    parser.add_argument("--execute-real-mt5", action="store_true", default=_env_bool("EXECUTE_REAL_MT5", False), help="Ejecuta órdenes reales en MT5 (requiere login)")
    parser.add_argument("--mt5-login", default=os.getenv("MT5_LOGIN"), help="Login de cuenta MT5")
    parser.add_argument("--mt5-password", default=os.getenv("MT5_PASSWORD"), help="Password de cuenta MT5")
    parser.add_argument("--mt5-server", default=os.getenv("MT5_SERVER"), help="Servidor de cuenta MT5")
    parser.add_argument("--mt5-path", default=os.getenv("MT5_PATH"), help="Ruta terminal MT5 (opcional)")
    parser.add_argument("--mt5-volume", type=float, default=float(os.getenv("MT5_VOLUME", "0.01")), help="Lote para ejecución real")
    parser.add_argument("--mt5-magic", type=int, default=int(os.getenv("MT5_MAGIC", "20260312")), help="Magic number")
    parser.add_argument("--mt5-deviation", type=int, default=int(os.getenv("MT5_DEVIATION", "20")), help="Desviación de precio")
    args = parser.parse_args()
    if not args.input:
        raise RuntimeError("Falta --input o INPUT_JSON_PATH en .env")

    DEBUG = bool(args.debug)

    input_path = _resolve_path(args.input)
    db_path = _resolve_path(args.db)
    output_path = _resolve_path(args.output) if args.output else None

    _debug("Modo DEBUG activado")
    _debug(
        f"Parámetros: input={input_path}, db={db_path}, model={args.model}, once={args.once}, "
        f"analysis_every={args.analysis_every_minutes}, review_every={args.review_every_minutes}, "
        f"execute_real_mt5={args.execute_real_mt5}"
    )
    mt5_config = _build_mt5_config(args)

    init_db(db_path)

    if args.once:
        result, executed = run_cycle(input_path, db_path, args.model, mt5_config=mt5_config, output=output_path)
        print(json.dumps({"mode": "once", "analysis_id": result.get("analysis_id"), "executed_market_orders": executed}, ensure_ascii=False))
        return

    analysis_sec = max(1, args.analysis_every_minutes) * 60
    review_sec = max(1, args.review_every_minutes) * 60

    last_analysis_ts = 0.0
    while True:
        now_ts = time.time()
        payload = _load_json(input_path)
        _debug(f"Loop continuo: now_ts={now_ts}, last_analysis_ts={last_analysis_ts}")

        # 1) Análisis IA cada N minutos (default 15)
        if now_ts - last_analysis_ts >= analysis_sec:
            result = generate_signal(payload, args.model)
            store_analysis(db_path, payload, result, args.model)
            last_analysis_ts = now_ts
            if output_path:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({
                "at": _utc_now(),
                "event": "analysis",
                "analysis_id": result.get("analysis_id"),
                "symbol": result.get("symbol"),
                "setups": len(result.get("order_setups", [])),
            }, ensure_ascii=False))
            _debug(f"Evento analysis emitido. analysis_id={result.get('analysis_id')}")

        # 2) Revisión/ejecución cada 1 minuto (o valor configurable)
        executed = evaluate_and_execute_setups(db_path, payload, mt5_config=mt5_config)
        print(json.dumps({
            "at": _utc_now(),
            "event": "review",
            "executed_market_orders": executed,
        }, ensure_ascii=False))
        _debug(f"Evento review emitido. executed_market_orders={executed}. sleep={review_sec}s")

        time.sleep(review_sec)


if __name__ == "__main__":
    main()

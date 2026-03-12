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
from typing import Any, Dict, List, Tuple

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
    finally:
        conn.close()


def _to_market_only(action: str) -> str:
    if action in {"BUY_LIMIT", "BUY_STOP", "MARKET_BUY"}:
        return "MARKET_BUY"
    if action in {"SELL_LIMIT", "SELL_STOP", "MARKET_SELL"}:
        return "MARKET_SELL"
    return "NO_TRADE"


def validate_and_normalize(result: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    result.setdefault("symbol", symbol)
    result.setdefault("analysis_id", f"analysis_{int(datetime.now().timestamp())}")
    result.setdefault("market_summary", "")
    result.setdefault("timeframe_bias", {})

    setups = result.get("order_setups") or []
    if not isinstance(setups, list) or not setups:
        setups = [{"setup_id": f"{result['analysis_id']}_1", "priority": 1, "action": "NO_TRADE", "activation_condition": "Sin setup"}]

    normalized: List[Dict[str, Any]] = []
    for idx, setup in enumerate(setups[:3], start=1):
        setup_id = str(setup.get("setup_id") or f"{result['analysis_id']}_{idx}")
        action = str(setup.get("action", "NO_TRADE"))
        if action not in ALLOWED_ACTIONS:
            action = "NO_TRADE"

        confidence = _safe_float(setup.get("confidence"), 0.0)
        rr = _safe_float(setup.get("rr"), 0.0)
        if action != "NO_TRADE" and (confidence < 70 or rr < 1.5):
            action = "NO_TRADE"

        action = _to_market_only(action)  # requisito usuario: solo MARKET

        activation_type = str(setup.get("activation_type", "other"))
        if activation_type not in ALLOWED_ACTIVATION_TYPES:
            activation_type = "other"

        trigger_tf = str(setup.get("trigger_timeframe", "m15"))
        if trigger_tf not in {"m15", "h4", "d1"}:
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
    result["order_setups"] = normalized
    return result


def generate_signal(payload: Dict[str, Any], model: str) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Falta OPENAI_API_KEY en variables de entorno.")

    from openai import OpenAI  # lazy import para permitir tests sin dependencia instalada

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Analiza y genera setups para este payload:\n" + json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
    )
    parsed = json.loads(response.output_text)
    return validate_and_normalize(parsed, symbol=str(payload.get("symbol", "UNKNOWN")))


def store_analysis(db_path: str, payload: Dict[str, Any], result: Dict[str, Any], model: str) -> None:
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
        return False
    if activation_type == "immediate":
        return True
    if current_close <= 0:
        return False
    if action == "MARKET_BUY":
        return current_close >= entry if entry > 0 else False
    if action == "MARKET_SELL":
        return current_close <= entry if entry > 0 else False
    return False


def evaluate_and_execute_setups(db_path: str, payload: Dict[str, Any]) -> int:
    conn = sqlite3.connect(db_path)
    executed = 0
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.setup_id, s.analysis_id, a.symbol, s.action, s.entry, s.activation_type, s.trigger_timeframe
            FROM order_setups s
            JOIN analyses a ON a.analysis_id = s.analysis_id
            WHERE s.is_active = 1 AND s.status = 'PENDING'
            ORDER BY s.created_at_utc ASC, s.priority ASC
            """
        )
        rows = cur.fetchall()

        for setup_id, analysis_id, symbol, action, entry, activation_type, trigger_tf in rows:
            current_close = _latest_close(payload, trigger_tf)
            if not _should_activate(action, activation_type, _safe_float(entry), current_close):
                continue

            now_utc = _utc_now()
            side = "BUY" if action == "MARKET_BUY" else "SELL"
            note = f"Activado por {activation_type}. close={current_close}"

            cur.execute(
                """
                UPDATE order_setups
                SET status='EXECUTED', is_active=0, activation_price=?, activated_at_utc=?, executed_at_utc=?
                WHERE setup_id=?
                """,
                (current_close, now_utc, now_utc, setup_id),
            )
            cur.execute(
                """
                INSERT INTO executions (setup_id, analysis_id, symbol, side, execution_price, execution_note, executed_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (setup_id, analysis_id, symbol, side, current_close, note, now_utc),
            )
            executed += 1

        conn.commit()
    finally:
        conn.close()
    return executed


def run_cycle(input_path: str, db_path: str, model: str, output: str | None = None) -> Tuple[Dict[str, Any], int]:
    payload = _load_json(input_path)
    result = generate_signal(payload, model)
    store_analysis(db_path, payload, result, model)
    executed = evaluate_and_execute_setups(db_path, payload)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result, executed


def main() -> None:
    parser = argparse.ArgumentParser(description="SMC engine: IA cada 15m + revisión cada 1m + SQLite + market-only")
    parser.add_argument("--input", required=True, help="JSON de mercado actualizado por MT")
    parser.add_argument("--db", default="signals.db", help="Ruta SQLite")
    parser.add_argument("--model", default="gpt-5-mini", help="Modelo OpenAI")
    parser.add_argument("--output", help="Salida JSON de análisis")
    parser.add_argument("--analysis-every-minutes", type=int, default=15, help="Frecuencia de análisis IA")
    parser.add_argument("--review-every-minutes", type=int, default=1, help="Frecuencia de revisión/ejecución")
    parser.add_argument("--once", action="store_true", help="Ejecuta un ciclo único (analiza y revisa una vez)")
    args = parser.parse_args()

    init_db(args.db)

    if args.once:
        result, executed = run_cycle(args.input, args.db, args.model, args.output)
        print(json.dumps({"mode": "once", "analysis_id": result.get("analysis_id"), "executed_market_orders": executed}, ensure_ascii=False))
        return

    analysis_sec = max(1, args.analysis_every_minutes) * 60
    review_sec = max(1, args.review_every_minutes) * 60

    last_analysis_ts = 0.0
    while True:
        now_ts = time.time()
        payload = _load_json(args.input)

        # 1) Análisis IA cada N minutos (default 15)
        if now_ts - last_analysis_ts >= analysis_sec:
            result = generate_signal(payload, args.model)
            store_analysis(args.db, payload, result, args.model)
            last_analysis_ts = now_ts
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps({
                "at": _utc_now(),
                "event": "analysis",
                "analysis_id": result.get("analysis_id"),
                "symbol": result.get("symbol"),
                "setups": len(result.get("order_setups", [])),
            }, ensure_ascii=False))

        # 2) Revisión/ejecución cada 1 minuto (o valor configurable)
        executed = evaluate_and_execute_setups(args.db, payload)
        print(json.dumps({
            "at": _utc_now(),
            "event": "review",
            "executed_market_orders": executed,
        }, ensure_ascii=False))

        time.sleep(review_sec)


if __name__ == "__main__":
    main()

"""Servidor Flask Principal y API REST para Huarizo AI.

Ejecuta en el puerto 5050 (aislado del proyecto principal en 5000).
Expone endpoints para Trading en Alpaca Paper, Market Data, Noticias Benzinga,
Corporate Actions, Análisis Técnico Cuantitativo, Torneo de Estrategias y Bracket Orders.
"""

import json
import os
import sys
import tempfile
import threading
import time
from datetime import date, datetime
from typing import List, Optional
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, request, send_from_directory
from dotenv import load_dotenv

# Cargar variables de entorno locales
load_dotenv()

# Asegurar importación de módulos locales
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, CURRENT_DIR)

from alpaca_service import AlpacaService
from alpaca_tools import AlpacaTools, ToolUnavailable
from technical_engine import compute_technical_indicators, volume_allows_entry
from pattern_engine import detect_candlestick_patterns, detect_chart_patterns
from backtest_engine import run_backtest, STRATEGY_PRESETS
import agent_engine
from agent_engine import HuarizoAgent
import journal as journal_mod
from exit_manager import ExitManager, is_market_open
from execution_ledger import ExecutionLedger
from risk_gate import build_client_order_id, evaluate_new_entry
from account_onboarding import (
    EXPECTED_START_EQUITY as CONTEST_EXPECTED_START_EQUITY,
    build_onboarding_record,
    load_onboarding,
    save_onboarding,
    validate_armed_account,
    validate_contest_account,
)

_ET = ZoneInfo("America/New_York")

app = Flask(__name__, static_folder="static", static_url_path="")

# Instanciar servicios
alpaca_service = AlpacaService()
huarizo_agent = HuarizoAgent()

# Ledger de ejecución: única vía autorizada para convertir una propuesta en
# orden. Persiste autorización + reserva + llave de idempotencia, así que un
# reenvío o un ciclo repetido no pueden duplicar órdenes. Se crea antes del
# exit manager porque éste lo recibe para compartir la reclamación de cierre.
exec_ledger = ExecutionLedger()

exit_manager = ExitManager(alpaca_service, interval_seconds=60,
                           ledger=exec_ledger)

# Los daemons (exit manager y autopilot de opciones) se arrancan al importar
# porque este modulo ES el servidor. Pero `import app` tambien lo hacen
# herramientas de un solo tiro (p.ej. `smoke_contest_flow.py`) que solo quieren
# reusar `submit_authorized_option_order`: ahi un exit manager en segundo plano
# podria cerrar posiciones mientras corre el script. Con
# HUARIZO_DISABLE_DAEMONS=true el modulo se importa sin lanzar ningun hilo.
DAEMONS_DISABLED = os.getenv("HUARIZO_DISABLE_DAEMONS", "false").lower() == "true"
if not DAEMONS_DISABLED:
    exit_manager.start()

# Ventana de idempotencia para órdenes de opciones. Un reenvío dentro de la
# ventana se rechaza; pasada la ventana se permite una orden nueva, para que
# comprar el mismo contrato más tarde no quede bloqueado para siempre.
OPTIONS_IDEMPOTENCY_BUCKET_SECONDS = int(
    os.getenv("HUARIZO_OPTIONS_IDEMPOTENCY_BUCKET", "900")
)
OPTIONS_CONTRACT_MULTIPLIER = 100  # 1 contrato = 100 acciones

OPTIONS_AUTOPILOT_ENV_ENABLED = os.getenv("HUARIZO_AUTOPILOT_ENABLED", "false").lower() == "true"

# El interruptor del autopilot se persiste, y no es comodidad: es la diferencia
# entre un bot que sigue operando y uno que se queda mudo sin avisar.
#
# El estado vivia SOLO en memoria (`{"enabled": False}` al importar), asi que
# cada reinicio — caida, corte de luz, despliegue — apagaba el trading en
# silencio. Nadie se enteraba hasta la manana siguiente, y con una fecha de
# entrega encima eso son horas de mercado perdidas sin que nada falle
# visiblemente: el proceso esta vivo, responde HTTP y no opera.
#
# Lo que NO se persiste es `paused_reason`, y es deliberado. Las pausas de
# riesgo ya tienen su propia fuente de verdad en disco (`DailyLossTracker`), y
# `_run_one_options_cycle` la consulta en el Limite 0 de CADA ciclo. Guardarlas
# tambien aqui crearia dos estados que pueden discrepar, y el riesgo es que el
# que gane sea el equivocado. Lo unico propio de este archivo es la intencion
# del operador: encendido o apagado.
OPTIONS_AUTOPILOT_STATE_PATH = os.getenv(
    "HUARIZO_AUTOPILOT_STATE_PATH",
    os.path.join(CURRENT_DIR, "options_autopilot_state.json"),
)


def _autopilot_persist() -> bool:
    """Guarda `enabled` de forma atomica. False si no se pudo escribir.

    Avisa por consola en vez de fallar la peticion. Apagar es el freno de
    emergencia y no puede quedar bloqueado porque el disco este roto; encender
    tampoco se bloquea, porque en esta sesion el bot opera igual, solo que no
    sobreviviria a un reinicio. En ambos casos el resultado viaja al llamador
    (`persisted`) para que quien opera lo vea.
    """
    try:
        directory = os.path.dirname(OPTIONS_AUTOPILOT_STATE_PATH) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".autopilot_", suffix=".tmp")
    except OSError as e:
        print(f"[autopilot] Could not prepare the state file: {e}")
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "enabled": bool(options_autopilot_state.get("enabled")),
                    "saved_at": time.time(),
                },
                handle,
            )
        os.replace(tmp, OPTIONS_AUTOPILOT_STATE_PATH)
        return True
    except (OSError, TypeError, ValueError) as e:
        print(f"[autopilot] Could not persist the state: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _autopilot_restore() -> bool:
    """Recupera `enabled` del disco al arrancar. Fail-closed.

    Cualquier duda deja el autopilot APAGADO: un archivo ausente, corrupto o
    escrito a mano no puede encender el trading. Y el kill-switch de entorno
    manda sobre el disco, para que desactivarlo por variables de entorno no lo
    vuelva a prender un archivo viejo.
    """
    if not OPTIONS_AUTOPILOT_ENV_ENABLED:
        return False
    try:
        with open(OPTIONS_AUTOPILOT_STATE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    # Solo un booleano explicito: ni "true" ni 1. Es la diferencia entre
    # restaurar una decision del operador y dejarse arrancar por cualquier cosa
    # que quepa en un JSON.
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return False
    options_autopilot_state["enabled"] = enabled
    return enabled


options_autopilot_state = {"enabled": False, "paused_reason": None}
options_lock = threading.Lock()

# Restaurar va DESPUES de crear el estado, porque `_autopilot_restore` lo muta.
if _autopilot_restore():
    print(">> [OPTIONS AUTOPILOT] Reanudado desde el estado guardado (enabled=True).")

# Capa de herramientas OFICIAL de Alpaca (MCP server o CLI). La exige el
# concurso. Arranca APAGADA (HUARIZO_ALPACA_TOOL=none): mientras este apagada
# la app se comporta exactamente igual que antes. Construirla no abre ningun
# subproceso; la sesion se crea en la primera llamada util.
ALPACA_TOOLS = AlpacaTools()

# Registro de onboarding de la cuenta de concurso: la constancia de CONTRA QUÉ
# cuenta se armó el bot. Sin él no hay validación continua posible. Si el
# archivo falta o se corrompe se degrada a "no armado" (nunca levanta).
ONBOARDING_PATH = os.path.join(CURRENT_DIR, "account_onboarding.json")


# ── 1. RUTAS DE FRONTEND ESTÁTICO ───────────────────────────────────────────

@app.route("/")
def serve_index():
    return send_from_directory(app.static_folder, "index.html")


# ── 2. ENDPOINTS DE CUENTA Y PORTAFOLIO ─────────────────────────────────────

@app.route("/api/account", methods=["GET"])
def api_get_account():
    try:
        acct = alpaca_service.get_account()
        return jsonify({"success": True, "account": acct})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/positions", methods=["GET"])
def api_get_positions():
    try:
        positions = alpaca_service.get_positions()
        return jsonify({"success": True, "positions": positions})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/history", methods=["GET"])
def api_get_history():
    period = request.args.get("period", "1M")
    timeframe = request.args.get("timeframe", "1D")
    try:
        hist = alpaca_service.get_portfolio_history(period=period, timeframe=timeframe)
        return jsonify({"success": True, "history": hist})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── 3. SCREENER DE MERCADO EN VIVO ───────────────────────────────────────────

@app.route("/api/screener/movers", methods=["GET"])
def api_get_movers():
    try:
        movers = alpaca_service.get_top_movers(top=10)
        return jsonify({"success": True, "movers": movers})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/screener/most-actives", methods=["GET"])
def api_get_most_actives():
    try:
        actives = alpaca_service.get_most_active(by="volume", top=10)
        return jsonify({"success": True, "most_actives": actives})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── 4. ANÁLISIS DE MERCADO, TÉCNICO Y PATRONES ───────────────────────────────

@app.route("/api/market/analysis/<ticker>", methods=["GET"])
def api_market_analysis(ticker):
    try:
        bars = alpaca_service.get_stock_bars(ticker.upper(), days=250)
        if bars.empty:
            return jsonify({"success": False, "error": f"No data available for {ticker}"}), 404

        tech = compute_technical_indicators(bars)
        candles = detect_candlestick_patterns(bars)
        charts = detect_chart_patterns(bars)
        snapshot = alpaca_service.get_stock_snapshot(ticker.upper())

        return jsonify({
            "success": True,
            "ticker": ticker.upper(),
            "snapshot": snapshot,
            "technical": tech,
            "candlesticks": candles,
            "chart_patterns": charts
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/market/news/<ticker>", methods=["GET"])
def api_market_news(ticker):
    try:
        news = alpaca_service.get_stock_news(ticker.upper(), limit=8)
        return jsonify({"success": True, "ticker": ticker.upper(), "news": news})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/market/dividends/<ticker>", methods=["GET"])
def api_market_dividends(ticker):
    try:
        divs = alpaca_service.get_dividends(ticker.upper(), days=365)
        return jsonify({"success": True, "ticker": ticker.upper(), "dividends": divs})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── 5. AGENTE HUARIZO AI Y TORNEO DE ESTRATEGIAS ────────────────────────────

@app.route("/api/agent/analyze", methods=["POST"])
def api_agent_analyze():
    data = request.get_json() or {}
    ticker = data.get("ticker", "NVDA").upper()
    try:
        analysis = huarizo_agent.analyze(ticker, alpaca_service)
        return jsonify({"success": True, "analysis": analysis})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/agent/backtest", methods=["POST"])
def api_agent_backtest():
    data = request.get_json() or {}
    ticker = data.get("ticker", "NVDA").upper()
    strategy = data.get("strategy", "trend_following")
    custom_config = data.get("custom_config")

    try:
        bars = alpaca_service.get_stock_bars(ticker, days=250)
        if bars.empty:
            return jsonify({"success": False, "error": f"No data available for {ticker}"}), 404

        result = run_backtest(bars, strategy=strategy, custom_config=custom_config)
        return jsonify({"success": True, "ticker": ticker, "result": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── ESTADO DEL AUTO-PILOT EN SEGUNDO PLANO ───────────────────────────────────

autopilot_state = {
    "is_running": False,
    "interval_seconds": 60,
    "last_run": None,
    "auto_submit": True,
    "max_orders": 3,
    "logs": [],
    "total_cycles": 0,
    "executed_orders_count": 0
}
autopilot_thread = None
autopilot_lock = threading.Lock()

# Un solo ciclo de autopilot a la vez. El 31-ago se lanzaron varios hilos en
# paralelo porque el arranque comprobaba `is_alive()` fuera del lock: N clicks
# rápidos en "start" creaban N hilos, cada uno enviando hasta 3 órdenes.
autopilot_busy = False
autopilot_run_lock = threading.Lock()


def run_autopilot_once(auto_submit: bool, max_orders: int, candidates=None):
    """Ejecuta un ciclo del autopilot, o devuelve None si ya hay uno corriendo."""
    global autopilot_busy

    with autopilot_run_lock:
        if autopilot_busy:
            return None
        autopilot_busy = True

    try:
        return huarizo_agent.run_autonomous_autopilot(
            service=alpaca_service,
            auto_submit=auto_submit,
            max_orders=max_orders,
            candidate_tickers=candidates
        )
    finally:
        with autopilot_run_lock:
            autopilot_busy = False


def autopilot_background_worker():
    """Hilo continuo en segundo plano que escanea y opera en bucle."""
    print(">> [AUTOPILOT THREAD] Background thread started.")
    while True:
        with autopilot_lock:
            if not autopilot_state["is_running"]:
                print(">> [AUTOPILOT THREAD] Thread stopped by user.")
                break
            auto_submit = autopilot_state["auto_submit"]
            max_orders = autopilot_state["max_orders"]
            interval = autopilot_state["interval_seconds"]

        try:
            timestamp = time.strftime("%H:%M:%S")
            res = run_autopilot_once(auto_submit, max_orders)
            if res is None:
                # Ya hay un ciclo en curso: este hilo se limita a esperar.
                time.sleep(2)
                continue
            with autopilot_lock:
                autopilot_state["last_run"] = timestamp
                autopilot_state["total_cycles"] += 1
                autopilot_state["executed_orders_count"] += res.get("orders_executed_count", 0)
                
                # Conservar los últimos 80 logs
                for log_msg in res.get("logs", []):
                    autopilot_state["logs"].append(f"[{timestamp}] {log_msg}")
                if len(autopilot_state["logs"]) > 80:
                    autopilot_state["logs"] = autopilot_state["logs"][-80:]
        except Exception as e:
            with autopilot_lock:
                autopilot_state["logs"].append(f"[{time.strftime('%H:%M:%S')}] Error in cycle: {e}")

        # Wait for the interval before the next scan (checking every 2s if stopped)
        for _ in range(int(interval / 2)):
            with autopilot_lock:
                if not autopilot_state["is_running"]:
                    break
            time.sleep(2)


@app.route("/api/agent/auto-pilot/start", methods=["POST"])
def api_agent_autopilot_start():
    global autopilot_thread
    data = request.get_json() or {}
    with autopilot_lock:
        autopilot_state["is_running"] = True
        autopilot_state["auto_submit"] = bool(data.get("auto_submit", True))
        autopilot_state["max_orders"] = int(data.get("max_orders", 3))
        autopilot_state["interval_seconds"] = int(data.get("interval", 60))
        autopilot_state["logs"].append(f"[{time.strftime('%H:%M:%S')}] [AUTOPILOT ENABLED] Scanning every {autopilot_state['interval_seconds']}s...")

    # El arranque debe ser atómico: si dos peticiones llegan juntas y la
    # comprobación queda fuera del lock, ambas crean su propio hilo.
    with autopilot_lock:
        if autopilot_thread is None or not autopilot_thread.is_alive():
            autopilot_thread = threading.Thread(target=autopilot_background_worker, daemon=True)
            autopilot_thread.start()

    return jsonify({"success": True, "state": autopilot_state})


@app.route("/api/agent/auto-pilot/stop", methods=["POST"])
def api_agent_autopilot_stop():
    with autopilot_lock:
        autopilot_state["is_running"] = False
        autopilot_state["logs"].append(f"[{time.strftime('%H:%M:%S')}] [AUTOPILOT STOPPED] Autonomous scan paused by the user.")

    return jsonify({"success": True, "state": autopilot_state})


@app.route("/api/agent/auto-pilot/status", methods=["GET"])
def api_agent_autopilot_status():
    # El frontend necesita saber si el flujo de acciones está vivo antes de
    # pintar el botón de bracket orders. Sin esta bandera la interfaz mostraba
    # un CTA que siempre responde 403 (el backend rechaza en
    # `api_place_bracket_order` cuando el autopilot de acciones está apagado),
    # y un botón que parece habilitado y no lo es es peor que uno deshabilitado.
    with autopilot_lock:
        return jsonify({
            "success": True,
            "state": autopilot_state,
            "stock_autopilot_enabled": agent_engine.STOCK_AUTOPILOT_ENABLED,
        })


@app.route("/api/agent/auto-pilot", methods=["POST"])
def api_agent_autopilot_oneshot():
    data = request.get_json() or {}
    auto_submit = bool(data.get("auto_submit", True))
    max_orders = int(data.get("max_orders", 3))
    candidates = data.get("candidates")

    results = run_autopilot_once(auto_submit, max_orders, candidates)
    if results is None:
        return jsonify({
            "success": False,
            "error": "autopilot_cycle_already_running",
            "message": "A cycle is already running; this request was dropped to avoid duplicate orders."
        }), 409

    return jsonify(results)


# ── 5b. OPCIONES: CADENA, PROPUESTAS, ÓRDENES Y AUTOPILOT ───────────────────

@app.route("/api/options/chain/<ticker>", methods=["GET"])
def api_options_chain(ticker):
    try:
        ticker = ticker.upper()
        contracts = alpaca_service.get_options_chain(ticker)
        if not contracts:
            return jsonify({"success": False,
                            "error": f"No options chain for {ticker}"}), 404
        snap = alpaca_service.get_stock_snapshot(ticker)
        spot = float(snap.get("price", 0.0))
        contracts = alpaca_service.enrich_chain_with_oi(
            contracts, spot=spot, n_strikes=None, expiries_limit=4)
        atm_ivs = []
        for c in contracts:
            iv_val = c.get("iv")
            if isinstance(iv_val, (int, float)) and spot > 0 \
                    and abs(c.get("strike", 0.0) - spot) / spot < 0.02:
                atm_ivs.append(float(iv_val))
        oi_expiries = sorted({
            str(c.get("expiry") or "")[:10] for c in contracts
            if c.get("open_interest") is not None
            and str(c.get("expiry") or "")[:10]
        })
        return jsonify({
            "success": True, "ticker": ticker, "spot": spot,
            "atm_iv_avg": round(sum(atm_ivs) / len(atm_ivs), 4) if atm_ivs else None,
            "oi_expiries": oi_expiries,
            "contracts": contracts,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/options/proposal", methods=["POST"])
def api_options_proposal():
    data = request.get_json() or {}
    ticker = (data.get("ticker") or "").upper()
    overrides = data.get("config")
    strategy_type = data.get("strategy_type")
    premium_strategy = data.get("premium_strategy")
    target_strike = data.get("target_strike")
    if not ticker:
        return jsonify({"success": False, "error": "ticker required"}), 400
    try:
        proposal = huarizo_agent.build_options_proposal(
            ticker, alpaca_service,
            config_overrides=overrides,
            strategy_type=strategy_type,
            premium_strategy=premium_strategy,
            target_strike=target_strike,
        )
        return jsonify({"success": True, "proposal": proposal})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


def submit_authorized_option_order(occ_symbol, qty, ask_val, direction="",
                                   account=None, open_positions=None):
    """ÚNICA vía autorizada para enviar una orden de opciones.

    La usan tanto el endpoint manual (`/api/options/order`) como el autopilot
    autónomo. Antes el autopilot llamaba a `place_option_order` directamente y
    se quedó sin ledger cuando la Task 3 cableó sólo el endpoint manual: sin
    autorización ni llave de idempotencia, dos ciclos en la misma ventana
    duplicaban la orden.

    Devuelve `{"ok": True, "order": ..., "client_order_id": ...}` o
    `{"ok": False, "error": ..., "status": <http>, "risk_verdict": ...}`.
    Nunca lanza: el llamador decide qué hacer con el rechazo.
    """
    try:
        occ_norm = agent_engine.assert_option_symbol(occ_symbol)
    except ValueError:
        return {"ok": False, "status": 400,
                "error": f"invalid OCC option symbol: {occ_symbol!r}"}

    if account is None or open_positions is None:
        try:
            account = alpaca_service.get_account()
            open_positions = alpaca_service.get_positions()
        except Exception as e:
            return {"ok": False, "status": 503,
                    "error": f"risk snapshot unavailable, order refused: {e}"}

    # El multiplicador importa: 1 contrato a 3 USD son 300 USD de exposición,
    # no 3. Sin x100 el gate compararía la prima contra el techo por orden y
    # dejaría pasar tamaños desproporcionados.
    verdict = evaluate_new_entry(
        account=account,
        open_positions=open_positions,
        symbol=occ_norm,
        qty=qty,
        price=ask_val * OPTIONS_CONTRACT_MULTIPLIER,
        limits=huarizo_agent.risk_limits,
    )

    bucket = int(time.time() // OPTIONS_IDEMPOTENCY_BUCKET_SECONDS)
    auth = exec_ledger.authorize({"cycle_id": str(bucket), "occ_symbol": occ_norm},
                                 verdict)
    if auth.get("status") != "authorized":
        return {"ok": False, "status": 409, "risk_verdict": verdict,
                "error": f"order not authorized: {auth.get('reason')}"}

    # `now_epoch` es OBLIGATORIO y no opcional. Sin el, `build_client_order_id`
    # deja el bucket en 0 y la llave se vuelve CONSTANTE para un contrato:
    # `huarizo-SPY26090-buy-longcall-0`. Como la reserva del ledger se guarda
    # con esa llave y nunca caduca, el mismo contrato quedaba bloqueado para
    # SIEMPRE despues de la primera orden (409 permanente), que es justo lo que
    # el comentario de arriba dice que no debe pasar. Se vio en el smoke de
    # concurso del 2026-09-02: la llave impresa terminaba en "-0".
    now_epoch = time.time()
    client_order_id = build_client_order_id(
        symbol=occ_norm, side="buy", strategy=str(direction or ""),
        bucket_seconds=OPTIONS_IDEMPOTENCY_BUCKET_SECONDS,
        now_epoch=now_epoch,
    )
    reservation = exec_ledger.reserve(auth["authorization_id"], occ_norm,
                                      "buy", qty, client_order_id)
    if reservation.get("duplicate"):
        return {"ok": False, "status": 409, "client_order_id": client_order_id,
                "error": "duplicate submission: this order is already reserved",
                "record": reservation.get("record")}
    if not reservation.get("reserved"):
        return {"ok": False, "status": 409,
                "error": f"could not reserve order: {reservation.get('reason')}"}

    try:
        order = alpaca_service.place_option_order(
            occ_norm, qty=qty, action="buy_to_open",
            limit_price=ask_val, time_in_force="day",
            client_order_id=client_order_id,
        )
    except ValueError as ve:
        return {"ok": False, "status": 400, "error": str(ve)}
    except Exception as e:
        return {"ok": False, "status": 502, "error": str(e)}

    try:
        exec_ledger.mark_submitted(client_order_id, order.get("id"))
    except Exception as le:
        print(f"[options_order] Ledger mark_submitted failed for {client_order_id}: {le}")

    return {"ok": True, "order": order, "client_order_id": client_order_id,
            "occ_symbol": occ_norm}


@app.route("/api/options/order", methods=["POST"])
def api_options_order():
    data = request.get_json() or {}
    ticker = (data.get("ticker") or "").upper()
    direction = data.get("direction")
    contract = data.get("contract") or {}
    sizing = data.get("sizing") or {}
    exit_plan = data.get("exit_plan") or {}
    try:
        qty = int(sizing.get("qty", 0))
    except (TypeError, ValueError):
        qty = 0
    occ = contract.get("occ_symbol")
    if not ticker or not occ or qty <= 0:
        return jsonify({"success": False,
                        "error": "Incomplete proposal (ticker, contract.occ_symbol, sizing.qty)"}), 400
    try:
        ask_val = float(contract.get("ask") or 0)
    except (TypeError, ValueError):
        ask_val = 0.0
    if ask_val <= 0:
        return jsonify({"success": False,
                        "error": "contract.ask required and must be positive (limit orders only)"}), 400

    outcome = submit_authorized_option_order(
        occ_symbol=occ, qty=qty, ask_val=ask_val, direction=str(direction or ""),
    )
    if not outcome["ok"]:
        body = {"success": False, "error": outcome["error"]}
        if "risk_verdict" in outcome:
            body["risk_verdict"] = outcome["risk_verdict"]
        return jsonify(body), outcome["status"]

    order = outcome["order"]
    try:
        entry = journal_mod.add_entry({
            "ts_entry": datetime.now(_ET).strftime("%Y-%m-%dT%H:%M:%S"),
            "ticker": ticker, "occ": outcome["occ_symbol"],
            "direction": direction, "qty": qty,
            "entry_ask": ask_val,
            "order_id": order.get("id"),
            "exit_plan": exit_plan,
        })
    except Exception as je:
        print(f"[options_order] Journal failed after order: {je}")
        try:
            alpaca_service.cancel_order(order.get("id"))
        except Exception as ce:
            print(f"[options_order] Could not cancel orphan order {order.get('id')}: {ce}")
        return jsonify({"success": False,
                        "error": f"Order placed but journal write failed; order {order.get('id')} cancelled if possible"}), 502
    return jsonify({"success": True, "order": order, "journal_entry": entry})


@app.route("/api/options/journal", methods=["GET"])
def api_options_journal():
    try:
        entries = journal_mod.load_journal()
        closed = [e for e in entries if e.get("status") == "closed"]
        open_like = [e for e in entries if e.get("status") in ("open", "closing")]
        realized = journal_mod.realized_pnl()
        return jsonify({
            "success": True, "entries": entries,
            "summary": {
                "total": len(entries),
                "open": len(open_like),
                "closed": len(closed),
                "realized_pnl": round(realized, 2),
            },
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/options/close/<int:journal_id>", methods=["POST"])
def api_options_manual_close(journal_id):
    try:
        entry = next((e for e in journal_mod.load_journal()
                      if int(e.get("id", -1)) == journal_id), None)
    except (TypeError, ValueError):
        entry = None
    if not entry:
        return jsonify({"success": False, "error": "Position not found"}), 404

    # Reclamación atómica: si el exit manager ya está cerrando esta entrada,
    # la reclamación falla y no se duplica la orden. Antes se comprobaba
    # `status == "open"` y DESPUÉS se marcaba "closing", con una ventana en la
    # que el daemon y este endpoint podían enviar dos cierres del mismo contrato.
    claim = exec_ledger.begin_close(journal_id, "manual")
    if not claim.get("claimed"):
        return jsonify({"success": False,
                        "error": f"Position not closable: {claim.get('reason')}",
                        "status": claim.get("status")}), 409
    try:
        order = alpaca_service.close_option_position(str(entry.get("occ")),
                                                     qty=int(entry.get("qty", 1)))
    except ValueError as ve:
        exec_ledger.finish_close(journal_id, None, False)
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        exec_ledger.finish_close(journal_id, None, False)
        return jsonify({"success": False, "error": str(e)}), 502
    exec_ledger.mark_close_submitted(journal_id, order.get("id"))
    return jsonify({"success": True, "order": order})


def expected_start_equity() -> float:
    """Base de frescura del onboarding: 100.000 USD que exige el concurso.

    Sólo se sobreescribe a mano (`HUARIZO_CONTEST_BASELINE_EQUITY`) para
    re-onboardear una cuenta ya operada si se pierde el registro: sin eso, un
    archivo borrado dejaría al bot permanentemente fuera de combate porque el
    equity derivado ya no cae en la banda de +/-1%. Es una acción deliberada
    del operador, no una relajación automática del chequeo.
    """
    raw = os.getenv("HUARIZO_CONTEST_BASELINE_EQUITY")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return CONTEST_EXPECTED_START_EQUITY


def _contest_account_snapshot():
    """Foto de cuenta para validar el concurso. Nunca levanta.

    Sin la herramienta oficial se usa el servicio. Con ella activa se hace MERGE
    (no reemplazo): la herramienta manda en los números, pero se conservan
    `id`/`account_number` del servicio, que es la única identidad disponible
    — el snapshot del MCP no la trae, y sin identidad no hay validación
    continua.
    """
    errors = []
    account = {}
    try:
        account = alpaca_service.get_account() or {}
    except Exception as e:  # noqa: BLE001 - se reporta, no se propaga
        errors.append(f"alpaca_service: {e}")
    if not isinstance(account, dict):
        account = {}

    if ALPACA_TOOLS.method != "none":
        tool_snapshot = None
        try:
            tool_snapshot = ALPACA_TOOLS.get_account_snapshot()
        except Exception as e:  # noqa: BLE001
            errors.append(f"alpaca_tool: {e}")
        if isinstance(tool_snapshot, dict):
            merged = dict(account)
            for key, value in tool_snapshot.items():
                if key == "source" or value is None:
                    continue
                # El MCP manda status="" cuando no lo trae; no debe pisar al
                # status real, o la cuenta parecería no validable.
                if isinstance(value, str) and not value.strip():
                    continue
                merged[key] = value
            account = merged

    if not account:
        return None, "; ".join(errors) or None
    return account, "; ".join(errors) or None


@app.route("/api/options/autopilot", methods=["GET", "POST"])
def api_options_autopilot():
    if request.method == "GET":
        with options_lock:
            state = dict(options_autopilot_state)
        state["env_kill_switch"] = not OPTIONS_AUTOPILOT_ENV_ENABLED
        if not OPTIONS_AUTOPILOT_ENV_ENABLED:
            state["status_label"] = "DISABLED (kill-switch)"
        elif state.get("paused_reason"):
            state["status_label"] = f"PAUSED ({state['paused_reason']})"
        else:
            state["status_label"] = "ON" if state.get("enabled") else "OFF"
        return jsonify({"success": True, "state": state})

    # ── POST ────────────────────────────────────────────────────────────────
    data = request.get_json(silent=True) or {}
    enable = bool(data.get("enabled"))

    # APAGAR es el freno de emergencia: va PRIMERO y sin condiciones. No puede
    # quedar bloqueado por una validación, por la red ni por un archivo de
    # estado corrupto. Por eso se atiende antes que cualquier otra cosa.
    if not enable:
        with options_lock:
            options_autopilot_state["enabled"] = False
            options_autopilot_state["paused_reason"] = None
            state = dict(options_autopilot_state)
        # Persistir DESPUES de apagar en memoria: si el disco falla, el bot ya
        # esta detenido de todos modos. El freno nunca depende del archivo.
        persisted = _autopilot_persist()
        return jsonify({"success": True, "state": state, "persisted": persisted})

    if not OPTIONS_AUTOPILOT_ENV_ENABLED:
        return jsonify({"success": False,
                        "error": "Autopilot disabled: set env HUARIZO_AUTOPILOT_ENABLED=true"}), 403

    # La foto se toma FUERA del lock: puede tardar (MCP/CLI) y no debe
    # congelar al worker del autopilot.
    snapshot, snapshot_error = _contest_account_snapshot()
    if not snapshot:
        # Fail-closed: sin foto no se arma una cuenta sin verificar.
        return jsonify({"success": False,
                        "reason": "account_snapshot_unavailable",
                        "error": "Could not verify the Alpaca account: "
                                 f"{snapshot_error}",
                        "detail": {"error": snapshot_error}}), 409

    onboarding = load_onboarding(ONBOARDING_PATH)
    if onboarding is None:
        # Primera vez: onboarding estricto (cuenta nueva, equity ≈ 100k).
        verdict = validate_contest_account(snapshot, expected_start_equity())
    else:
        # Ya armado: identidad y salud. El equity puede derivar libremente.
        verdict = validate_armed_account(snapshot, onboarding)

    if not verdict["valid"]:
        return jsonify({"success": False,
                        "reason": verdict["reason"],
                        "error": f"Invalid contest account: "
                                 f"{verdict['reason']}",
                        "detail": verdict["detail"],
                        "onboarded": onboarding is not None}), 409

    if onboarding is None:
        onboarding = build_onboarding_record(snapshot, expected_start_equity())
        save_onboarding(ONBOARDING_PATH, onboarding)

    with options_lock:
        options_autopilot_state["enabled"] = True
        # Limpiar la pausa en AMBOS sentidos: re-habilitar tras un cooldown
        # diario debe funcionar sin requerir un disable intermedio.
        options_autopilot_state["paused_reason"] = None
        state = dict(options_autopilot_state)
    # `persisted` va en la respuesta a proposito: si es False el bot opera en
    # esta sesion pero NO arrancaria solo tras un reinicio, y eso tiene que
    # verse en el momento, no descubrirse a la manana siguiente.
    persisted = _autopilot_persist()
    return jsonify({"success": True, "state": state, "persisted": persisted,
                    "onboarding": onboarding, "validation": verdict})


# ── 5c. AUTOPILOT DE OPCIONES (aperturas autónomas con límites duros) ───────

OPTIONS_SCAN_INTERVAL_SECONDS = 300  # 1 ciclo de apertura cada 5 min

# Universo de respaldo del autopilot de opciones.
#
# El screener de Alpaca devuelve lo MAS movido, que con frecuencia NO es
# operable por opciones: el 2026-09-02 los primeros gainers fueron warrants
# (RCKTW, GFAIW, KWMWW) sin cadena alguna, y `get_most_actives` devuelve lista
# vacia en el plan de datos gratuito. Con el screener como unica fuente, el
# daemon corria cada 5 min y JAMAS encontraba candidatos: la autonomia era
# teatro. Contrastado el mismo dia: un universo liquido dio 7 propuestas
# validas de 8 simbolos.
#
# El screener conserva la PRIORIDAD (es el que "descubre"): si un gainer es
# operable, gana. Este respaldo solo garantiza que el ciclo tenga siempre
# nombres con cadena donde aplicar el torneo.
# Sobre-escribible con HUARIZO_OPTIONS_UNIVERSE (separado por comas).
DEFAULT_OPTIONS_UNIVERSE = "SPY,QQQ,AAPL,NVDA,MSFT,AMD,TSLA,META,AMZN,GOOGL"
OPTIONS_UNIVERSE = [
    s.strip().upper()
    for s in os.getenv("HUARIZO_OPTIONS_UNIVERSE", DEFAULT_OPTIONS_UNIVERSE).split(",")
    if s.strip()
]
# Techo de analisis por ciclo. Cada candidato cuesta 250 barras + cadena +
# enriquecimiento de OI + 4 backtests, asi que no se analiza el universo entero.
OPTIONS_MAX_CANDIDATES = 8


def _run_one_options_cycle(now: Optional[datetime] = None):
    """Un ciclo de apertura autonoma. Limites duros del spec:
    max 3 posiciones abiertas · pausa por daily loss <= -5% · 1 apertura/ciclo.
    Reutiliza el MISMO pipeline de propuesta que el copiloto.

    `now` es inyectable para tests deterministas de horario.
    """
    # Limite -1: horario de mercado. El exit manager SI respetaba el horario
    # (`_is_market_open`), pero este ciclo de APERTURA no: con el daemon
    # encendido escaneaba y cotizaba a las 3 a.m., tomando el `ask` de un
    # snapshot rancio y mandando una orden `limit` + `day` contra un precio que
    # no representa el libro. Va PRIMERO para no gastar una llamada a MCP ni
    # tocar el estado de pausas cuando no se puede operar de todos modos.
    if not is_market_open(now):
        return {"skipped": "market_closed"}

    # Limite 0: pausas persistentes. Se consultan ANTES de cualquier llamada a
    # Alpaca. El estado en memoria (`options_autopilot_state`) se pierde al
    # reiniciar el proceso, así que la fuente de verdad es el DailyLossTracker.
    day_key = date.today().isoformat()
    pauses = huarizo_agent.risk_tracker.pause_state(day_key)
    if pauses["safety_paused"]:
        return {"skipped": "safety_pause", "reason": pauses["safety_reason"]}
    if pauses["daily_loss_paused"]:
        return {"skipped": "daily_loss_pause", "reason": pauses["daily_reason"]}

    # Limite 1: maximo de posiciones abiertas (open + closing cuentan)
    open_now = [e for e in journal_mod.load_journal()
                if e.get("status") in ("open", "closing")]
    if len(open_now) >= 3:
        return {"skipped": "max_positions"}

    # Herramienta oficial (MCP/CLI): puerta ADICIONAL, no un reemplazo del
    # bloque de abajo. Si la capa esta activa y no puede entregar un snapshot
    # utilizable — incluida una cuenta bloqueada — no se opera. Sin esta capa
    # la entrada al concurso seria invalida; con ella mal usada, la cuenta se
    # revienta, asi que tambien es fail-closed.
    tool_snapshot = None
    if ALPACA_TOOLS.method != "none":
        try:
            tool_snapshot = ALPACA_TOOLS.get_account_snapshot()
        except ToolUnavailable as e:
            reason = f"mcp_cli_unavailable: {e}"
            huarizo_agent.risk_tracker.safety_pause(reason)
            with options_lock:
                options_autopilot_state["paused_reason"] = reason
            return {"skipped": reason}

    # Limite 2: pausa por perdida diaria (equity vs last_equity)
    try:
        acct = alpaca_service.get_account()
        if tool_snapshot:
            # La herramienta manda cuando esta activa. Se hace merge en vez de
            # reemplazo para no perder las llaves que el risk gate espera.
            acct = dict(acct or {})
            for key in ("equity", "cash", "buying_power", "last_equity",
                        "options_buying_power"):
                if key in tool_snapshot:
                    acct[key] = tool_snapshot[key]
        last_eq = float(acct.get("last_equity", 0) or 0)
        equity = float(acct.get("equity", 0) or 0)
    except Exception as e:
        # Fail-closed y persistente: no podemos medir el riesgo, así que la
        # cuenta queda en safety pause hasta que se levante a mano.
        huarizo_agent.risk_tracker.safety_pause(f"account_error: {e}")
        with options_lock:
            options_autopilot_state["paused_reason"] = f"account_error: {e}"
        return {"skipped": f"account_error: {e}"}
    if last_eq > 0:
        daily_pl_pct = (equity - last_eq) / last_eq * 100.0
        if daily_pl_pct <= -5.0:
            reason = f"daily loss {daily_pl_pct:.1f}%"
            # Persistente: sobrevive a un reinicio del proceso.
            huarizo_agent.risk_tracker.pause(day_key, reason)
            with options_lock:
                options_autopilot_state["paused_reason"] = reason
            return {"skipped": "daily_loss_pause", "reason": reason}
    # Libro real para el gate de riesgo. Se consulta UNA vez por ciclo porque
    # antes se pasaba `open_positions=[]` y el gate evaluaba cada candidato
    # contra una cartera vacia: `already_in_portfolio`,
    # `max_open_positions_reached`, la exposicion bruta y R6 (concentracion por
    # subyacente) quedaban ciegos. El unico freno real era el
    # `len(open_now) >= 3` de arriba, que cuenta FILAS del journal y no puede
    # ver que dos filas sean el mismo ticker — que es exactamente como el
    # 2026-09-02 entro un segundo call de SPY con el primero todavia abierto.
    #
    # Si Alpaca no responde no se pausa la cuenta (un error transitorio no
    # deberia dejar al agente fuera del concurso): se cae al journal, que
    # ademas ve las ordenes ya enviadas aunque todavia no esten filled.
    held_positions: List[Dict[str, Any]] = []
    try:
        held_positions = alpaca_service.get_positions() or []
    except Exception as e:
        print(f"[options_autopilot] get_positions failed, falling back to journal: {e}")
    if not held_positions:
        held_positions = [
            {"symbol": str(e.get("occ") or "").upper(),
             "qty": int(e.get("qty") or 0),
             "market_value": float(e.get("entry_ask") or 0) * OPTIONS_CONTRACT_MULTIPLIER
                             * int(e.get("qty") or 0)}
            for e in open_now if e.get("occ")
        ]
    # Candidatos: gainers del screener PRIMERO (mismo universo que el autopilot
    # de acciones y el que sostiene el argumento de que el agente "descubre"),
    # y detras el respaldo liquido hasta completar el techo. Ver el comentario
    # de `OPTIONS_UNIVERSE` para el por que del respaldo.
    candidates: List[str] = []
    try:
        movers = alpaca_service.get_top_movers(top=5)
        candidates = [str(m.get("symbol") or "").strip().upper()
                      for m in movers.get("gainers", [])][:5]
    except Exception as e:
        # Screener caido NO tumba el ciclo: con el respaldo alcanza para operar.
        print(f"[options_autopilot] Screener unavailable: {e}")
    candidates = [t for t in candidates if t]
    for t in OPTIONS_UNIVERSE:
        if len(candidates) >= OPTIONS_MAX_CANDIDATES:
            break
        if t not in candidates:
            candidates.append(t)
    for t in candidates:
        if not t:
            continue
        try:
            proposal = huarizo_agent.build_options_proposal(t, alpaca_service)
        except Exception as e:
            print(f"[options_autopilot] Error analyzing {t}: {e}")
            continue
        if not proposal.get("available"):
            # Se registra el MOTIVO: un agente que rechaza y no dice por que no
            # se puede auditar. Con las reglas de opciones (DTE/IV/theta/
            # estructura de plazos) el rechazo es la decision habitual.
            print(f"[options_autopilot] {t} descartado: "
                  f"{proposal.get('reason', 'sin motivo')}")
            continue
        # Confirmacion por volumen. No mueve el score: es un freno sobre la
        # entrada AUTONOMA, igual que el horario o la perdida diaria. Solo
        # bloquea volumen realmente muerto (< 0.5x su media de 20 sesiones) o
        # no medible — `volume_allows_entry` falla cerrado, asi que un feed
        # degradado detiene el ciclo en lugar de dejarlo operar a ciegas.
        vol = proposal.get("volume_confirmation") or {}
        if not volume_allows_entry(vol):
            print(f"[options_autopilot] {t} descartado: volumen "
                  f"{vol.get('level', 'unknown')} "
                  f"(ratio {vol.get('ratio')} vs media 20 sesiones)")
            continue
        contract = proposal.get("contract") or {}
        sizing = proposal.get("sizing") or {}
        occ = contract.get("occ_symbol")
        qty = int(sizing.get("qty", 0))
        ask_val = float(contract.get("ask") or 0)
        if not occ or qty <= 0 or ask_val <= 0:
            continue
        # Misma vía autorizada que el endpoint manual: ledger + idempotencia.
        # Si el gate o la reserva rechazan, se prueba el siguiente candidato.
        outcome = submit_authorized_option_order(
            occ_symbol=occ, qty=qty, ask_val=ask_val,
            direction=str(proposal.get("direction") or ""),
            account=acct, open_positions=held_positions,
        )
        if not outcome["ok"]:
            print(f"[options_autopilot] Rejected {occ}: {outcome['error']}")
            continue
        order = outcome["order"]
        try:
            journal_mod.add_entry({
                "ts_entry": datetime.now(_ET).strftime("%Y-%m-%dT%H:%M:%S"),
                "ticker": t, "occ": occ,
                "direction": proposal.get("direction"), "qty": qty,
                "entry_ask": ask_val, "order_id": order.get("id"),
                "exit_plan": proposal.get("exit_plan"), "source": "autopilot",
            })
        except Exception as je:
            # Same safety protocol as the manual order (b71aa43): if the journal
            # fails after the order was placed, cancel it so we don't leave an
            # orphan position without tracking or exit management.
            print(f"[options_autopilot] Journal failed after order {occ}: {je}")
            try:
                alpaca_service.cancel_order(order.get("id"))
            except Exception as ce:
                print(f"[options_autopilot] Could not cancel orphan order "
                      f"{order.get('id')}: {ce}")
            continue
        return {"opened": t, "occ": occ, "qty": qty}
    return {"skipped": "no_valid_candidates"}


def options_autopilot_worker():
    print(">> [OPTIONS AUTOPILOT] Worker started.")
    while True:
        time.sleep(OPTIONS_SCAN_INTERVAL_SECONDS)
        with options_lock:
            active = bool(OPTIONS_AUTOPILOT_ENV_ENABLED
                          and options_autopilot_state.get("enabled")
                          and not options_autopilot_state.get("paused_reason"))
        if not active:
            continue
        try:
            result = _run_one_options_cycle()
            print(f"[options_autopilot] Cycle: {result}")
        except Exception as e:
            print(f"[options_autopilot] Error in cycle: {e}")


if not DAEMONS_DISABLED:
    threading.Thread(target=options_autopilot_worker, daemon=True,
                     name="huarizo-options-autopilot").start()


# ── 6. EJECUCIÓN DE ÓRDENES BRACKET ──────────────────────────────────────────

@app.route("/api/orders/bracket", methods=["POST"])
def api_place_bracket_order():
    # Política options-only del hackathon: las órdenes de acciones quedan
    # fuera del flujo autónomo. Se rechaza ANTES de tocar Alpaca.
    if not agent_engine.STOCK_AUTOPILOT_ENABLED:
        return jsonify({
            "success": False,
            "error": "Equity orders disabled: autonomous flow is options-only "
                     "(set HUARIZO_STOCK_AUTOPILOT=true to re-enable)",
        }), 403

    data = request.get_json() or {}
    ticker = data.get("ticker", "").upper()
    qty = float(data.get("qty", 1.0))
    side = data.get("side", "buy").lower()
    tp = float(data.get("take_profit_price")) if data.get("take_profit_price") else None
    sl = float(data.get("stop_loss_price")) if data.get("stop_loss_price") else None

    if not ticker or qty <= 0:
        return jsonify({"success": False, "error": "Ticker and quantity required"}), 400

    try:
        order = alpaca_service.place_bracket_order(
            ticker=ticker,
            qty=qty,
            side=side,
            take_profit_price=tp,
            stop_loss_price=sl
        )
        return jsonify({"success": True, "order": order})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders", methods=["GET"])
def api_get_orders():
    status = request.args.get("status", "open")
    try:
        orders = alpaca_service.get_orders(status=status)
        return jsonify({"success": True, "orders": orders})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/<order_id>", methods=["DELETE"])
def api_cancel_order(order_id):
    try:
        ok = alpaca_service.cancel_order(order_id)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5050))
    print(f">> Starting Huarizo AI at http://localhost:{port} (Isolated Mode)")
    app.run(host="0.0.0.0", port=port, debug=False)

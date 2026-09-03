"""Tests de regresión del incidente del 2026-08-31.

Ese día `run_autonomous_autopilot` envió 180 órdenes en 18 segundos sobre tres
símbolos (NVDA 169, AAPL 166, AMZN 165), dejó la cuenta con cash -263.703 USD y
apalancada en margen. Causas: no verificaba posiciones abiertas, no miraba
buying power, no tenía pausa por pérdida diaria y no usaba llave de idempotencia.

Estos tests reproducen las condiciones exactas de esa cuenta y comprueban que el
gate de riesgo bloquea cada una de esas vías.
"""
import os
import sys
from datetime import date

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_engine
from agent_engine import HuarizoAgent


@pytest.fixture(autouse=True)
def stock_autopilot_enabled(monkeypatch):
    """Este archivo prueba el gate de riesgo DEL autopilot de acciones.

    Desde el 2026-09-01 ese flujo queda apagado por defecto (política
    options-only, ver `test_options_only.py`). Aquí se habilita de forma
    explícita porque lo que se verifica es que, SI alguien lo reactiva con
    HUARIZO_STOCK_AUTOPILOT=true, el gate sigue conteniendo el runaway del
    2026-08-31. Sin este fixture los tests quedarían vacíos.
    """
    monkeypatch.setattr(agent_engine, "STOCK_AUTOPILOT_ENABLED", True)


# Cuenta real del incidente, tal como la devolvía Alpaca aquel día.
INCIDENT_POSITIONS = [
    {"symbol": "AAPL", "qty": "372", "avg_entry_price": "318.95", "market_value": "117496.20"},
    {"symbol": "AMZN", "qty": "427", "avg_entry_price": "262.87", "market_value": "109952.50"},
    {"symbol": "NVDA", "qty": "559", "avg_entry_price": "217.99", "market_value": "121812.98"},
]

INCIDENT_ACCOUNT = {
    "equity": 185761.47,
    "cash": -263703.93,
    "buying_power": 123492.14,
}


def canned_analysis(symbol="NVDA", price=200.0, signal="STRONG BUY", score=90):
    """Análisis sintético con la forma exacta que consume el autopilot."""
    return {
        "available": True,
        "symbol": symbol,
        "price": price,
        "signal": signal,
        "confidence_score": score,
        "winning_strategy": {"name": "Trend Following", "win_rate": 65.0, "profit_factor": 1.8},
        "bracket_order_plan": {
            "entry_price": price,
            "take_profit_price": price * 1.06,
            "stop_loss_price": price * 0.96,
        },
    }


class FakeService:
    """Doble de prueba del AlpacaService. Nunca toca la red."""

    def __init__(self, account=None, positions=None, account_error=False):
        self.account = account
        self.positions = positions if positions is not None else []
        self.account_error = account_error
        self.placed_orders = []
        self.get_stock_bars = lambda sym, days=60: pd.DataFrame()

    def get_account(self):
        if self.account_error:
            raise RuntimeError("Alpaca 503")
        return self.account

    def get_positions(self):
        return list(self.positions)

    def get_top_movers(self, top=6):
        return {"gainers": []}

    def get_most_active(self, top=6):
        return []

    def place_bracket_order(self, ticker, qty, side="buy", take_profit_price=None,
                            stop_loss_price=None, client_order_id=None, **kwargs):
        self.placed_orders.append({
            "ticker": ticker, "qty": qty, "side": side,
            "client_order_id": client_order_id,
        })
        return {"id": f"SIM-{ticker}-{len(self.placed_orders)}", "status": "accepted"}


def seed_day_start(agent, equity):
    """Fija el equity de apertura del día.

    El tracker establece la línea base en la PRIMERA observación del día, así
    que para simular una pérdida intradía hay que sembrarla explícitamente.
    """
    return agent.risk_tracker.day_start_equity(date.today().isoformat(), equity)


def make_agent(monkeypatch, tmp_path, candidates, limits=None):
    agent = HuarizoAgent(risk_state_path=str(tmp_path / "risk_state.json"))
    if limits:
        agent.risk_limits.update(limits)

    seq = iter(candidates)

    def fake_analyze(self, ticker, service):
        return canned_analysis(symbol=ticker)

    monkeypatch.setattr(HuarizoAgent, "analyze", fake_analyze)
    return agent


# ── El incidente: símbolo ya en cartera ───────────────────────────────────────

def test_no_recompra_simbolos_que_ya_estan_en_cartera(monkeypatch, tmp_path):
    """AAPL recibió 166 órdenes de compra teniendo ya 372 acciones."""
    service = FakeService(account=INCIDENT_ACCOUNT, positions=INCIDENT_POSITIONS)
    agent = make_agent(monkeypatch, tmp_path, ["AAPL", "AMZN", "NVDA"])

    res = agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=3,
                                         candidate_tickers=["AAPL", "AMZN", "NVDA"])

    assert res["orders_executed_count"] == 0
    assert service.placed_orders == []
    assert res["blocked_orders_count"] == 3
    assert all(b["reason"] == "already_in_portfolio" for b in res["blocked_orders"])


def test_cinco_ciclos_seguidos_no_acumulan_ordenes(monkeypatch, tmp_path):
    """El runaway fue acumulativo: ciclo tras ciclo sobre los mismos símbolos."""
    service = FakeService(account=INCIDENT_ACCOUNT, positions=INCIDENT_POSITIONS)
    agent = make_agent(monkeypatch, tmp_path, ["AAPL", "AMZN", "NVDA"])

    for _ in range(5):
        agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=3,
                                       candidate_tickers=["AAPL", "AMZN", "NVDA"])

    # Con el código original esto habría generado 15 órdenes (3 por ciclo).
    assert len(service.placed_orders) == 0


# ── Límite de órdenes por ciclo ───────────────────────────────────────────────

def test_respeta_max_orders_por_ciclo(monkeypatch, tmp_path):
    account = {"equity": 100000.0, "cash": 100000.0, "buying_power": 100000.0}
    service = FakeService(account=account, positions=[])
    agent = make_agent(monkeypatch, tmp_path, ["AAA", "BBB", "CCC", "DDD", "EEE"])

    res = agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=2,
                                         candidate_tickers=["AAA", "BBB", "CCC", "DDD", "EEE"])

    assert res["orders_executed_count"] == 2
    assert len(service.placed_orders) == 2


def test_no_repite_simbolo_dentro_del_mismo_ciclo(monkeypatch, tmp_path):
    """El guard de in-flight evita duplicados si el candidato aparece dos veces."""
    account = {"equity": 100000.0, "cash": 100000.0, "buying_power": 100000.0}
    service = FakeService(account=account, positions=[])
    agent = make_agent(monkeypatch, tmp_path, ["AAA", "AAA", "BBB"])

    agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=5,
                                   candidate_tickers=["AAA", "AAA", "BBB"])

    symbols = [o["ticker"] for o in service.placed_orders]
    assert len(symbols) == len(set(symbols))
    assert symbols == ["AAA", "BBB"]


# ── Idempotencia ──────────────────────────────────────────────────────────────

def test_toda_orden_lleva_client_order_id(monkeypatch, tmp_path):
    account = {"equity": 100000.0, "cash": 100000.0, "buying_power": 100000.0}
    service = FakeService(account=account, positions=[])
    agent = make_agent(monkeypatch, tmp_path, ["AAA", "BBB"])

    agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=5,
                                   candidate_tickers=["AAA", "BBB"])

    assert service.placed_orders
    for order in service.placed_orders:
        assert order["client_order_id"]
        assert len(order["client_order_id"]) <= 48


def test_client_order_ids_son_distintos_por_simbolo(monkeypatch, tmp_path):
    account = {"equity": 100000.0, "cash": 100000.0, "buying_power": 100000.0}
    service = FakeService(account=account, positions=[])
    agent = make_agent(monkeypatch, tmp_path, ["AAA", "BBB"])

    agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=5,
                                   candidate_tickers=["AAA", "BBB"])

    ids = [o["client_order_id"] for o in service.placed_orders]
    assert len(ids) == len(set(ids))


# ── Buying power ──────────────────────────────────────────────────────────────

def test_bloquea_con_buying_power_insuficiente(monkeypatch, tmp_path):
    account = {"equity": 100000.0, "cash": 500.0, "buying_power": 500.0}
    service = FakeService(account=account, positions=[])
    agent = make_agent(monkeypatch, tmp_path, ["AAA"])

    res = agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=3,
                                         candidate_tickers=["AAA"])

    assert res["orders_executed_count"] == 0
    assert res["blocked_orders"][0]["reason"] == "insufficient_buying_power"


# ── Pausa por pérdida diaria ──────────────────────────────────────────────────

def test_pausa_por_perdida_diaria_y_persiste(monkeypatch, tmp_path):
    account = {"equity": 90000.0, "cash": 90000.0, "buying_power": 90000.0}
    service = FakeService(account=account, positions=[])
    agent = make_agent(monkeypatch, tmp_path, ["AAA"])
    seed_day_start(agent, 100000.0)  # apertura en 100k, ahora en 90k -> -10%

    first = agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=3,
                                           candidate_tickers=["AAA"])
    assert first["orders_executed_count"] == 0
    assert first["blocked_orders"][0]["reason"] == "daily_loss_breach"

    # Segundo ciclo: la pausa debe sobrevivir (antes vivía solo en memoria).
    second = agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=3,
                                            candidate_tickers=["AAA"])
    assert second["paused"] is True
    assert second["reason"] == "daily_loss_pause"
    assert len(service.placed_orders) == 0


def test_pausa_persiste_incluso_en_nueva_instancia_del_agente(monkeypatch, tmp_path):
    """Reiniciar el servidor no debe borrar la pausa."""
    account = {"equity": 90000.0, "cash": 90000.0, "buying_power": 90000.0}
    service = FakeService(account=account, positions=[])

    agent1 = make_agent(monkeypatch, tmp_path, ["AAA"])
    seed_day_start(agent1, 100000.0)
    agent1.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=3,
                                    candidate_tickers=["AAA"])

    agent2 = HuarizoAgent(risk_state_path=str(tmp_path / "risk_state.json"))
    res = agent2.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=3,
                                          candidate_tickers=["AAA"])
    assert res["paused"] is True


def test_primera_observacion_del_dia_fija_la_linea_base(monkeypatch, tmp_path):
    """Sin línea base previa no hay falsa pausa: el día arranca donde se observa."""
    account = {"equity": 90000.0, "cash": 90000.0, "buying_power": 90000.0}
    service = FakeService(account=account, positions=[])
    agent = make_agent(monkeypatch, tmp_path, ["AAA"])

    res = agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=3,
                                         candidate_tickers=["AAA"])

    assert res["paused"] is False
    assert res["orders_executed_count"] == 1


# ── Fail-closed ───────────────────────────────────────────────────────────────

def test_sin_acceso_a_la_cuenta_no_se_envia_nada(monkeypatch, tmp_path):
    service = FakeService(account=INCIDENT_ACCOUNT, account_error=True)
    agent = make_agent(monkeypatch, tmp_path, ["AAA"])

    res = agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=3,
                                         candidate_tickers=["AAA"])

    assert res["success"] is False
    assert res["reason"] == "account_unavailable"
    assert res["orders_executed_count"] == 0
    assert service.placed_orders == []


def test_cuenta_sana_si_permite_operar(monkeypatch, tmp_path):
    """El gate no debe ser tan estricto que bloquee todo: camino feliz."""
    account = {"equity": 100000.0, "cash": 100000.0, "buying_power": 100000.0}
    service = FakeService(account=account, positions=[])
    agent = make_agent(monkeypatch, tmp_path, ["AAA", "BBB"])

    res = agent.run_autonomous_autopilot(service=service, auto_submit=True, max_orders=3,
                                         candidate_tickers=["AAA", "BBB"])

    assert res["orders_executed_count"] == 2
    assert res["blocked_orders_count"] == 0
    # 5% de 100.000 = 5.000 USD, precio 200 -> 25 acciones
    assert all(o["qty"] == 25 for o in service.placed_orders)


def test_auto_submit_desactivado_no_envia_ordenes(monkeypatch, tmp_path):
    account = {"equity": 100000.0, "cash": 100000.0, "buying_power": 100000.0}
    service = FakeService(account=account, positions=[])
    agent = make_agent(monkeypatch, tmp_path, ["AAA"])

    res = agent.run_autonomous_autopilot(service=service, auto_submit=False, max_orders=3,
                                         candidate_tickers=["AAA"])

    assert res["orders_executed_count"] == 0
    assert service.placed_orders == []

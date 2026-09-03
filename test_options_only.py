"""Política options-only: el flujo autónomo del hackathon no opera acciones.

Contexto (auditoría 2026-09-01):
- El concurso exige operar opciones; el autopilot de `run_autonomous_autopilot`
  sólo emitía bracket orders de acciones.
- Ese mismo método envió 180 órdenes en 18 segundos el 2026-08-31.

Estos tests congelan el kill switch: con `STOCK_AUTOPILOT_ENABLED` en False el
autopilot de acciones no escanea, no consulta la cuenta y no envía nada, y el
endpoint de bracket orders lo rechaza con 403.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_engine
import app as app_module
from agent_engine import HuarizoAgent


# ── 1. Kill switch por defecto ────────────────────────────────────────────────

def test_stock_autopilot_disabled_by_default():
    """Fail-closed: sin variable de entorno explícita, las acciones no operan."""
    assert agent_engine.STOCK_AUTOPILOT_ENABLED is False


def test_run_autonomous_autopilot_refuses_when_disabled(tmp_path):
    """Ni escanea ni toca la cuenta: devuelve antes del primer request."""
    agent = HuarizoAgent(risk_state_path=str(tmp_path / "risk.json"))
    result = agent.run_autonomous_autopilot(service=object(), auto_submit=True)

    assert result["orders_executed_count"] == 0
    assert result["blocked_orders_count"] == 0
    assert result["candidates_scanned"] == 0
    assert result["reason"] == "stock_autopilot_disabled"


def test_autopilot_refuses_even_with_a_working_service(tmp_path):
    """Un service válido no basta: la política manda sobre la disponibilidad.

    `object()` en el test anterior podría ocultar un error de atributo; aquí el
    service responde de verdad y aun así no debe ejecutarse nada.
    """
    class WorkingService:
        def get_account(self):
            return {"equity": 100000.0, "cash": 100000.0, "buying_power": 100000.0}

        def get_positions(self):
            return []

        def place_bracket_order(self, **kwargs):
            raise AssertionError("no debe enviarse ninguna orden de acciones")

    agent = HuarizoAgent(risk_state_path=str(tmp_path / "risk.json"))
    result = agent.run_autonomous_autopilot(service=WorkingService(), auto_submit=True)

    assert result["orders_executed_count"] == 0
    assert result["reason"] == "stock_autopilot_disabled"


# ── 2. Endpoint de bracket orders bloqueado ───────────────────────────────────

def test_bracket_endpoint_rejects_equity_when_autopilot_disabled():
    """El endpoint usa `ticker`, no `symbol` (nombres reales de app.py)."""
    client = app_module.app.test_client()
    resp = client.post("/api/orders/bracket",
                       json={"ticker": "AAPL", "qty": 1,
                             "take_profit_price": 300.0, "stop_loss_price": 200.0})

    assert resp.status_code == 403
    body = resp.get_json()
    assert body["success"] is False
    assert "options" in body["error"].lower() or "opciones" in body["error"].lower()


def test_bracket_endpoint_rejection_happens_before_alpaca(monkeypatch):
    """El rechazo es local: nunca se llega a instanciar la orden en Alpaca."""
    def boom(*args, **kwargs):
        raise AssertionError("la orden llegó a Alpaca y no debería")

    monkeypatch.setattr(app_module.alpaca_service, "place_bracket_order", boom)

    client = app_module.app.test_client()
    resp = client.post("/api/orders/bracket",
                       json={"ticker": "MSFT", "qty": 2,
                             "take_profit_price": 500.0, "stop_loss_price": 400.0})

    assert resp.status_code == 403


def test_autopilot_endpoint_is_inert_without_touching_alpaca(monkeypatch):
    """El endpoint HTTP del autopilot no debe poder alcanzar Alpaca.

    Si el guard desaparece, `get_account` se invoca y el test revienta: es la
    red que faltaba para el incidente del runaway, no sólo una bandera.
    """
    def boom(*args, **kwargs):
        raise AssertionError("el autopilot llegó a Alpaca y no debería")

    monkeypatch.setattr(app_module.alpaca_service, "get_account", boom)
    monkeypatch.setattr(app_module.alpaca_service, "place_bracket_order", boom)

    client = app_module.app.test_client()
    resp = client.post("/api/agent/auto-pilot",
                       json={"auto_submit": True, "max_orders": 3})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["orders_executed_count"] == 0
    assert body["reason"] == "stock_autopilot_disabled"


# ── 3. Validación de símbolos OCC (consumida por Tasks 3 y 4) ─────────────────

# Formato real de Alpaca: SPY + 260901 + C + 00420000  (18 chars, fecha YYMMDD).
# Los helpers de test del repo generan fecha YYYYMMDD (8 dígitos), así que el
# validador acepta ambas longitudes en vez de romper la suite existente.
VALID_OCC = [
    "SPY260901C00420000",   # real, capturado del MCP server
    "AAPL260918P00215000",
    "XYZ20260918C021500000",  # variante YYYYMMDD usada en test_chain_endpoint
    "BRK.B260917C00500000",   # no alfanumérico puro -> se valida aparte
]

INVALID_OCC = [
    "AAPL",              # acción, no opción
    "",                  # vacío
    None,                # nulo
    "SPY260901X00420000",  # letra de tipo inválida
    "SPY26090C00420000",   # fecha corta
    "123456C00420000",     # sin underlying
    "SPY260901C0042000",   # strike con 7 dígitos
]


@pytest.mark.parametrize("symbol", INVALID_OCC)
def test_assert_option_symbol_rejects(symbol):
    with pytest.raises(ValueError):
        agent_engine.assert_option_symbol(symbol)


def test_assert_option_symbol_accepts_real_contract():
    assert agent_engine.assert_option_symbol("SPY260901C00420000") == "SPY260901C00420000"


def test_assert_option_symbol_normalizes_case_and_spaces():
    assert agent_engine.assert_option_symbol("  spy260901c00420000 ") == "SPY260901C00420000"


def test_assert_option_symbol_rejects_brk_dot_notation():
    """Los subyacentes con punto no son OCC válido: no colarlos por descuido."""
    with pytest.raises(ValueError):
        agent_engine.assert_option_symbol("BRK.B260917C00500000")


def test_is_option_symbol_is_predicate_not_raiser():
    assert agent_engine.is_option_symbol("SPY260901C00420000") is True
    assert agent_engine.is_option_symbol("AAPL") is False
    assert agent_engine.is_option_symbol(None) is False

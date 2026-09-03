"""Test del pipeline de propuesta de opciones (analyze mockeado)."""
import os
import sys
from datetime import date as _date
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_engine import HuarizoAgent

PINNED = _date(2026, 8, 25)


def _base_analysis(signal="STRONG BUY", score=85):
    return {
        "available": True, "signal": signal, "confidence_score": score,
        "price": 215.0,
        "winning_strategy": {"name": "Trend Following Master",
                             "win_rate": 70.0, "profit_factor": 2.1},
    }


def _chain():
    return [{"occ_symbol": "AAPL260918C00220000", "type": "call", "strike": 220.0,
             "expiry": "2026-09-18", "bid": 1.44, "ask": 1.50, "delta": 0.41,
             "iv": 0.28, "volume": 1200, "open_interest": 17074}]


def _services():
    svc = MagicMock()
    svc.get_options_chain.return_value = _chain()
    svc.enrich_chain_with_oi.side_effect = lambda c, spot, n_strikes=5, **kw: c
    svc.get_account.return_value = {"equity": 100000.0}
    return svc


def test_propuesta_completa_para_senal_alcista():
    agent = HuarizoAgent(gemini_api_key="")
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal("AAPL", _services(), today=PINNED)
    assert prop["available"] is True
    assert prop["direction"] == "long_call"
    assert prop["contract"]["occ_symbol"] == "AAPL260918C00220000"
    assert prop["sizing"]["qty"] >= 1
    assert prop["exit_plan"]["tp_premium"] > prop["contract"]["ask"]
    assert prop["exit_plan"]["sl_premium"] < prop["contract"]["ask"]
    assert "headline" in prop["thesis"]
    # El embudo viaja con la propuesta: es lo que la UI pinta para que el
    # escrutinio del selector sea visible, no una conclusión a ciegas.
    stats = prop.get("filter_stats")
    assert stats, "la propuesta llego sin filter_stats y la UI no puede pintar el embudo"
    assert stats["chain_size"] > 0
    assert stats["candidates"] > 0


def test_propuesta_rechaza_senal_neutral():
    agent = HuarizoAgent(gemini_api_key="")
    with patch.object(agent, "analyze", return_value=_base_analysis(signal="HOLD")):
        prop = agent.build_options_proposal("AAPL", _services(), today=PINNED)
    assert prop["available"] is False
    assert "reason" in prop


def test_propuesta_bajista_genera_long_put():
    agent = HuarizoAgent(gemini_api_key="")
    chain = [{"occ_symbol": "AAPL260918P00200000", "type": "put", "strike": 200.0,
              "expiry": "2026-09-18", "bid": 0.55, "ask": 0.60, "delta": -0.38,
              "iv": 0.31, "volume": 800, "open_interest": 5000}]
    svc = _services()
    svc.get_options_chain.return_value = chain
    with patch.object(agent, "analyze",
                      return_value=_base_analysis(signal="SELL / TAKE PROFIT")):
        prop = agent.build_options_proposal("AAPL", svc, today=PINNED)
    assert prop["available"] is True
    assert prop["direction"] == "long_put"


def test_propuesta_fail_closed_sin_cadena():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_options_chain.return_value = []
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal("AAPL", svc, today=PINNED)
    assert prop["available"] is False


def test_propuesta_fail_closed_sin_liquidez():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_options_chain.return_value = [
        {"occ_symbol": "AAPL260918C00220000", "type": "call", "strike": 220.0,
         "expiry": "2026-09-18", "bid": 0.01, "ask": 5.00, "delta": 0.40,
         "iv": 0.28, "volume": 500, "open_interest": 2000}]  # spread ~196%: solo spread violado
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal("AAPL", svc, today=PINNED)
    assert prop["available"] is False


def test_propuesta_fail_closed_sizing_cero():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_account.return_value = {"equity": 100.0}  # max prima 2 USD vs ask 150
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal("AAPL", svc, today=PINNED)
    assert prop["available"] is False


def test_rechazo_por_sizing_tambien_trae_el_embudo_de_filtros():
    """Era el único rechazo que se iba sin `filter_stats`.

    Y es el más desconcertante de todos: el contrato PASÓ los filtros de
    opciones y aun así no se opera. Sin el embudo, la UI mostraba el motivo y
    ninguna prueba de que el contrato era bueno, así que el rechazo parecía el
    juicio del selector cuando en realidad es el dimensionamiento.
    """
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_account.return_value = {"equity": 100.0}
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal("AAPL", svc, today=PINNED)

    assert prop["available"] is False
    stats = prop.get("filter_stats")
    assert stats, "el rechazo por sizing se fue sin filter_stats"
    # El embudo tiene que contar el escrutinio, no estar vacío.
    assert stats["chain_size"] > 0
    assert stats["candidates"] > 0
    # Y tiene que declarar por cuál de las dos rutas salió el contrato: con
    # griegas utilizables (`survivors`) o con la degradación declarada
    # (`greeks_degraded`). Lo que no puede es no decir nada.
    assert stats.get("survivors") or stats.get("greeks_degraded")


def test_propuesta_analyze_no_disponible():
    agent = HuarizoAgent(gemini_api_key="")
    with patch.object(agent, "analyze",
                      return_value={"available": False, "reason": "Insufficient Alpaca data"}):
        prop = agent.build_options_proposal("AAPL", _services(), today=PINNED)
    assert prop["available"] is False
    assert prop["reason"] == "Insufficient Alpaca data"


def test_propuesta_fail_closed_cuenta_excepcion():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_account.side_effect = RuntimeError("api down")
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal("AAPL", svc, today=PINNED)
    assert prop["available"] is False
    assert "0 contracts" in prop["reason"]


# ── premium income branch ──────────────────────────────────────────────────────

def _put_chain():
    return [{"occ_symbol": "AAPL260918P00300000", "type": "put", "strike": 300.0,
             "expiry": "2026-09-18", "bid": 4.00, "ask": 4.10, "delta": -0.40,
             "iv": 0.28, "volume": 1200, "open_interest": 5000}]


def test_propuesta_sin_args_premium_sigue_siendo_direccional():
    agent = HuarizoAgent(gemini_api_key="")
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal("AAPL", _services(), today=PINNED)
    assert prop["available"] is True
    assert prop["direction"] == "long_call"
    assert prop["contract"]["occ_symbol"] == "AAPL260918C00220000"


def test_csp_happy_path():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_options_chain.return_value = _put_chain()
    svc.get_account.return_value = {"equity": 100000.0, "buying_power": 74691.45}
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal(
            "AAPL", svc, today=PINNED,
            strategy_type="premium_income",
            premium_strategy="cash_secured_put",
            target_strike=300,
        )
    assert prop["available"] is True
    assert prop["strategy"] == "cash_secured_put"
    assert prop["action"] == "sell_to_open"
    assert prop["quantity"] >= 1
    assert prop["limit_price"] == 4.00
    # Decimal fraction. A value near 1.33 means the backend pre-multiplied.
    assert prop["metrics"]["premium_yield"] < 0.5
    assert prop["metrics"]["premium_yield"] == pytest.approx(400.0 / 30000.0)


def test_csp_rechaza_por_colateral():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_options_chain.return_value = _put_chain()
    svc.get_account.return_value = {"equity": 100000.0, "buying_power": 12450.0}
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal(
            "AAPL", svc, today=PINNED,
            strategy_type="premium_income",
            premium_strategy="cash_secured_put",
            target_strike=300,
        )
    assert prop["available"] is False
    assert "collateral" in prop["reason"].lower()
    assert "No liquid put" not in prop["reason"]


def test_csp_rechaza_por_liquidez_sin_consultar_buying_power():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_options_chain.return_value = [
        {"occ_symbol": "AAPL260918P00300000", "type": "put", "strike": 300.0,
         "expiry": "2026-09-18", "bid": 4.00, "ask": 6.00, "delta": -0.40,
         "iv": 0.28, "volume": 1200, "open_interest": 5000},
    ]
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal(
            "AAPL", svc, today=PINNED,
            strategy_type="premium_income",
            premium_strategy="cash_secured_put",
            target_strike=300,
        )
    assert prop["available"] is False
    assert "No liquid put" in prop["reason"]
    svc.get_account.assert_not_called()


def test_covered_call_happy_path():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_stock_position.return_value = 250.0
    svc.get_open_short_call_contracts.return_value = 0
    with patch.object(agent, "analyze", return_value=_base_analysis(signal="HOLD")):
        prop = agent.build_options_proposal(
            "AAPL", svc, today=PINNED,
            strategy_type="premium_income",
            premium_strategy="covered_call",
        )
    assert prop["available"] is True
    assert prop["quantity"] == 2
    assert prop["action"] == "sell_to_open"
    assert prop["strategy"] == "covered_call"


def test_covered_call_acciones_insuficientes():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_stock_position.return_value = 50.0
    svc.get_open_short_call_contracts.return_value = 0
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal(
            "AAPL", svc, today=PINNED,
            strategy_type="premium_income",
            premium_strategy="covered_call",
        )
    assert prop["available"] is False
    assert "share" in prop["reason"].lower()


def test_covered_call_neto_de_calls_cortos():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_stock_position.return_value = 250.0
    svc.get_open_short_call_contracts.return_value = 1
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal(
            "AAPL", svc, today=PINNED,
            strategy_type="premium_income",
            premium_strategy="covered_call",
        )
    assert prop["available"] is True
    assert prop["quantity"] == 1


def test_covered_call_cobertura_desconocida():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_stock_position.return_value = 250.0
    svc.get_open_short_call_contracts.return_value = None
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal(
            "AAPL", svc, today=PINNED,
            strategy_type="premium_income",
            premium_strategy="covered_call",
        )
    assert prop["available"] is False
    assert "coverage" in prop["reason"].lower() or "option positions" in prop["reason"].lower()


def test_csp_target_strike_invalido_no_toca_la_cadena():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal(
            "AAPL", svc, today=PINNED,
            strategy_type="premium_income",
            premium_strategy="cash_secured_put",
            target_strike="abc",
        )
    assert prop["available"] is False
    assert "target strike" in prop["reason"].lower()
    svc.get_options_chain.assert_not_called()


def test_premium_estrategia_desconocida_no_toca_la_cadena():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal(
            "AAPL", svc, today=PINNED,
            strategy_type="premium_income",
            premium_strategy="iron_condor",
            target_strike=300,
        )
    assert prop["available"] is False
    svc.get_options_chain.assert_not_called()


def test_csp_payload_expone_spot():
    agent = HuarizoAgent(gemini_api_key="")
    svc = _services()
    svc.get_options_chain.return_value = _put_chain()
    svc.get_account.return_value = {"equity": 100000.0, "buying_power": 74691.45}
    with patch.object(agent, "analyze", return_value=_base_analysis()):
        prop = agent.build_options_proposal(
            "AAPL", svc, today=PINNED,
            strategy_type="premium_income",
            premium_strategy="cash_secured_put",
            target_strike=300,
        )
    assert prop["available"] is True
    assert prop["spot"] == 215.0  # base price del analyze mockeado

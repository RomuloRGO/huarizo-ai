"""Tests del exit manager: triggers puros + procesamiento con mocks."""
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exit_manager import evaluate_exit, process_open_positions, _is_market_open, _time_trigger


def _entry(**over):
    base = {
        "id": 1, "occ": "AAPL260918C00220000", "qty": 2,
        "entry_ask": 2.00, "status": "open",
        "exit_plan": {"tp_premium": 3.00, "sl_premium": 1.40, "min_exit_date": "2026-09-13"},
    }
    base.update(over)
    return base


def test_trigger_tp_cuando_mid_supera_take_profit():
    assert evaluate_exit(mid=3.05, entry=_entry(), today=None) == "tp"


def test_trigger_sl_cuando_mid_baja_stop():
    assert evaluate_exit(mid=1.39, entry=_entry(), today=None) == "sl"


def test_trigger_time_stop_por_fecha():
    assert evaluate_exit(mid=2.00, entry=_entry(), today=date(2026, 9, 14)) == "time"


def test_sin_trigger_retorna_none():
    assert evaluate_exit(mid=2.00, entry=_entry(), today=date(2026, 8, 26)) is None


def test_tp_tiene_prioridad_sobre_time_stop():
    assert evaluate_exit(mid=3.10, entry=_entry(), today=date(2026, 9, 14)) == "tp"


def test_mid_invalido_retorna_none():
    assert evaluate_exit(mid="n/a", entry=_entry(), today=None) is None
    assert evaluate_exit(mid=float("nan"), entry=_entry(), today=None) is None
    assert evaluate_exit(mid=None, entry=_entry(), today=None) is None


def _fake_service(quotes):
    svc = MagicMock()
    svc.get_option_quote.return_value = quotes
    svc.close_option_position.return_value = {"id": "close-order-1"}
    return svc


@pytest.fixture
def close_env(tmp_path):
    """Journal y ledger temporales; el daemon nunca toca archivos reales."""
    import journal
    from execution_ledger import ExecutionLedger

    jpath = str(tmp_path / "journal.json")
    entry = journal.add_entry({"occ": "AAPL260918C00220000", "qty": 2,
                               "entry_ask": 2.00, "status": "open",
                               "exit_plan": {"tp_premium": 3.00,
                                             "sl_premium": 1.40,
                                             "min_exit_date": "2026-09-13"}},
                              path=jpath)
    led = ExecutionLedger(path=str(tmp_path / "ledger.json"), journal_path=jpath)
    return led, entry["id"], jpath


@patch("exit_manager.open_entries")
def test_proceso_dispara_close_y_marca_closing(mock_open, close_env):
    ledger, jid, jpath = close_env
    import journal
    mock_open.return_value = journal.load_journal(path=jpath)
    svc = _fake_service({"AAPL260918C00220000": 3.10})

    results = process_open_positions(svc, today=None, ledger=ledger)

    svc.close_option_position.assert_called_once_with("AAPL260918C00220000", 2)
    assert results[0]["action"] == "close_submitted"
    # Queda en `closing` a la espera del fill: el P&L se liquida después.
    final = next(e for e in journal.load_journal(path=jpath)
                 if int(e.get("id")) == jid)
    assert final["status"] == "closing"
    assert final["reason"] == "tp"
    assert final["close_order_id"] == "close-order-1"


@patch("exit_manager.open_entries")
def test_proceso_no_hace_nada_sin_trigger(mock_open, close_env):
    ledger, jid, jpath = close_env
    import journal
    mock_open.return_value = journal.load_journal(path=jpath)
    svc = _fake_service({"AAPL260918C00220000": 2.00})

    results = process_open_positions(svc, today=None, ledger=ledger)

    svc.close_option_position.assert_not_called()
    assert results == []


@patch("exit_manager.open_entries")
def test_proceso_tolerancia_si_close_falla(mock_open, close_env):
    """Si la orden falla se libera la reclamación y la entrada vuelve a `open`."""
    ledger, jid, jpath = close_env
    import journal
    mock_open.return_value = journal.load_journal(path=jpath)
    svc = _fake_service({"AAPL260918C00220000": 3.10})
    svc.close_option_position.side_effect = ValueError("Sin posicion abierta")

    results = process_open_positions(svc, today=None, ledger=ledger)

    assert results == []  # no explota; siguiente ciclo reintenta
    final = next(e for e in journal.load_journal(path=jpath)
                 if int(e.get("id")) == jid)
    assert final["status"] == "open"  # nuevamente elegible, no quedó trabada


@patch("exit_manager.open_entries")
def test_dos_cierres_simultaneos_generan_una_sola_orden(mock_open, close_env):
    """El TOCTOU: dos ciclos concurrentes sólo pueden enviar un cierre."""
    import threading

    ledger, jid, jpath = close_env
    import journal
    mock_open.return_value = journal.load_journal(path=jpath)
    svc = _fake_service({"AAPL260918C00220000": 3.10})

    barrier = threading.Barrier(4)
    errors = []

    def worker():
        try:
            barrier.wait()
            process_open_positions(svc, today=None, ledger=ledger)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert svc.close_option_position.call_count == 1


def test_market_open_weekday_en_horario():
    from datetime import datetime as _dt
    # Miercoles 10:30 ET (inventado con zona fija via fold-safe constructor)
    dt = _dt(2026, 8, 26, 10, 30, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
    assert _is_market_open(dt) is True


def test_market_open_fuera_de_horario_y_finde():
    from datetime import datetime as _dt
    zi = __import__("zoneinfo").ZoneInfo("America/New_York")
    assert _is_market_open(_dt(2026, 8, 26, 8, 0, tzinfo=zi)) is False      # antes de apertura
    assert _is_market_open(_dt(2026, 8, 26, 16, 30, tzinfo=zi)) is False    # despues de cierre
    assert _is_market_open(_dt(2026, 8, 22, 11, 0, tzinfo=zi)) is False     # sabado


@patch("exit_manager.update_entry")
@patch("exit_manager.load_journal")
def test_finalize_filled_registra_pnl_y_cierra(mock_load, mock_upd):
    from exit_manager import ExitManager
    entry = {"id": 1, "occ": "AAPL260918C00220000", "qty": 2, "entry_ask": 2.00,
             "status": "closing", "close_order_id": "ord1", "last_mid": 3.05}
    mock_load.return_value = [entry]
    svc = MagicMock()
    svc.get_order.return_value = {"status": "filled", "filled_avg_price": "3.10"}
    em = ExitManager(svc)
    em._finalize_filled_closes()
    upd = mock_upd.call_args[0]
    assert upd[0] == 1
    assert upd[1]["status"] == "closed"
    assert upd[1]["exit_mid"] == 3.10
    assert upd[1]["pnl_usd"] == pytest.approx((3.10 - 2.00) * 100 * 2)


@patch("exit_manager.update_entry")
@patch("exit_manager.load_journal")
def test_finalize_usa_last_mid_si_no_hay_fill_price(mock_load, mock_upd):
    from exit_manager import ExitManager
    entry = {"id": 2, "occ": "X", "qty": 1, "entry_ask": 1.00,
             "status": "closing", "close_order_id": "o2", "last_mid": 0.80}
    mock_load.return_value = [entry]
    svc = MagicMock()
    svc.get_order.return_value = {"status": "filled"}  # sin filled_avg_price
    ExitManager(svc)._finalize_filled_closes()
    assert mock_upd.call_args[0][1]["exit_mid"] == 0.80


@patch("exit_manager.update_entry")
@patch("exit_manager.load_journal")
def test_finalize_orden_cancelada_revierte_a_open(mock_load, mock_upd):
    from exit_manager import ExitManager
    entry = {"id": 3, "occ": "X", "qty": 1, "entry_ask": 1.00,
             "status": "closing", "close_order_id": "o3"}
    mock_load.return_value = [entry]
    svc = MagicMock()
    svc.get_order.return_value = {"status": "canceled"}
    ExitManager(svc)._finalize_filled_closes()
    upd = mock_upd.call_args[0][1]
    assert upd["status"] == "open" and upd["close_order_id"] is None


def test_plan_malformado_no_explota_ni_envenena():
    mala = {"occ_symbol": "X", "occ": "X", "qty": 1, "entry_ask": 2.00,
            "exit_plan": {"tp_premium": "oops", "sl_premium": None}}
    assert evaluate_exit(mid=99.0, entry=mala, today=None) is None


def test_time_stop_dispara_sin_quote():
    # Sin mid disponible, el time-stop igual debe disparar
    entry = {"occ": "X", "qty": 1, "exit_plan": {"min_exit_date": "2026-01-01"}}
    assert _time_trigger(entry, date(2026, 8, 26)) == "time"
    assert _time_trigger({"exit_plan": {}}, date(2026, 8, 26)) is None
    assert _time_trigger({"exit_plan": {"min_exit_date": "basura"}}, date(2026, 8, 26)) is None


def test_limites_exactos_tp_y_sl():
    base = {"occ": "X", "qty": 1, "entry_ask": 2.00,
            "exit_plan": {"tp_premium": 3.00, "sl_premium": 1.40}}
    assert evaluate_exit(mid=3.00, entry=base, today=None) == "tp"   # >= tp
    assert evaluate_exit(mid=1.40, entry=base, today=None) == "sl"   # <= sl

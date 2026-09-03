import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import journal
from execution_ledger import ExecutionLedger


def _ledger(tmp_path):
    return ExecutionLedger(path=str(tmp_path / "ledger.json"),
                           journal_path=str(tmp_path / "journal.json"))


def test_authorize_requires_approved_verdict(tmp_path):
    led = _ledger(tmp_path)
    out = led.authorize({"cycle_id": "c1", "occ_symbol": "AAPL260918C00230000"},
                        {"allowed": False, "reason": "max_open_positions_reached"})
    assert out["status"] == "rejected"
    assert out["authorization_id"] is None


def test_duplicate_reserve_returns_same_record(tmp_path):
    led = _ledger(tmp_path)
    auth = led.authorize({"cycle_id": "c1", "occ_symbol": "AAPL260918C00230000"},
                         {"allowed": True, "reason": "approved"})
    r1 = led.reserve(auth["authorization_id"], "AAPL260918C00230000", "buy", 1,
                     "huarizo-aapl-buy-1")
    r2 = led.reserve(auth["authorization_id"], "AAPL260918C00230000", "buy", 1,
                     "huarizo-aapl-buy-1")
    assert r1["client_order_id"] == r2["client_order_id"]
    assert r2["duplicate"] is True


def test_concurrent_reserve_single_winner(tmp_path):
    led = _ledger(tmp_path)
    auth = led.authorize({"cycle_id": "c1", "occ_symbol": "AAPL260918C00230000"},
                         {"allowed": True, "reason": "approved"})
    results = []
    barrier = threading.Barrier(8)

    def worker(i):
        barrier.wait()
        results.append(led.reserve(auth["authorization_id"],
                                   "AAPL260918C00230000", "buy", 1,
                                   "huarizo-aapl-buy-1"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r["duplicate"] is False) == 1


def test_authorization_consumed_once(tmp_path):
    led = _ledger(tmp_path)
    auth = led.authorize({"cycle_id": "c1", "occ_symbol": "AAPL260918C00230000"},
                         {"allowed": True, "reason": "approved"})
    led.reserve(auth["authorization_id"], "AAPL260918C00230000", "buy", 1, "k1")
    led.mark_submitted("k1", "alpaca-order-1")
    second = led.authorize({"cycle_id": "c1", "occ_symbol": "AAPL260918C00230000"},
                           {"allowed": True, "reason": "approved"})
    assert second["status"] == "rejected"
    assert second["reason"] == "authorization_already_consumed"


def test_authorize_rejects_garbage_that_a_length_check_would_pass(tmp_path):
    """El chequeo original era `len(occ) >= 15`, no una validación de forma.

    "NOTANOPTION12345" tiene 16 caracteres y habría quedado autorizada. El
    ledger es la última barrera antes de Alpaca, así que valida formato OCC.
    """
    led = _ledger(tmp_path)
    out = led.authorize({"cycle_id": "c1", "occ_symbol": "NOTANOPTION12345"},
                        {"allowed": True, "reason": "approved"})
    assert out["status"] == "rejected"
    assert out["reason"] == "not_an_option_symbol"


def test_authorize_rejects_equity_ticker(tmp_path):
    """Un ticker de acción no debe poder obtener una autorización de opción."""
    led = _ledger(tmp_path)
    out = led.authorize({"cycle_id": "c1", "occ_symbol": "AAPL"},
                        {"allowed": True, "reason": "approved"})
    assert out["status"] == "rejected"
    assert out["reason"] == "not_an_option_symbol"


@pytest.fixture
def close_env(tmp_path):
    jpath = str(tmp_path / "journal.json")
    lpath = str(tmp_path / "ledger.json")
    entry = journal.add_entry({"occ": "AAPL260918C00230000", "qty": 1,
                               "status": "open"}, path=jpath)
    return ExecutionLedger(path=lpath, journal_path=jpath), entry["id"], jpath


def test_begin_close_single_winner(close_env):
    led, jid, jpath = close_env
    results = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        results.append(led.begin_close(jid, "test"))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r["claimed"] is True) == 1


def test_second_close_is_refused(close_env):
    led, jid, jpath = close_env
    first = led.begin_close(jid, "manual")
    second = led.begin_close(jid, "time_stop")
    assert first["claimed"] is True
    assert second["claimed"] is False
    assert second["reason"] == "not_open"


def test_unknown_journal_id_refused(close_env):
    led, jid, jpath = close_env
    out = led.begin_close(999999, "manual")
    assert out["claimed"] is False
    assert out["reason"] == "not_found"


def test_begin_close_uses_configured_journal_path(tmp_path):
    jpath = str(tmp_path / "journal.json")
    led = ExecutionLedger(path=str(tmp_path / "ledger.json"), journal_path=jpath)
    entry = journal.add_entry({"occ": "AAPL260918C00230000", "qty": 1,
                               "status": "open"}, path=jpath)
    out = led.begin_close(entry["id"], "manual")
    assert out["claimed"] is True
    assert journal.load_journal(path=jpath)[0]["status"] == "closing"

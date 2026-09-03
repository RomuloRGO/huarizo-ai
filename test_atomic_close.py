"""Cierre atómico: el cierre manual y el exit manager no pueden duplicarse.

Bug original (TOCTOU): ambos caminos hacían

    if entry["status"] == "open":      <- miran
        update_entry(..., "closing")   <- marcan
        close_option_position(...)     <- envían

La ventana entre "mirar" y "marcar" permitía que el daemon y un clic en la UI
enviaran DOS órdenes de cierre del mismo contrato. Ahora la reclamación viaja
por el execution ledger: el primero gana, el resto recibe `claimed=False`.
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module
import journal
from execution_ledger import ExecutionLedger

OCC = "AAPL260918C00220000"

# `app_module.journal_mod` ES el módulo `journal`, así que parchear
# `app_module.journal_mod.load_journal` reemplaza la función que el propio
# parche necesita invocar. Se captura la original una vez, antes de parchear.
_ORIG_LOAD_JOURNAL = journal.load_journal


@pytest.fixture
def close_env(tmp_path, monkeypatch):
    jpath = str(tmp_path / "journal.json")
    entry = journal.add_entry({"occ": OCC, "qty": 2, "entry_ask": 2.00,
                               "status": "open"}, path=jpath)

    monkeypatch.setattr(app_module, "exec_ledger",
                        ExecutionLedger(path=str(tmp_path / "ledger.json"),
                                        journal_path=jpath))
    # El endpoint lee el journal por la ruta por defecto; se redirige al temporal
    # para no tocar nunca el journal real de la app.
    monkeypatch.setattr(journal, "load_journal",
                        lambda *a, **k: _ORIG_LOAD_JOURNAL(path=jpath))

    calls = []

    def fake_close(occ_symbol, qty=None, **kw):
        calls.append({"occ": occ_symbol, "qty": qty})
        return {"id": f"SIM-CLOSE-{len(calls)}", "status": "accepted"}

    monkeypatch.setattr(app_module.alpaca_service, "close_option_position",
                        fake_close)
    return calls, entry["id"], jpath


def test_cierre_manual_unico(close_env):
    calls, jid, _ = close_env
    client = app_module.app.test_client()

    r = client.post(f"/api/options/close/{jid}")

    assert r.status_code == 200
    assert len(calls) == 1


def test_segundo_cierre_manual_es_rechazado(close_env):
    calls, jid, _ = close_env
    client = app_module.app.test_client()

    r1 = client.post(f"/api/options/close/{jid}")
    r2 = client.post(f"/api/options/close/{jid}")

    assert r1.status_code == 200
    assert r2.status_code == 409
    assert len(calls) == 1  # una sola orden real


def test_cierres_manuales_concurrentes_generan_una_sola_orden(close_env):
    """El clic doble (o dos pestañas) sólo puede cerrar una vez."""
    calls, jid, _ = close_env

    results = []
    barrier = threading.Barrier(6)

    def worker():
        client = app_module.app.test_client()
        barrier.wait()
        results.append(client.post(f"/api/options/close/{jid}").status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1
    assert results.count(409) == 5
    assert len(calls) == 1


def test_cierre_fallido_devuelve_la_entrada_a_open(close_env, monkeypatch):
    """Si Alpaca rechaza el cierre, la entrada vuelve a ser elegible."""
    calls, jid, jpath = close_env

    def boom(occ_symbol, qty=None, **kw):
        raise ValueError("Sin posicion abierta")

    monkeypatch.setattr(app_module.alpaca_service, "close_option_position", boom)
    client = app_module.app.test_client()

    r = client.post(f"/api/options/close/{jid}")

    assert r.status_code == 400
    entry = next(e for e in journal.load_journal(path=jpath)
                 if int(e.get("id")) == jid)
    assert entry["status"] == "open"  # reintento posible, no quedó en "closing"


def test_cierre_de_entrada_inexistente(close_env):
    calls, _, _ = close_env
    client = app_module.app.test_client()

    r = client.post("/api/options/close/999999")

    assert r.status_code == 404
    assert calls == []

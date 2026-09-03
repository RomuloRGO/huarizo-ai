"""El endpoint /api/options/order debe pasar por el execution ledger.

Brecha encontrada en Task 3: `ExecutionLedger` existía y estaba testeado, y
`place_option_order` ya aceptaba `client_order_id`, pero NADIE instanciaba el
ledger. El endpoint enviaba órdenes sin autorización, sin reserva y sin llave
de idempotencia: dos POST idénticos creaban dos órdenes reales, que es la
misma clase de falla que produjo las 180 órdenes del 2026-08-31.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module
from execution_ledger import ExecutionLedger

OCC = "AAPL260918C00220000"


@pytest.fixture
def order_env(tmp_path, monkeypatch):
    """App con ledger temporal y Alpaca simulado. Nunca toca la red."""
    monkeypatch.setattr(
        app_module, "exec_ledger",
        ExecutionLedger(path=str(tmp_path / "ledger.json"),
                        journal_path=str(tmp_path / "journal.json")),
    )

    calls = []

    def fake_place(occ_symbol, qty=None, action=None, limit_price=None,
                   time_in_force=None, client_order_id=None, **kw):
        calls.append({"occ": occ_symbol, "qty": qty, "limit_price": limit_price,
                      "client_order_id": client_order_id})
        return {"id": f"SIM-{len(calls)}", "status": "accepted"}

    monkeypatch.setattr(app_module.alpaca_service, "place_option_order", fake_place)
    monkeypatch.setattr(app_module.alpaca_service, "get_account",
                        lambda: {"equity": 100000.0, "cash": 100000.0,
                                 "buying_power": 100000.0,
                                 "options_buying_power": 100000.0})
    monkeypatch.setattr(app_module.alpaca_service, "get_positions", lambda: [])
    monkeypatch.setattr(app_module.alpaca_service, "cancel_order", lambda oid: True)
    monkeypatch.setattr(app_module.journal_mod, "add_entry",
                        lambda entry, **kw: dict(entry, id=1))
    return calls


def _proposal(**over):
    p = {"ticker": "AAPL", "direction": "long",
         "contract": {"occ_symbol": OCC, "ask": 3.0},
         "sizing": {"qty": 1}, "exit_plan": {}}
    p.update(over)
    return p


# ── Validación de símbolo ─────────────────────────────────────────────────────

def test_options_order_rejects_invalid_occ(order_env):
    """Un ticker de acción no debe poder colarse como contrato."""
    client = app_module.app.test_client()
    r = client.post("/api/options/order",
                    json=_proposal(contract={"occ_symbol": "AAPL", "ask": 3.0}))
    assert r.status_code == 400
    assert order_env == []  # nunca llegó a Alpaca


# ── Camino feliz con idempotencia ─────────────────────────────────────────────

def test_options_order_reserves_and_sends_client_order_id(order_env):
    client = app_module.app.test_client()
    r = client.post("/api/options/order", json=_proposal())
    assert r.status_code == 200, r.get_json()
    assert len(order_env) == 1
    assert order_env[0]["client_order_id"]
    assert order_env[0]["client_order_id"].startswith("huarizo-")


def test_options_order_duplicate_submission_does_not_reorder(order_env):
    """El bug central: un reenvío no debe crear una segunda orden real."""
    client = app_module.app.test_client()
    r1 = client.post("/api/options/order", json=_proposal())
    r2 = client.post("/api/options/order", json=_proposal())

    assert r1.status_code == 200
    assert r2.status_code == 409
    assert len(order_env) == 1  # una sola orden real


def test_options_order_persists_reservation(order_env, tmp_path, monkeypatch):
    """La reserva sobrevive al objeto en memoria: vive en el JSON."""
    client = app_module.app.test_client()
    r = client.post("/api/options/order", json=_proposal())
    assert r.status_code == 200

    cid = order_env[0]["client_order_id"]
    fresh = ExecutionLedger(path=str(tmp_path / "ledger.json"))
    rec = fresh.get_reservation(cid)
    assert rec is not None
    assert rec["state"] == "submitted"
    assert rec["alpaca_order_id"] == "SIM-1"


# ── Gate de riesgo con multiplicador de opciones ──────────────────────────────

def test_options_order_applies_option_multiplier_to_notional(order_env):
    """1 contrato a 60 USD son 6.000 USD de exposición, no 60.

    Cada contrato representa 100 acciones. Sin el multiplicador x100 el gate
    compararía 60 USD contra un techo de 5.000 USD y dejaría pasar tamaños
    absurdos: el incidente del 31-ago fue, en el fondo, un error de escala.
    """
    client = app_module.app.test_client()
    r = client.post("/api/options/order",
                    json=_proposal(contract={"occ_symbol": OCC, "ask": 60.0}))

    body = r.get_json()
    assert r.status_code == 409
    # El gate vio 6.000 USD de nocional (1 contrato x 60 USD x 100), no 60.
    # Con 60 habría pasado todos los límites y la orden habría salido.
    assert body["risk_verdict"]["notional"] == 6000.0
    # 6.000 USD sobre 100.000 USD de equity = 6% > 5% de tope por posición.
    assert "position_size_pct_limit" in body["error"]
    assert order_env == []


def test_options_order_blocked_when_contract_already_in_portfolio(order_env, monkeypatch):
    monkeypatch.setattr(app_module.alpaca_service, "get_positions",
                        lambda: [{"symbol": OCC, "qty": "1"}])
    client = app_module.app.test_client()
    r = client.post("/api/options/order", json=_proposal())

    assert r.status_code == 409
    assert "already_in_portfolio" in r.get_json()["error"]
    assert order_env == []


def test_options_order_fails_closed_when_account_unreadable(order_env, monkeypatch):
    """Sin foto de cuenta no se opera: fail-closed, no fail-open."""
    def boom():
        raise RuntimeError("Alpaca 503")

    monkeypatch.setattr(app_module.alpaca_service, "get_account", boom)
    client = app_module.app.test_client()
    r = client.post("/api/options/order", json=_proposal())

    assert r.status_code == 503
    assert order_env == []


# ── La ventana de idempotencia tiene que avanzar ──────────────────────────────

def test_el_bucket_de_idempotencia_avanza_con_el_tiempo(order_env, monkeypatch):
    """La llave no puede quedar congelada: bloquearía el contrato para siempre.

    Encontrado con el smoke de concurso del 2026-09-02: la llave impresa era
    `huarizo-SPY26090-buy-longcall-0`, con el bucket en 0 porque la única vía
    de opciones no le pasaba `now_epoch` a `build_client_order_id`.

    La reserva del ledger se guarda con esa llave y NO caduca, así que una
    llave constante significa que el contrato queda vetado de por vida tras la
    primera orden: el bot se bloquea solo a mitad del concurso y empieza a
    escupir 409 eternamente. Las dos puntas importan: el duplicado dentro de la
    ventana se rechaza, y pasada la ventana el mismo contrato vuelve a poder
    operarse.
    """
    base = 1_700_000_000.0
    monkeypatch.setattr(app_module.time, "time", lambda: base)
    client = app_module.app.test_client()

    r1 = client.post("/api/options/order", json=_proposal())
    assert r1.status_code == 200, r1.get_json()
    first = order_env[0]["client_order_id"]
    assert not first.endswith("-0"), (
        f"bucket congelado en 0: {first}. Sin bucket la llave es constante y el "
        "contrato queda bloqueado para siempre.")

    # Misma ventana: el reenvío sigue bloqueado (la idempotencia vive).
    r2 = client.post("/api/options/order", json=_proposal())
    assert r2.status_code == 409
    assert len(order_env) == 1

    # Ventana nueva: la llave cambia y el contrato vuelve a ser operable.
    monkeypatch.setattr(
        app_module.time, "time",
        lambda: base + app_module.OPTIONS_IDEMPOTENCY_BUCKET_SECONDS)
    r3 = client.post("/api/options/order", json=_proposal())

    assert r3.status_code == 200, r3.get_json()
    assert len(order_env) == 2
    assert order_env[1]["client_order_id"] != first

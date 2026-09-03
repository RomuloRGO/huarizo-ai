"""Gate duro de `options_buying_power` en el risk gate.

Contexto: desde la Task 2 el bot es SOLO opciones, pero `evaluate_new_entry`
seguia midiendo contra `buying_power` general. En la cuenta del concurso eso es
400.000 (margen 4x sobre equity de 100.000) cuando el poder de compra de
opciones real es 100.000.

El nocional llega con el multiplicador ya aplicado (1 contrato = 100 acciones)
desde `app.py`, asi que aqui `price` es el costo real por contrato: un contrato
con ask de $3.00 llega como price=300.

Alcance real del gate (medido, no supuesto): con los limites por defecto
`max_notional_per_order` es 5.000, es decir 18 veces menor que el poder de
opciones usable (90.000). **Una orden individual no puede dispararlo nunca.**
Es defensa en profundidad: se vuelve el freno efectivo cuando
(a) alguien sube los topes por `limits`, o (b) el poder de opciones se degrada,
que es el escenario que estos tests ejercitan.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from risk_gate import evaluate_new_entry, is_option_symbol, normalize_account


# Contratos OCC validos: <subyacente><YYMMDD><C|P><strike 8 digitos>
CALL = "AAPL250919C00200000"   # strike 200
PUT = "NVDA250919P00150000"    # strike 150

ASK_3 = 300.0   # ask de $3.00 x 100 = coste real de un contrato


def _account(**over):
    acc = {
        "equity": 100000.0,
        "cash": 100000.0,
        "buying_power": 400000.0,      # margen 4x
        "options_buying_power": 100000.0,
    }
    acc.update(over)
    return acc


def _entry(account=None, symbol=CALL, qty=1, price=ASK_3, **kw):
    acc = account if account is not None else _account()
    return evaluate_new_entry(acc, [], symbol=symbol, qty=qty, price=price, **kw)


# ── Camino sano ──────────────────────────────────────────────────────────────

def test_un_contrato_se_aprueba():
    """El caso normal del concurso: 1 contrato, nocional 300."""
    res = _entry(qty=1)

    assert res["allowed"] is True, res["reason"]
    assert res["notional"] == 300.0


# ── El escenario que el gate cubre: poder de opciones degradado ───────────────

def test_poder_de_opciones_degradado_rechaza_una_orden_que_el_general_aprobaria():
    """Regresion central.

    10 contratos @ $3 = nocional 3.000:
      - 3% del equity          -> pasa el limite por posicion (5%)
      - 3.000 < 5.000          -> pasa el techo absoluto por orden
      - 3.000 < 400.000        -> pasa el poder GENERAL (margen 4x)
      - 3.000 > 1.800          -> NO cabe en el poder de opciones degradado

    Sin este gate la orden se aprobaba apoyandose en un poder de compra que
    corresponde al margen de acciones, no al de opciones.
    """
    res = _entry(account=_account(options_buying_power=2000.0), qty=10)

    assert res["allowed"] is False
    assert res["reason"] == "insufficient_options_buying_power"
    assert res["notional"] == 3000.0


def test_el_rechazo_reporta_el_poder_util_para_el_log():
    res = _entry(account=_account(options_buying_power=2000.0), qty=10)

    assert res["options_buying_power"] == 2000.0
    assert res["usable_options_buying_power"] == 1800.0   # 10% de colchon


# ── Desconocido falla cerrado ────────────────────────────────────────────────

def test_poder_de_opciones_desconocido_falla_cerrado():
    """Si no sabemos el poder de opciones, no se opera.

    Caer al poder general (400.000 por margen) seria exactamente el bug que
    este gate corrige, y hacerlo en silencio es peor: pareceria una aprobacion
    legitima.
    """
    res = _entry(account=_account(options_buying_power=None))

    assert res["allowed"] is False
    assert res["reason"] == "options_buying_power_unknown"


def test_poder_de_opciones_ausente_del_dict_falla_cerrado():
    acc = _account()
    acc.pop("options_buying_power")

    res = _entry(account=acc)

    assert res["allowed"] is False
    assert res["reason"] == "options_buying_power_unknown"


def test_poder_de_opciones_no_numerico_falla_cerrado():
    res = _entry(account=_account(options_buying_power="no-es-un-numero"))

    assert res["allowed"] is False
    assert res["reason"] == "options_buying_power_unknown"


def test_poder_de_opciones_en_string_numerico_se_acepta():
    """Alpaca manda los importes como strings: '100000' es utilizable."""
    res = _entry(account=_account(options_buying_power="100000"))

    assert res["allowed"] is True, res["reason"]


# ── El colchon se aplica igual que en el poder general ───────────────────────

def test_el_colchon_del_10_por_ciento_se_aplica():
    """2.000 con colchon del 10% -> 1.800 utiles. 2.100 no cabe."""
    res = _entry(account=_account(options_buying_power=2000.0), qty=7)  # 2.100

    assert res["allowed"] is False
    assert res["reason"] == "insufficient_options_buying_power"


def test_el_limite_exacto_del_colchon_cabe():
    """1.800 es exactamente el util: cabe, porque la condicion es '>'."""
    res = _entry(account=_account(options_buying_power=2000.0), qty=6)  # 1.800

    assert res["allowed"] is True, res["reason"]


# ── Acciones NO se ven afectadas ─────────────────────────────────────────────

def test_una_accion_no_exige_poder_de_opciones():
    """El gate es solo para opciones: una accion sin ese dato pasa."""
    acc = _account()
    acc.pop("options_buying_power")

    res = _entry(account=acc, symbol="AAPL", qty=10, price=300.0)

    assert res["allowed"] is True, res["reason"]


def test_una_accion_sigue_midiendose_contra_el_poder_general():
    """16 acciones @ $300 = 4.800: bajo el techo de 5.000, sobre... nada mas."""
    res = _entry(symbol="AAPL", qty=16, price=300.0)

    assert res["allowed"] is True, res["reason"]


# ── Orden de los checks ──────────────────────────────────────────────────────

def test_el_gate_de_opciones_no_enmascara_la_perdida_diaria():
    """La pausa diaria es mas severa y debe seguir ganando."""
    res = _entry(account=_account(options_buying_power=2000.0), qty=10,
                 day_start_equity=200000.0)

    assert res["allowed"] is False
    assert res["reason"] == "daily_loss_breach"


def test_el_gate_de_opciones_no_enmascara_el_simbolo_duplicado():
    positions = [{"symbol": CALL, "qty": 1, "market_value": 300.0}]
    res = evaluate_new_entry(_account(options_buying_power=2000.0), positions,
                             symbol=CALL, qty=10, price=ASK_3)

    assert res["allowed"] is False
    assert res["reason"] == "already_in_portfolio"


# ── Con topes mas altos el gate es el freno efectivo ─────────────────────────

def test_con_topes_altos_el_gate_de_opciones_es_el_que_frena():
    """Si alguien sube los limites, este gate pasa a ser la unica defensa."""
    acc = _account(equity=100000.0, buying_power=400000.0,
                   options_buying_power=50000.0)
    limits = {"max_notional_per_order": 200000.0, "max_position_pct": 100.0,
              "max_gross_exposure_pct": 100.0}

    res = evaluate_new_entry(acc, [], symbol=CALL, qty=200, price=ASK_3,
                             limits=limits)   # nocional 60.000

    assert res["allowed"] is False
    assert res["reason"] == "insufficient_options_buying_power"


# ── normalize_account ────────────────────────────────────────────────────────

def test_normalize_account_superficia_options_buying_power():
    acc = normalize_account(_account())

    assert acc["options_buying_power"] == 100000.0


def test_normalize_account_tolera_options_buying_power_ausente():
    """Ausente no invalida la cuenta: el gate decide si eso es bloqueante."""
    acc = normalize_account({"equity": 100000.0, "cash": 100000.0,
                             "buying_power": 400000.0})

    assert acc is not None
    assert acc["options_buying_power"] is None


# ── Ambos lados del libro ────────────────────────────────────────────────────

@pytest.mark.parametrize("symbol", [CALL, PUT])
def test_aplica_a_calls_y_a_puts(symbol):
    assert is_option_symbol(symbol)
    res = _entry(account=_account(options_buying_power=2000.0), symbol=symbol, qty=10)

    assert res["allowed"] is False
    assert res["reason"] == "insufficient_options_buying_power"

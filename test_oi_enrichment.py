"""Test del enriquecimiento OI multi-vencimiento para Options Charts."""
import os
import sys
from datetime import date
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alpaca_service import AlpacaService

TODAY = date(2026, 8, 30)
EXPIRIES = ["2026-09-18", "2026-10-16", "2026-11-20", "2026-12-18", "2027-01-15"]
STRIKES = [200.0, 210.0, 215.0, 220.0, 230.0]


def _contract(expiry, strike, typ):
    letter = "C" if typ == "call" else "P"
    occ = f"XYZ{expiry.replace('-', '')}{letter}{int(round(strike * 1000)):08d}"
    return {"occ_symbol": occ, "type": typ, "strike": strike, "expiry": expiry,
            "bid": 1.0, "ask": 1.1, "delta": 0.5, "iv": 0.3, "volume": 100,
            "open_interest": None}


def _chain(expiries=EXPIRIES, strikes=STRIKES):
    out = []
    for e in expiries:
        for k in strikes:
            out.append(_contract(e, k, "call"))
            out.append(_contract(e, k, "put"))
    return out


def _svc(oi_side_effect=None):
    svc = AlpacaService(api_key="k", secret_key="s", paper=True)
    svc.get_option_contracts_oi = MagicMock(return_value={})
    if oi_side_effect:
        svc.get_option_contracts_oi.side_effect = oi_side_effect
    svc.get_option_contract = MagicMock(return_value=None)
    return svc


def test_default_comportamiento_intacto_un_vencimiento():
    """Regresión: firma por defecto = hoy (1 vencimiento, 5 strikes cercanos)."""
    svc = _svc()
    chain = _chain()
    out = svc.enrich_chain_with_oi(chain, spot=215.0, today=TODAY)
    assert svc.get_option_contracts_oi.call_count == 1
    call = svc.get_option_contracts_oi.call_args
    assert call.args[1] == "2026-09-18"  # vencimiento más cercano
    assert len(call.args[2:]) == 2       # (strike_min, strike_max) del rango de 5
    for c in out:
        assert c["open_interest"] is None  # mapa vacío → fallback per-contract → None
    assert svc.get_option_contract.call_count == 10  # presupuesto 2*5


def test_fallback_presupuesto_por_vencimiento_en_multimodo():
    """Con expiries_limit>1 y n_strikes numérico, el presupuesto 2*n es por vencimiento."""
    svc = _svc()
    chain = _chain(expiries=EXPIRIES[:2], strikes=STRIKES[:3])
    svc.enrich_chain_with_oi(chain, spot=215.0, n_strikes=2, expiries_limit=2, today=TODAY)
    assert svc.get_option_contract.call_count == 8  # 2 vencimientos * presupuesto (2*2)
    assert svc.get_option_contracts_oi.call_count == 2


def test_expiries_limit_4_n_strikes_none_enriquece_todo():
    """Modo chart: 4 vencimientos, rango COMPLETO de strikes, 1 llamada batched c/u."""
    calls = []

    def oi_map(root, expiry, smin, smax):
        calls.append((expiry, smin, smax))
        return {}

    svc = _svc(oi_side_effect=oi_map)
    chain = _chain()
    out = svc.enrich_chain_with_oi(
        chain, spot=215.0, n_strikes=None, expiries_limit=4, today=TODAY)
    assert len(calls) == 4
    for expiry, smin, smax in calls:
        assert smin == min(STRIKES)
        assert smax == max(STRIKES)
    # El 5o vencimiento NO se enriquece
    assert "2027-01-15" not in [c[0] for c in calls]
    # Sin OI disponible en el mapa → sin fallback per-contract (presupuesto duro)
    svc.get_option_contract.assert_not_called()
    for c in out:
        assert c["open_interest"] is None


def test_oi_aplicado_cuando_el_mapa_trae_datos():
    def oi_map(root, expiry, smin, smax):
        return {f"XYZ{expiry.replace('-', '')}C{int(round(smin * 1000)):08d}": 77}

    svc = _svc(oi_side_effect=oi_map)
    chain = _chain(expiries=EXPIRIES[:1])
    out = svc.enrich_chain_with_oi(
        chain, spot=215.0, n_strikes=None, expiries_limit=1, today=TODAY)
    got = [c for c in out if c["occ_symbol"] ==
           f"XYZ{EXPIRIES[0].replace('-', '')}C{int(round(min(STRIKES) * 1000)):08d}"]
    assert got and got[0]["open_interest"] == 77
    others = [c for c in out if c["occ_symbol"] != got[0]["occ_symbol"]]
    assert all(c["open_interest"] is None for c in others)


def test_ventana_dte_respeta_expiries_limit():
    svc = _svc()
    svc.enrich_chain_with_oi(_chain(), spot=215.0, n_strikes=None,
                             expiries_limit=4, dte_min=7, dte_max=45, today=TODAY)
    expiries_called = [c.args[1] for c in svc.get_option_contracts_oi.call_args_list]
    assert expiries_called == ["2026-09-18"]

    svc2 = _svc()
    svc2.enrich_chain_with_oi(_chain(), spot=215.0, n_strikes=None,
                              expiries_limit=4, dte_min=7, dte_max=90, today=TODAY)
    expiries_called2 = [c.args[1] for c in svc2.get_option_contracts_oi.call_args_list]
    assert expiries_called2 == ["2026-09-18", "2026-10-16", "2026-11-20"]


def test_full_range_sin_root_no_hace_llamadas_http():
    """Modo chart con occ_symbol no parseable: 0 llamadas HTTP, sin crash."""
    svc = _svc()
    chain = _chain(expiries=EXPIRIES[:2], strikes=STRIKES[:2])
    for c in chain:
        c["occ_symbol"] = "FAKE"
    out = svc.enrich_chain_with_oi(chain, spot=215.0, n_strikes=None,
                                   expiries_limit=2, today=TODAY)
    svc.get_option_contracts_oi.assert_not_called()
    svc.get_option_contract.assert_not_called()
    assert all(c["open_interest"] is None for c in out)


# ── dte_sweet_min: el vencimiento del tramo dulce ─────────────────────────────
# Con `expiries_limit=1` solo se enriquece el vencimiento MAS CERCANO. Como
# `volume` viene siempre en 0 en el plan de datos gratuito, el filtro de
# liquidez del motor (vol > 0 or oi >= 100) dejaba pasar unicamente ese
# vencimiento y el selector quedaba OBLIGADO a operar ~7 DTE. Medido en vivo el
# 2026-09-02: los 9 simbolos del universo caian en 7 DTE con theta de
# 9-10% de la prima por dia. `dte_sweet_min` anade el primer vencimiento que
# alcanza el tramo dulce para que R1 y R5 tengan donde elegir.

def test_dte_sweet_min_anade_el_vencimiento_del_tramo_dulce():
    svc = _svc()
    out = svc.enrich_chain_with_oi(_chain(), spot=215.0, today=TODAY,
                                   dte_sweet_min=21)
    # 2026-09-18 (DTE 19) es el mas cercano; 2026-10-16 (DTE 47) es el primero
    # que alcanza el tramo dulce.
    assert svc.get_option_contracts_oi.call_count == 2
    llamados = {c.args[1] for c in svc.get_option_contracts_oi.call_args_list}
    assert llamados == {"2026-09-18", "2026-10-16"}
    assert len(out) == len(_chain())


def test_dte_sweet_min_no_duplica_si_el_cercano_ya_es_dulce():
    svc = _svc()
    svc.enrich_chain_with_oi(_chain(), spot=215.0, today=TODAY, dte_sweet_min=7)
    # 2026-09-18 ya tiene DTE 19 >= 7: no se anade nada, sigue siendo 1 llamada.
    assert svc.get_option_contracts_oi.call_count == 1


def test_dte_sweet_min_fuera_de_la_ventana_no_anade_nada():
    """Si ningun vencimiento de la ventana alcanza el dulce, no se inventa."""
    svc = _svc()
    svc.enrich_chain_with_oi(_chain(), spot=215.0, today=TODAY,
                             dte_min=7, dte_max=45, dte_sweet_min=21)
    # Dentro de 7..45 solo cae 2026-09-18 (DTE 19): el dulce (21) no existe.
    assert svc.get_option_contracts_oi.call_count == 1
    assert svc.get_option_contracts_oi.call_args.args[1] == "2026-09-18"


def test_dte_sweet_min_invalido_no_explota():
    svc = _svc()
    for basura in (None, "abc", 0, -5, float("nan")):
        svc.get_option_contracts_oi.reset_mock()
        svc.enrich_chain_with_oi(_chain(), spot=215.0, today=TODAY,
                                 dte_sweet_min=basura)
        assert svc.get_option_contracts_oi.call_count == 1

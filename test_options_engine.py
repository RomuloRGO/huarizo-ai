"""Tests unitarios de options_engine (funciones puras, fixtures sintéticos)."""
import os
import sys
from copy import deepcopy
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from options_engine import (
    DEFAULT_OPTIONS_CONFIG, merge_config, select_option_contract,
    size_position, build_exit_plan, resolve_direction,
    parse_target_strike, select_premium_contract,
    compute_csp_metrics, validate_csp_collateral,
    covered_call_contracts, compute_covered_call_metrics,
)

TODAY = date(2026, 8, 25)


def _mk_contract(occ, ctype, strike, expiry, bid, ask, delta, volume=1000, oi=5000):
    return {
        "occ_symbol": occ, "type": ctype, "strike": strike, "expiry": expiry,
        "bid": bid, "ask": ask, "delta": delta, "iv": 0.30,
        "volume": volume, "open_interest": oi,
    }


@pytest.fixture
def chain():
    return [
        _mk_contract("AAPL260918C00200000", "call", 200.0, "2026-09-18", 2.90, 3.00, 0.62),
        _mk_contract("AAPL260918C00220000", "call", 220.0, "2026-09-18", 1.44, 1.50, 0.41),
        _mk_contract("AAPL260918C00240000", "call", 240.0, "2026-09-18", 0.48, 0.52, 0.22),
        _mk_contract("AAPL261016C00220000", "call", 220.0, "2026-10-16", 2.40, 2.50, 0.45),
        _mk_contract("AAPL260918P00200000", "put", 200.0, "2026-09-18", 0.55, 0.60, -0.38),
        _mk_contract("AAPL260918P00180000", "put", 180.0, "2026-09-18", 0.19, 0.21, -0.21),
        _mk_contract("AAPL260918P00220000", "put", 220.0, "2026-09-18", 2.43, 2.50, -0.61),
    ]


def test_merge_config_none_devuelve_defaults():
    assert merge_config(None) == DEFAULT_OPTIONS_CONFIG


def test_merge_config_whitelist_ignora_claves_desconocidas():
    merged = merge_config({"target_delta": 0.30, "clave_maliciosa": "x"})
    assert merged["target_delta"] == 0.30
    assert "clave_maliciosa" not in merged


def test_merge_config_no_mut_defaults():
    original = deepcopy(DEFAULT_OPTIONS_CONFIG)
    merge_config({"dte_min": 1})
    assert DEFAULT_OPTIONS_CONFIG == original


def test_config_expone_claves_de_riesgo_y_retira_max_premium_pct():
    assert DEFAULT_OPTIONS_CONFIG["risk_per_trade_pct"] == 1.5
    assert DEFAULT_OPTIONS_CONFIG["max_risk_per_trade_pct"] == 3.0
    assert "max_premium_pct" not in DEFAULT_OPTIONS_CONFIG
    # clave retirada => override silenciosamente ignorado por la whitelist
    assert "max_premium_pct" not in merge_config({"max_premium_pct": 99})


def test_seleccion_long_call_delta_mas_cercana_a_target(chain):
    sel = select_option_contract(chain, 215.0, "long_call", today=TODAY)
    assert sel is not None
    # |0.41 - 0.40| es mínimo entre los calls líquidos en ventana DTE
    assert sel["occ_symbol"] == "AAPL260918C00220000"
    assert sel["dte"] == 24


def test_seleccion_long_put_delta_mas_cercana_a_target(chain):
    sel = select_option_contract(chain, 215.0, "long_put", today=TODAY)
    assert sel is not None
    assert sel["occ_symbol"] == "AAPL260918P00200000"  # |-0.38| más cerca de 0.40


def test_ventana_dte_excluye_fuera_de_rango(chain):
    chain2 = chain + [
        _mk_contract("AAPL260828C00215000", "call", 215.0, "2026-08-28", 1.00, 1.05, 0.40),  # DTE 3 < min
        _mk_contract("AAPL261023C00215000", "call", 215.0, "2026-10-23", 3.00, 3.10, 0.40),  # DTE 59 > max
    ]
    sel = select_option_contract(chain2, 215.0, "long_call", today=TODAY)
    assert sel["occ_symbol"] not in ("AAPL260828C00215000", "AAPL261023C00215000")
    assert sel["occ_symbol"] == "AAPL260918C00220000"


def test_filtro_spread_fail_closed(chain):
    # Delta exactamente en target: si el filtro de spread no existiera, este contrato ganaría.
    wide = _mk_contract("AAPL260918C00215000", "call", 215.0, "2026-09-18", 0.50, 0.80, 0.40)  # spread ~46%
    sel = select_option_contract(chain + [wide], 215.0, "long_call", today=TODAY)
    assert sel["occ_symbol"] != "AAPL260918C00215000"
    assert sel["occ_symbol"] == "AAPL260918C00220000"


def test_filtro_liquidez_sin_volumen_ni_oi(chain):
    # Delta exactamente en target: sin el filtro de liquidez, este contrato ilíquido ganaría.
    illiquid = _mk_contract("AAPL260918C00215000", "call", 215.0, "2026-09-18",
                            1.20, 1.24, 0.40, volume=0, oi=50)  # oi < 100 y sin volumen
    sel = select_option_contract(chain + [illiquid], 215.0, "long_call", today=TODAY)
    assert sel["occ_symbol"] != "AAPL260918C00215000"
    assert sel["occ_symbol"] == "AAPL260918C00220000"


def test_sin_candidatos_validos_retorna_none(chain):
    vacia = [_mk_contract("AAPL260918C00200000", "call", 200.0, "2026-09-18",
                          0.01, 5.00, 0.99, volume=0, oi=1)]  # spread enorme
    assert select_option_contract(vacia, 215.0, "long_call", today=TODAY) is None


def test_direccion_invalida_retorna_none(chain):
    assert select_option_contract(chain, 215.0, "butterfly", today=TODAY) is None


def test_fallback_atm_sin_deltas_validas(chain):
    sin_delta = [
        _mk_contract("AAPL260918C00210000", "call", 210.0, "2026-09-18", 1.20, 1.24, None),
        _mk_contract("AAPL260918C00230000", "call", 230.0, "2026-09-18", 1.00, 1.04, None),
    ]
    sel = select_option_contract(sin_delta, 214.0, "long_call", today=TODAY)
    assert sel is not None
    assert sel["occ_symbol"] == "AAPL260918C00210000"  # strike mas cercano al spot


def test_spot_invalido_retorna_none(chain):
    assert select_option_contract(chain, 0, "long_call", today=TODAY) is None
    assert select_option_contract(chain, None, "long_call", today=TODAY) is None


def test_bordes_dte_inclusivos(chain):
    borde_min = _mk_contract("AAPL260901C00215000", "call", 215.0, "2026-09-01", 1.20, 1.24, 0.40)
    borde_max = _mk_contract("AAPL261009C00216000", "call", 216.0, "2026-10-09", 2.40, 2.46, 0.39)
    sel = select_option_contract([borde_min], 215.0, "long_call", today=TODAY)
    assert sel is not None and sel["dte"] == 7
    sel2 = select_option_contract([borde_max], 215.0, "long_call", today=TODAY)
    assert sel2 is not None and sel2["dte"] == 45


def test_mercado_cruzado_rechazado(chain):
    cruzado = _mk_contract("AAPL260918C00214000", "call", 214.0, "2026-09-18", 1.60, 1.50, 0.40)  # ask < bid
    sel = select_option_contract(chain + [cruzado], 215.0, "long_call", today=TODAY)
    assert sel["occ_symbol"] != "AAPL260918C00214000"


def test_fila_malformada_se_descarta_sin_explotar(chain):
    mala = {"occ_symbol": "", "type": "call", "strike": 215.0, "expiry": "2026-09-18",
            "bid": 1.0, "ask": 1.05, "delta": 0.40}
    sel = select_option_contract(chain + [mala], 215.0, "long_call", today=TODAY)
    assert sel["occ_symbol"] != ""


def test_strike_ausente_y_volumen_basura_no_explotan(chain):
    sin_strike = {"occ_symbol": "MALO1", "type": "call", "expiry": "2026-09-18",
                  "bid": 1.0, "ask": 1.05, "delta": 0.40}  # sin strike -> skip fila
    vol_basura = _mk_contract("AAPL260918C00220000", "call", 220.0, "2026-09-18",
                              1.44, 1.50, 0.41)
    vol_basura["volume"] = "abc"
    vol_basura["open_interest"] = "xyz"
    sel = select_option_contract([sin_strike], 215.0, "long_call", today=TODAY)
    assert sel is None or sel["occ_symbol"] != "MALO1"
    sel2 = select_option_contract(chain + [vol_basura], 215.0, "long_call", today=TODAY)
    assert sel2 is not None and sel2["occ_symbol"] == "AAPL260918C00220000"


def test_spot_nan_inf_retorna_none(chain):
    assert select_option_contract(chain, float("nan"), "long_call", today=TODAY) is None
    assert select_option_contract(chain, float("inf"), "long_call", today=TODAY) is None


# ── size_position: risk-per-trade sizing ──────────────────────────────────────

# Reference equity used across sizing tests. risk_budget = 1.5% = 1390.21,
# max_risk (floor rule ceiling) = 3.0% = 2780.43, stop_loss_pct = 30%.
EQ = 92_680.93


def test_sizing_usa_riesgo_al_stop_no_la_prima():
    # ask 4.63 -> risk/contract = 463 * 0.30 = 138.90
    # floor(1390.21 / 138.90) = 10 -> tocado por el cap max_contracts
    s = size_position(ask=4.63, equity=EQ)
    assert s["qty"] == DEFAULT_OPTIONS_CONFIG["max_contracts"]
    assert s["risk_budget_usd"] == 1390.21
    assert s["premium_usd"] == pytest.approx(4630.0)
    assert s["risk_usd"] == pytest.approx(1389.0)


def test_sizing_prima_alta_ya_no_se_rechaza():
    # Bajo la regla vieja (prima <= 2% equity = 1853) esto daba qty 0.
    # ask 20 -> risk/contract = 600 -> floor(1390.21 / 600) = 2
    s = size_position(ask=20.00, equity=EQ)
    assert s["qty"] == 2
    assert s["premium_usd"] == pytest.approx(4000.0)
    assert s["risk_usd"] == pytest.approx(1200.0)


def test_sizing_respeta_cap_max_contracts():
    s = size_position(ask=0.50, equity=EQ)  # sin cap serian cientos
    assert s["qty"] == DEFAULT_OPTIONS_CONFIG["max_contracts"]


def test_sizing_piso_de_un_contrato_cuando_cabe_en_riesgo_maximo():
    # ask 60 -> risk/contract = 1800 > risk_budget 1390.21 -> qty calculado 0,
    # pero 1800 <= max_risk 2780.43 -> el piso lo lleva a 1.
    s = size_position(ask=60.00, equity=EQ)
    assert s["qty"] == 1
    assert s["floor_applied"] is True
    assert s["premium_usd"] == pytest.approx(6000.0)
    assert s["risk_usd"] == pytest.approx(1800.0)


def test_sizing_rechaza_contrato_sobre_el_riesgo_maximo_absoluto():
    # ask 100 -> risk/contract = 3000 > max_risk 2780.43 -> sin trade.
    s = size_position(ask=100.00, equity=EQ)
    assert s["qty"] == 0
    assert s["premium_usd"] == 0.0
    assert s["risk_usd"] == 0.0
    assert s["floor_applied"] is False


def test_sizing_config_override_de_riesgo():
    # 3% de riesgo con ask 20 (risk/contract 600) -> floor(2780.43 / 600) = 4
    s = size_position(ask=20.00, equity=EQ,
                      config={"risk_per_trade_pct": 3.0})
    assert s["qty"] == 4


def test_sizing_inputs_invalidos_fail_closed():
    assert size_position(ask=0, equity=EQ)["qty"] == 0
    assert size_position(ask=1.0, equity=-5)["qty"] == 0
    assert size_position(ask=None, equity=1000.0)["qty"] == 0
    assert size_position(ask=1.0, equity=0)["qty"] == 0


# ── build_exit_plan ───────────────────────────────────────────────────────────

def test_exit_plan_aritmetica_exacta():
    plan = build_exit_plan(2.00, "2026-09-18", today=TODAY)
    assert plan["tp_premium"] == pytest.approx(3.00)   # +50%
    assert plan["sl_premium"] == pytest.approx(1.40)   # -30%
    assert plan["min_exit_date"] == "2026-09-13"       # expiry - 5 dias


def test_exit_plan_custom_pcts():
    plan = build_exit_plan(1.00, "2026-10-16",
                           config={"take_profit_pct": 100.0, "stop_loss_pct": 50.0},
                           today=TODAY)
    assert plan["tp_premium"] == pytest.approx(2.00)
    assert plan["sl_premium"] == pytest.approx(0.50)


def test_exit_plan_expiry_invalido_usa_hoy_como_min_exit():
    plan = build_exit_plan(1.00, "fecha-mala", today=TODAY)
    assert plan["min_exit_date"] == TODAY.isoformat()
    assert plan["tp_premium"] == pytest.approx(1.50)


# ── resolve_direction ─────────────────────────────────────────────────────────

def test_resolve_direction_mapping_completo():
    assert resolve_direction("STRONG BUY") == "long_call"
    assert resolve_direction("ACCUMULATE") == "long_call"
    assert resolve_direction("SELL / TAKE PROFIT") == "long_put"
    assert resolve_direction("REDUCE") == "long_put"
    assert resolve_direction("HOLD") is None
    assert resolve_direction("") is None
    assert resolve_direction(None) is None


def test_sizing_rechaza_nan_e_inf():
    assert size_position(float("nan"), 1000.0)["qty"] == 0
    assert size_position(1.0, float("inf"))["qty"] == 0
    assert size_position(1.0, float("nan"))["qty"] == 0


def test_resolve_direction_normaliza_casing_y_espacios():
    assert resolve_direction("strong buy") == "long_call"
    assert resolve_direction("  ACCUMULATE  ") == "long_call"


def test_exit_plan_entry_invalido_retorna_primas_none():
    plan = build_exit_plan(None, "2026-09-18", today=TODAY)
    assert plan["tp_premium"] is None and plan["sl_premium"] is None
    assert plan["min_exit_date"] == "2026-09-13"
    plan2 = build_exit_plan("abc", "2026-09-18", today=TODAY)
    assert plan2["tp_premium"] is None
    plan3 = build_exit_plan(-2.0, "2026-09-18", today=TODAY)
    assert plan3["sl_premium"] is None
    assert build_exit_plan(float("nan"), "2026-09-18")["tp_premium"] is None


def test_exit_plan_expiry_pasado_min_exit_en_pasado_documentado():
    # Si el expiry ya pasó, min_exit queda en el pasado por diseño:
    # el pipeline nunca selecciona contratos con DTE < dte_min=7 (> min_dte_exit=5).
    from datetime import date as _d
    pasado = _d(2026, 8, 20).strftime("%Y-%m-%d")
    plan = build_exit_plan(1.00, pasado, today=_d(2026, 8, 25))
    assert plan["min_exit_date"] == "2026-08-15"


# ── premium: parse_target_strike + select_premium_contract ─────────────────────

def test_parse_target_strike_acepta_string_y_numero():
    assert parse_target_strike("300") == 300.0
    assert parse_target_strike(300) == 300.0


def test_parse_target_strike_fail_closed():
    for bad in (None, "", "abc", 0, -5, float("nan"), float("inf")):
        assert parse_target_strike(bad) is None


def test_select_premium_csp_solo_puts_aunque_call_este_mas_cerca():
    chain = [
        _mk_contract("AAPL260918C00220000", "call", 220.0, "2026-09-18", 1.44, 1.50, 0.41),
        _mk_contract("AAPL260918P00200000", "put", 200.0, "2026-09-18", 0.55, 0.60, -0.38),
    ]
    sel = select_premium_contract(chain, "cash_secured_put", 220, today=TODAY)
    assert sel is not None
    assert sel["type"] == "put"
    assert sel["occ_symbol"] == "AAPL260918P00200000"


def test_select_premium_covered_call_solo_calls():
    chain = [
        _mk_contract("AAPL260918P00200000", "put", 200.0, "2026-09-18", 0.55, 0.60, -0.38),
        _mk_contract("AAPL260918C00220000", "call", 220.0, "2026-09-18", 1.44, 1.50, 0.41),
    ]
    sel = select_premium_contract(chain, "covered_call", 200, today=TODAY)
    assert sel is not None
    assert sel["type"] == "call"
    assert sel["occ_symbol"] == "AAPL260918C00220000"


def test_select_premium_rankea_por_strike_mas_cercano():
    chain = [
        _mk_contract("AAPL260918P00290000", "put", 290.0, "2026-09-18", 4.00, 4.10, -0.35),
        _mk_contract("AAPL260918P00300000", "put", 300.0, "2026-09-18", 5.00, 5.10, -0.42),
        _mk_contract("AAPL260918P00310000", "put", 310.0, "2026-09-18", 6.00, 6.10, -0.50),
    ]
    sel = select_premium_contract(chain, "cash_secured_put", 302, today=TODAY)
    assert sel is not None
    assert sel["strike"] == 300.0
    assert sel["dte"] == 24
    assert "spread_pct" in sel


def test_select_premium_desempate_delta_luego_oi_luego_spread():
    # Strikes equidistantes (295 y 305 vs target 300). Gana el de menor
    # | |delta| - 0.40 |; 305 con delta -0.41 vence a 295 con delta -0.55.
    closer_delta = [
        _mk_contract("AAPL260918P00295000", "put", 295.0, "2026-09-18", 4.00, 4.10, -0.55, oi=9000),
        _mk_contract("AAPL260918P00305000", "put", 305.0, "2026-09-18", 5.00, 5.10, -0.41, oi=500),
    ]
    sel = select_premium_contract(closer_delta, "cash_secured_put", 300, today=TODAY)
    assert sel["strike"] == 305.0

    # Mismo gap de delta: gana el de mayor open interest.
    same_delta = [
        _mk_contract("AAPL260918P00295000", "put", 295.0, "2026-09-18", 4.00, 4.10, -0.40, oi=500),
        _mk_contract("AAPL260918P00305000", "put", 305.0, "2026-09-18", 5.00, 5.10, -0.40, oi=5000),
    ]
    sel2 = select_premium_contract(same_delta, "cash_secured_put", 300, today=TODAY)
    assert sel2["strike"] == 305.0

    # Mismo delta y OI: gana el spread mas estrecho (295).
    same_oi = [
        _mk_contract("AAPL260918P00295000", "put", 295.0, "2026-09-18", 4.00, 4.08, -0.40, oi=5000),
        _mk_contract("AAPL260918P00305000", "put", 305.0, "2026-09-18", 5.00, 5.40, -0.40, oi=5000),
    ]
    sel3 = select_premium_contract(same_oi, "cash_secured_put", 300, today=TODAY)
    assert sel3["strike"] == 295.0


def test_select_premium_rechaza_fuera_de_ventana_y_liquidez():
    far = _mk_contract("AAPL260828P00300000", "put", 300.0, "2026-08-28", 4.00, 4.10, -0.40)
    assert select_premium_contract([far], "cash_secured_put", 300, today=TODAY) is None

    no_bid = _mk_contract("AAPL260918P00300000", "put", 300.0, "2026-09-18", 0.00, 4.10, -0.40)
    assert select_premium_contract([no_bid], "cash_secured_put", 300, today=TODAY) is None

    no_ask = _mk_contract("AAPL260918P00300000", "put", 300.0, "2026-09-18", 4.00, 0.00, -0.40)
    assert select_premium_contract([no_ask], "cash_secured_put", 300, today=TODAY) is None

    crossed = _mk_contract("AAPL260918P00300000", "put", 300.0, "2026-09-18", 4.20, 4.00, -0.40)
    assert select_premium_contract([crossed], "cash_secured_put", 300, today=TODAY) is None

    wide = _mk_contract("AAPL260918P00300000", "put", 300.0, "2026-09-18", 4.00, 5.00, -0.40)
    assert select_premium_contract([wide], "cash_secured_put", 300, today=TODAY) is None

    illiquid = _mk_contract("AAPL260918P00300000", "put", 300.0, "2026-09-18",
                            4.00, 4.10, -0.40, volume=0, oi=50)
    assert select_premium_contract([illiquid], "cash_secured_put", 300, today=TODAY) is None


def test_select_premium_none_en_estrategia_vacia_o_strike_invalido():
    chain = [
        _mk_contract("AAPL260918P00300000", "put", 300.0, "2026-09-18", 4.00, 4.10, -0.40),
    ]
    assert select_premium_contract(chain, "iron_condor", 300, today=TODAY) is None
    assert select_premium_contract([], "cash_secured_put", 300, today=TODAY) is None
    assert select_premium_contract(chain, "cash_secured_put", 0, today=TODAY) is None
    assert select_premium_contract(chain, "cash_secured_put", "abc", today=TODAY) is None
    assert select_premium_contract(None, "cash_secured_put", 300, today=TODAY) is None


# ── premium: compute_csp_metrics + validate_csp_collateral ─────────────────────

def test_compute_csp_metrics_valores_conocidos():
    m = compute_csp_metrics(strike=300.0, bid=4.0, contracts=1)
    assert m["premium_per_share"] == 4.0
    assert m["premium_received"] == 400.0
    assert m["cash_collateral"] == 30000.0
    assert m["effective_entry"] == 296.0
    assert m["max_loss_approx"] == 29600.0


def test_compute_csp_metrics_yield_es_fraccion_decimal():
    # 400 / 30000 = 0.013333... A 1.33 result means the backend pre-multiplied.
    m = compute_csp_metrics(strike=300.0, bid=4.0, contracts=1)
    assert m["premium_yield"] == pytest.approx(0.013333, abs=1e-5)
    assert m["premium_yield"] < 0.5


def test_compute_csp_metrics_escala_lineal_con_contratos():
    m = compute_csp_metrics(strike=300.0, bid=4.0, contracts=3)
    assert m["premium_received"] == 1200.0
    assert m["cash_collateral"] == 90000.0
    assert m["effective_entry"] == 296.0
    assert m["premium_yield"] == pytest.approx(0.013333, abs=1e-5)


def test_compute_csp_metrics_fail_closed():
    assert compute_csp_metrics(strike=300.0, bid=0.0, contracts=1) is None
    assert compute_csp_metrics(strike=0.0, bid=4.0, contracts=1) is None
    assert compute_csp_metrics(strike=300.0, bid=4.0, contracts=0) is None
    assert compute_csp_metrics(strike=300.0, bid=4.0, contracts=-1) is None
    assert compute_csp_metrics(strike=float("nan"), bid=4.0, contracts=1) is None
    assert compute_csp_metrics(strike=300.0, bid=float("inf"), contracts=1) is None


def test_validate_csp_collateral_insuficiente():
    v = validate_csp_collateral(strike=300.0, buying_power=12450.0, contracts=1)
    assert v["sufficient"] is False
    assert v["required_collateral"] == 30000.0
    assert v["buying_power"] == 12450.0
    assert "30,000.00" in v["reason"]
    assert "12,450.00" in v["reason"]


def test_validate_csp_collateral_suficiente():
    v = validate_csp_collateral(strike=300.0, buying_power=74691.45, contracts=1)
    assert v["sufficient"] is True
    assert v["max_contracts_by_buying_power"] == 2


def test_validate_csp_collateral_borde_exacto():
    v = validate_csp_collateral(strike=300.0, buying_power=30000.0, contracts=1)
    assert v["sufficient"] is True
    assert v["max_contracts_by_buying_power"] == 1


def test_validate_csp_collateral_buying_power_invalido():
    for bad in (None, -1, float("nan"), float("inf")):
        v = validate_csp_collateral(strike=300.0, buying_power=bad, contracts=1)
        assert v["sufficient"] is False
        assert v["max_contracts_by_buying_power"] == 0


# ── premium: covered_call_contracts + compute_covered_call_metrics ─────────────

def test_covered_call_contracts_pisos_de_100_acciones():
    expected = {0: 0, 99: 0, 100: 1, 199: 1, 200: 2, 250: 2}
    for shares, n in expected.items():
        result = covered_call_contracts(shares, short_calls=0)
        assert result["contracts"] == n
        assert result["shares_owned"] == float(shares)
        assert result["short_calls"] == 0


def test_covered_call_contracts_neto_de_calls_cortos():
    assert covered_call_contracts(250, short_calls=1)["contracts"] == 1
    assert covered_call_contracts(250, short_calls=2)["contracts"] == 0
    assert covered_call_contracts(100, short_calls=5)["contracts"] == 0


def test_covered_call_contracts_fail_closed_si_lectura_falla():
    result = covered_call_contracts(500, short_calls=None)
    assert result["contracts"] == 0
    assert result["short_calls"] is None
    assert "could not read option positions" in result["reason"]


def test_covered_call_contracts_shares_invalidas():
    for bad in (-50, float("nan"), float("inf"), None):
        assert covered_call_contracts(bad, short_calls=0)["contracts"] == 0


def test_compute_covered_call_metrics_valores_conocidos():
    m = compute_covered_call_metrics(strike=320.0, bid=5.0, spot=300.0, contracts=1)
    assert m["premium_per_share"] == 5.0
    assert m["premium_received"] == 500.0
    assert m["effective_exit"] == 325.0
    assert m["max_upside_from_current"] == 25.0
    assert m["premium_yield"] == pytest.approx(500.0 / 30000.0)


def test_compute_covered_call_metrics_fail_closed():
    assert compute_covered_call_metrics(strike=320.0, bid=5.0, spot=0.0, contracts=1) is None
    assert compute_covered_call_metrics(strike=320.0, bid=0.0, spot=300.0, contracts=1) is None
    assert compute_covered_call_metrics(strike=320.0, bid=5.0, spot=300.0, contracts=0) is None
    assert compute_covered_call_metrics(strike=float("nan"), bid=5.0, spot=300.0, contracts=1) is None
    none_spot = compute_covered_call_metrics(strike=320.0, bid=5.0, spot=-1.0, contracts=1)
    assert none_spot is None
    assert none_spot is None  # no yield invented for a non-positive spot

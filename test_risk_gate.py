"""Tests del gate de riesgo duro.

Cubren especificamente las causas del incidente del 2026-08-31: 180 ordenes en
18 segundos sobre 3 simbolos, cash -263.703 USD y cuenta apalancada en margen.
Cada test aqui existe porque ese dia no habia un check equivalente.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from risk_gate import (
    DEFAULT_RISK_LIMITS,
    DailyLossTracker,
    build_client_order_id,
    evaluate_daily_loss,
    evaluate_new_entry,
    gross_exposure,
    normalize_account,
    option_underlying,
    position_symbols,
    size_order_qty,
    underlying_symbols,
)


def healthy_account(equity=100000.0, cash=100000.0, buying_power=100000.0):
    return {"equity": equity, "cash": cash, "buying_power": buying_power}


def option_account(equity=100000.0, cash=100000.0, buying_power=400000.0,
                   options_buying_power=100000.0):
    """Cuenta con `options_buying_power`: sin el, el check #8 falla cerrado."""
    return {"equity": equity, "cash": cash, "buying_power": buying_power,
            "options_buying_power": options_buying_power}


def opt_pos(occ, qty, premium):
    """Posicion de opcion como la devuelve Alpaca (`symbol` = OCC)."""
    return {"symbol": occ, "qty": qty, "market_value": premium * 100.0 * qty}


# ── normalize_account: fail-closed ──────────────────────────────────────────

def test_normalize_account_valido():
    acc = normalize_account(healthy_account())
    assert acc is not None
    assert acc["equity"] == 100000.0


@pytest.mark.parametrize("bad", [None, {}, {"equity": 0}, {"equity": -1}, {"equity": "abc"},
                                 {"equity": float("nan")}, {"equity": float("inf")},
                                 {"equity": 1000, "buying_power": None}])
def test_normalize_account_rechaza_invalidos(bad):
    assert normalize_account(bad) is None


# ── evaluate_new_entry: aprobacion basica ────────────────────────────────────

def test_entrada_aprobada_en_cuenta_sana():
    res = evaluate_new_entry(healthy_account(), [], symbol="AAPL", qty=6, price=300.0,
                             day_start_equity=100000.0)
    assert res["allowed"] is True
    assert res["reason"] == "approved"
    assert res["notional"] == 1800.0


def test_entrada_rechazada_snapshot_invalido():
    res = evaluate_new_entry(None, [], symbol="AAPL", qty=6, price=300.0)
    assert res["allowed"] is False
    assert res["reason"] == "invalid_account_snapshot"


@pytest.mark.parametrize("qty,price,reason", [
    (0, 300.0, "invalid_quantity"),
    (-5, 300.0, "invalid_quantity"),
    (6, 0.0, "invalid_price"),
    (6, -300.0, "invalid_price"),
    (float("nan"), 300.0, "invalid_quantity"),
])
def test_entrada_rechazada_inputs_invalidos(qty, price, reason):
    res = evaluate_new_entry(healthy_account(), [], symbol="AAPL", qty=qty, price=price)
    assert res["allowed"] is False
    assert res["reason"] == reason


def test_entrada_rechazada_sin_simbolo():
    res = evaluate_new_entry(healthy_account(), [], symbol="", qty=6, price=300.0)
    assert res["allowed"] is False
    assert res["reason"] == "missing_symbol"


# ── El bug del 31-ago: simbolo repetido ──────────────────────────────────────

def test_rechaza_simbolo_que_ya_esta_en_cartera():
    """AAPL recibio 166 ordenes de compra. Este check es el que falto."""
    positions = [{"symbol": "AAPL", "market_value": 117496.2}]
    res = evaluate_new_entry(healthy_account(), positions, symbol="AAPL", qty=6, price=300.0)
    assert res["allowed"] is False
    assert res["reason"] == "already_in_portfolio"


def test_simbolo_en_cartera_case_insensitive():
    positions = [{"symbol": "aapl", "market_value": 1000.0}]
    res = evaluate_new_entry(healthy_account(), positions, symbol="AAPL", qty=1, price=100.0)
    assert res["allowed"] is False
    assert res["reason"] == "already_in_portfolio"


def test_rechaza_orden_en_vuelo_del_mismo_ciclo():
    res = evaluate_new_entry(healthy_account(), [], symbol="NVDA", qty=9, price=200.0,
                             in_flight_symbols={"NVDA"})
    assert res["allowed"] is False
    assert res["reason"] == "order_already_in_flight"


def test_permite_simbolo_distinto_al_en_vuelo():
    res = evaluate_new_entry(healthy_account(), [], symbol="AMZN", qty=7, price=260.0,
                             in_flight_symbols={"NVDA"})
    assert res["allowed"] is True


# ── Limites de cartera ───────────────────────────────────────────────────────

def test_limite_global_de_posiciones_abiertas():
    positions = [{"symbol": f"S{i}", "market_value": 1000.0} for i in range(6)]
    res = evaluate_new_entry(healthy_account(), positions, symbol="NVDA", qty=1, price=100.0)
    assert res["allowed"] is False
    assert res["reason"] == "max_open_positions_reached"


def test_limite_cuenta_posiciones_en_vuelo():
    positions = [{"symbol": f"S{i}", "market_value": 1000.0} for i in range(5)]
    res = evaluate_new_entry(healthy_account(), positions, symbol="NVDA", qty=1, price=100.0,
                             in_flight_symbols={"MSFT"})
    assert res["allowed"] is False
    assert res["reason"] == "max_open_positions_reached"


def test_limite_porcentaje_por_posicion():
    # 8% del equity en una sola orden, con limite de 5%
    res = evaluate_new_entry(healthy_account(), [], symbol="NVDA", qty=40, price=200.0)
    assert res["allowed"] is False
    assert res["reason"] == "position_size_pct_limit"


def test_tope_absoluto_notional():
    # Cuenta grande: 8.000 USD es solo 0,8% del equity, pero supera el tope de 5.000
    res = evaluate_new_entry(healthy_account(equity=1000000.0, buying_power=1000000.0), [],
                             symbol="NVDA", qty=20, price=400.0)
    assert res["allowed"] is False
    assert res["reason"] == "notional_cap_exceeded"


def test_limite_exposicion_bruta():
    positions = [{"symbol": "AAPL", "market_value": 99000.0}]
    res = evaluate_new_entry(healthy_account(), positions, symbol="MSFT", qty=10, price=200.0)
    assert res["allowed"] is False
    assert res["reason"] == "gross_exposure_limit"


def test_colchon_de_buying_power():
    # buying power 1000, colchon 10% -> usable 900, orden de 950
    acc = healthy_account(equity=100000.0, cash=1000.0, buying_power=1000.0)
    res = evaluate_new_entry(acc, [], symbol="NVDA", qty=5, price=190.0)
    assert res["allowed"] is False
    assert res["reason"] == "insufficient_buying_power"


def test_buying_power_negativo_no_permite_nada():
    """La cuenta quedo en cash -263.703. Con buying power negativo no debe operar."""
    acc = healthy_account(equity=185761.0, cash=-263703.0, buying_power=-1000.0)
    res = evaluate_new_entry(acc, [], symbol="NVDA", qty=1, price=100.0)
    assert res["allowed"] is False
    assert res["reason"] == "insufficient_buying_power"


# ── Pausa por perdida diaria ─────────────────────────────────────────────────

def test_pausa_por_perdida_diaria():
    res = evaluate_new_entry(healthy_account(equity=94000.0), [], symbol="AAPL",
                             qty=1, price=100.0, day_start_equity=100000.0)
    assert res["allowed"] is False
    assert res["reason"] == "daily_loss_breach"
    assert res["daily_loss"]["loss_pct"] == -6.0


def test_no_pausa_si_perdida_es_menor_al_limite():
    res = evaluate_new_entry(healthy_account(equity=97000.0), [], symbol="AAPL",
                             qty=1, price=100.0, day_start_equity=100000.0)
    assert res["allowed"] is True
    assert res["daily_loss"]["breached"] is False


def test_daily_loss_unknown_no_bloquea():
    res = evaluate_daily_loss(100000.0, None)
    assert res["breached"] is False
    assert res["known"] is False
    assert res["reason"] == "daily_loss_unknown"


def test_daily_loss_limite_en_el_borde():
    res = evaluate_daily_loss(95000.0, 100000.0)
    assert res["breached"] is True


# ── sizing derivado del equity ───────────────────────────────────────────────

def test_sizing_deriva_del_equity_no_de_un_fijo():
    """Antes era int(2000/price), ignorando el tamano de la cuenta."""
    assert size_order_qty(100000.0, 200.0) == 25     # 5% de 100k = 5000 -> 25 acciones
    assert size_order_qty(20000.0, 200.0) == 5       # 5% de 20k = 1000 -> 5
    assert size_order_qty(200000.0, 200.0) == 25     # tope absoluto de 5000 manda


def test_sizing_escala_hasta_el_tope_absoluto():
    # A partir de 100k de equity el freno es el tope de 5.000 USD
    assert size_order_qty(100000.0, 100.0) == 50
    assert size_order_qty(10000000.0, 100.0) == 50


def test_sizing_fail_closed():
    assert size_order_qty(None, 100.0) == 0
    assert size_order_qty(100000.0, 0) == 0
    assert size_order_qty(100000.0, -5) == 0
    assert size_order_qty(-1.0, 100.0) == 0


# ── idempotencia ─────────────────────────────────────────────────────────────

def test_client_order_id_determinista_en_el_mismo_bucket():
    a = build_client_order_id("AAPL", "buy", "trend", now_epoch=1000.0)
    b = build_client_order_id("AAPL", "buy", "trend", now_epoch=1100.0)
    assert a == b


def test_client_order_id_cambia_entre_buckets():
    a = build_client_order_id("AAPL", "buy", "trend", now_epoch=1000.0)
    b = build_client_order_id("AAPL", "buy", "trend", now_epoch=1000.0 + 900)
    assert a != b


def test_client_order_id_cumple_limite_de_48_chars():
    cid = build_client_order_id("VERYLONGSYMBOL", "buy", "verylongstrategyname",
                                now_epoch=9999999999.0, nonce="abc123xyz")
    assert len(cid) <= 48
    assert all(c.isalnum() or c in "-_.:" for c in cid)


def test_client_order_id_distingue_simbolo_y_lado():
    a = build_client_order_id("AAPL", "buy", now_epoch=1000.0)
    b = build_client_order_id("AAPL", "sell", now_epoch=1000.0)
    c = build_client_order_id("NVDA", "buy", now_epoch=1000.0)
    assert len({a, b, c}) == 3


def test_client_order_id_sin_epoch_es_estable():
    assert build_client_order_id("AAPL", "buy") == build_client_order_id("AAPL", "buy")


# ── helpers ──────────────────────────────────────────────────────────────────

def test_position_symbols_y_gross_exposure():
    positions = [{"symbol": "AAPL", "market_value": -1000.0},
                 {"symbol": "NVDA", "market_value": 2500.0},
                 "basura"]
    assert position_symbols(positions) == {"AAPL", "NVDA"}
    assert gross_exposure(positions) == 3500.0


def test_gross_exposure_vacio():
    assert gross_exposure(None) == 0.0
    assert gross_exposure([]) == 0.0


# ── DailyLossTracker: persistencia ───────────────────────────────────────────

def test_tracker_fija_equity_de_apertura_una_vez(tmp_path):
    path = str(tmp_path / "risk_state.json")
    t = DailyLossTracker(path)
    assert t.day_start_equity("2026-09-01", 100000.0) == 100000.0
    # Aunque el equity cambie, el de apertura queda fijo
    assert t.day_start_equity("2026-09-01", 95000.0) == 100000.0


def test_tracker_separa_por_dia(tmp_path):
    path = str(tmp_path / "risk_state.json")
    t = DailyLossTracker(path)
    t.day_start_equity("2026-09-01", 100000.0)
    assert t.day_start_equity("2026-09-02", 95000.0) == 95000.0


def test_tracker_persiste_entre_instancias(tmp_path):
    """La pausa anterior vivia solo en memoria y se borraba al reiniciar."""
    path = str(tmp_path / "risk_state.json")
    DailyLossTracker(path).day_start_equity("2026-09-01", 100000.0)
    assert DailyLossTracker(path).day_start_equity("2026-09-01", 91000.0) == 100000.0


def test_tracker_pausa_y_limpieza(tmp_path):
    path = str(tmp_path / "risk_state.json")
    t = DailyLossTracker(path)
    assert t.is_paused("2026-09-01") is False
    t.pause("2026-09-01", "daily_loss_breach")
    assert t.is_paused("2026-09-01") is True
    t.clear()
    assert t.is_paused("2026-09-01") is False


def test_tracker_ignora_equity_invalido(tmp_path):
    path = str(tmp_path / "risk_state.json")
    t = DailyLossTracker(path)
    assert t.day_start_equity("2026-09-01", None) is None
    assert t.day_start_equity("2026-09-01", 0) is None


def test_tracker_sobrevive_json_corrupto(tmp_path):
    path = str(tmp_path / "risk_state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{no es json")
    t = DailyLossTracker(path)
    assert t.day_start_equity("2026-09-01", 100000.0) == 100000.0


# ── limites por defecto ──────────────────────────────────────────────────────

def test_limites_por_defecto_son_conservadores():
    assert DEFAULT_RISK_LIMITS["max_open_positions"] <= 10
    assert 0 < DEFAULT_RISK_LIMITS["max_daily_loss_pct"] <= 10
    assert 0 < DEFAULT_RISK_LIMITS["max_position_pct"] <= 10
    assert DEFAULT_RISK_LIMITS["max_gross_exposure_pct"] <= 100


def test_limite_personalizado_se_aplica():
    res = evaluate_new_entry(healthy_account(), [], symbol="NVDA", qty=1, price=100.0,
                             limits={"max_position_pct": 0.001})
    assert res["allowed"] is False
    assert res["reason"] == "position_size_pct_limit"


def test_limite_personalizado_invalido_no_rompe():
    res = evaluate_new_entry(healthy_account(), [], symbol="NVDA", qty=1, price=100.0,
                             limits={"max_open_positions": None})
    assert res["allowed"] is True


# ── R6: concentracion por subyacente ─────────────────────────────────────────
#
# Motivo: el 2026-09-02 el libro quedo con dos calls de SPY, mismo vencimiento
# (09-09) y strikes a 3 puntos (764 / 767) => correlacion ~1.0. El check #2 no
# las frenó porque compara el OCC completo contra el OCC completo, y
# `SPY260909C00764000` != `SPY260909C00767000`. Eran una sola apuesta partida en
# dos tickets por $8.708 de prima (8,7 % de la cuenta).

@pytest.mark.parametrize("symbol,esperado", [
    ("SPY260909C00764000", "SPY"),
    ("SPY260909C00767000", "SPY"),
    ("QQQ260909P00710000", "QQQ"),
    ("AAPL260918C00220000", "AAPL"),
    ("spy260909c00764000", "SPY"),
    ("XYZ20260918C02150000", "XYZ"),
    ("AAPL", None),
    ("SPY", None),
    ("", None),
    (None, None),
    (123, None),
    ("SPY260909X00764000", None),
])
def test_option_underlying_extrae_el_ticker(symbol, esperado):
    assert option_underlying(symbol) == esperado


def test_underlying_symbols_cuenta_por_subyacente_no_por_contrato():
    pos = [opt_pos("SPY260909C00764000", 1, 4.16),
           opt_pos("SPY260909C00767000", 10, 3.70),
           opt_pos("QQQ260909P00710000", 7, 6.56)]
    assert underlying_symbols(pos) == {"SPY": 2, "QQQ": 1}


def test_underlying_symbols_cuenta_las_acciones_bajo_su_propio_simbolo():
    pos = [{"symbol": "AAPL", "qty": 10, "market_value": 2000.0},
           opt_pos("SPY260909C00764000", 1, 4.16)]
    assert underlying_symbols(pos) == {"AAPL": 1, "SPY": 1}


def test_r6_rechaza_la_segunda_opcion_del_mismo_subyacente():
    """El caso real del 2026-09-02: con R6 el SPY 767C de $3.700 no habria entrado."""
    libro = [opt_pos("SPY260909C00764000", 1, 4.16)]
    res = evaluate_new_entry(option_account(), libro, symbol="SPY260909C00767000",
                             qty=10, price=3.70 * 100)
    assert res["allowed"] is False
    assert res["reason"] == "underlying_concentration_limit"
    assert res["underlying"] == "SPY"
    assert res["open_in_underlying"] == 1
    assert res["limit"] == 1


def test_r6_permite_subyacentes_distintos():
    """El QQQ 710P del 2026-09-02 seguia entrando: QQQ no estaba en cartera."""
    libro = [opt_pos("SPY260909C00764000", 1, 4.16)]
    res = evaluate_new_entry(option_account(), libro, symbol="QQQ260909P00710000",
                             qty=7, price=6.56 * 100)
    assert res["allowed"] is True


def test_r6_el_tope_por_defecto_bloquea_la_segunda_del_mismo_ticker():
    assert DEFAULT_RISK_LIMITS["max_positions_per_underlying"] == 1


def test_r6_con_tope_2_deja_pasar_la_segunda_del_mismo_subyacente():
    libro = [opt_pos("SPY260909C00764000", 1, 4.16)]
    res = evaluate_new_entry(option_account(), libro, symbol="SPY260909C00767000",
                             qty=10, price=3.70 * 100,
                             limits={"max_positions_per_underlying": 2})
    assert res["allowed"] is True


def test_r6_con_tope_2_sigue_rechazando_la_tercera():
    libro = [opt_pos("SPY260909C00764000", 1, 4.16),
             opt_pos("SPY260909C00767000", 10, 3.70)]
    res = evaluate_new_entry(option_account(), libro, symbol="SPY260909C00770000",
                             qty=1, price=2.00 * 100,
                             limits={"max_positions_per_underlying": 2})
    assert res["allowed"] is False
    assert res["reason"] == "underlying_concentration_limit"
    assert res["open_in_underlying"] == 2


def test_r6_limite_none_vuelve_al_default():
    """Misma convencion que `max_open_positions`: None no es un override."""
    libro = [opt_pos("SPY260909C00764000", 1, 4.16)]
    res = evaluate_new_entry(option_account(), libro, symbol="SPY260909C00767000",
                             qty=10, price=3.70 * 100,
                             limits={"max_positions_per_underlying": None})
    assert res["allowed"] is False
    assert res["reason"] == "underlying_concentration_limit"


def test_r6_cuenta_las_ordenes_en_vuelo_del_mismo_subyacente():
    """Dos contratos de SPY en el mismo ciclo: el segundo no se cuela."""
    res = evaluate_new_entry(option_account(), [], symbol="SPY260909C00767000",
                             qty=10, price=3.70 * 100,
                             in_flight_symbols={"SPY260909C00764000"})
    assert res["allowed"] is False
    assert res["reason"] == "underlying_concentration_limit"


def test_r6_un_subyacente_distinto_en_vuelo_no_bloquea():
    res = evaluate_new_entry(option_account(), [], symbol="SPY260909C00767000",
                             qty=10, price=3.70 * 100,
                             in_flight_symbols={"QQQ260909P00710000"})
    assert res["allowed"] is True


def test_r6_una_accion_cuenta_como_exposicion_a_su_ticker():
    """Cartera mixta: tener AAPL en acciones bloquea una call de AAPL."""
    pos = [{"symbol": "AAPL", "qty": 10, "market_value": 2000.0}]
    res = evaluate_new_entry(option_account(), pos, symbol="AAPL260918C00220000",
                             qty=1, price=2.20 * 100)
    assert res["allowed"] is False
    assert res["reason"] == "underlying_concentration_limit"
    assert res["underlying"] == "AAPL"


def test_el_duplicado_exacto_sigue_reportandose_como_already_in_portfolio():
    """R6 no tapa el check #2: el mismo contrato dos veces mantiene su motivo."""
    libro = [opt_pos("SPY260909C00764000", 1, 4.16)]
    res = evaluate_new_entry(option_account(), libro, symbol="SPY260909C00764000",
                             qty=1, price=4.16 * 100)
    assert res["allowed"] is False
    assert res["reason"] == "already_in_portfolio"


def test_r6_no_dispara_para_una_opcion_suelta_en_cartera_vacia():
    res = evaluate_new_entry(option_account(), [], symbol="SPY260909C00767000",
                             qty=10, price=3.70 * 100)
    assert res["allowed"] is True

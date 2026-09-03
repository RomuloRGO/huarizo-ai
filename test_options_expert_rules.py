"""Reglas de trader de opciones (R1 DTE · R2 IV vs RV · R3 theta · R5 plazos).

Contexto: antes de estas reglas el selector ordenaba candidatos solo por
|delta - target| dentro de la ventana DTE y caia siempre en el vencimiento mas
cercano (DTE 7). La cadena de Alpaca traia IV, theta y todos los vencimientos y
no se usaba NINGUNO. Estos tests fijan el comportamiento nuevo.

Datos medidos en vivo (SPY, 2026-09-02, 3978 contratos): iv y theta vienen
poblados en 3247 de ellos (82%), asi que las reglas son aplicables en la
practica y no solo en sinteticos.
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from options_engine import (
    DEFAULT_OPTIONS_CONFIG,
    evaluate_options_filters,
    iv_term_structure,
    realized_volatility,
    select_option_contract,
)

TODAY = date(2026, 8, 25)  # == TODAY de test_options_engine.py


def _exp(days):
    return (TODAY.toordinal() + days)


def _iso(days):
    return date.fromordinal(_exp(days)).isoformat()


def _mk(occ, ctype, strike, dte, bid, ask, delta,
        iv=0.25, theta=-0.03, oi=5000, volume=1000):
    """Contrato sintetico CON griegas, como los que devuelve el feed real.

    Por defecto theta=-0.03 sobre mid~1.47 => ~2.0%/dia, por debajo del cap
    de 3.0%/dia: el contrato "pasa" salvo que el test lo rompa a proposito.
    """
    c = {
        "occ_symbol": occ, "type": ctype, "strike": strike,
        "expiry": _iso(dte), "bid": bid, "ask": ask, "delta": delta,
        "iv": iv, "volume": volume, "open_interest": oi,
    }
    if theta is not None:
        c["theta"] = theta
    return c


# ── realized_volatility ───────────────────────────────────────────────────────

def test_rv_serie_constante_no_es_volatilidad_utilizable():
    assert realized_volatility([100.0] * 40) is None


def test_rv_crece_con_la_dispersion_de_la_serie():
    calm = [100.0 + (0.05 if i % 2 else -0.05) for i in range(60)]
    wild = [100.0 + (5.0 if i % 2 else -5.0) for i in range(60)]
    assert realized_volatility(wild) > realized_volatility(calm)


def test_rv_datos_insuficientes_retorna_none():
    assert realized_volatility([100.0, 101.0, 102.0]) is None
    assert realized_volatility([]) is None


def test_rv_ventana_ignora_el_historial_antiguo():
    # 60 sesiones muy volatiles y despues 30 planas: la ventana de 30 debe
    # ver solo la cola tranquila, no el historial completo.
    old = [100.0 + (8.0 if i % 2 else -8.0) for i in range(60)]
    tail = [100.0 + (0.02 if i % 2 else -0.02) for i in range(40)]
    assert realized_volatility(old + tail, window=30) < 0.10
    assert realized_volatility(old + tail, window=200) > 0.50


def test_rv_descarta_precios_invalidos_sin_explotar():
    series = [None, 0, -5, "basura", float("nan")] + [
        100.0 + (1.0 if i % 2 else -1.0) for i in range(40)]
    rv = realized_volatility(series)
    assert rv is not None and rv > 0


# ── R1 · preferencia dentro de la ventana DTE ─────────────────────────────────

def test_r1_prefiere_el_tramo_dulce_aunque_el_delta_sea_peor():
    corto = _mk("X260901C00215000", "call", 215.0, 7, 1.44, 1.50, 0.40)
    largo = _mk("X260924C00220000", "call", 220.0, 30, 2.40, 2.46, 0.45)
    # delta 0.40 es EXACTAMENTE el target: sin R1 ganaria el de 7 DTE.
    sel = select_option_contract([corto, largo], 215.0, "long_call", today=TODAY)
    assert sel["occ_symbol"] == "X260924C00220000"
    assert sel["dte"] == 30


def test_r1_la_preferencia_es_escalonada_no_binaria():
    # Sin contratos en el tramo dulce (>=21): 16 DTE gana a 10 DTE.
    diez = _mk("X260904C00215000", "call", 215.0, 10, 1.44, 1.50, 0.40)
    dieciseis = _mk("X260910C00215000", "call", 215.0, 16, 2.40, 2.46, 0.42)
    sel = select_option_contract([diez, dieciseis], 215.0, "long_call", today=TODAY)
    assert sel["dte"] == 16


def test_r1_es_preferencia_no_veto_si_no_hay_nada_mas_largo():
    corto = _mk("X260901C00215000", "call", 215.0, 7, 1.44, 1.50, 0.40)
    sel = select_option_contract([corto], 215.0, "long_call", today=TODAY)
    assert sel is not None and sel["dte"] == 7


# ── R3 · coste diario (theta) como % de la prima ──────────────────────────────

def test_r3_rechaza_contrato_que_quema_demasiada_prima_por_dia():
    caro = _mk("X260924C00220000", "call", 220.0, 30, 1.44, 1.50, 0.40, theta=-0.30)
    res = evaluate_options_filters([caro], 215.0, "long_call", today=TODAY)
    assert res["ok"] is False
    assert "theta" in res["reason"].lower() or "premium per day" in res["reason"].lower()
    assert res["contract"] is None


def test_r3_acepta_decaimiento_dentro_del_cap():
    ok = _mk("X260924C00220000", "call", 220.0, 30, 1.44, 1.50, 0.40, theta=-0.03)
    res = evaluate_options_filters([ok], 215.0, "long_call", today=TODAY)
    assert res["ok"] is True
    assert res["contract"]["theta_pct"] < DEFAULT_OPTIONS_CONFIG["max_theta_pct"]


def test_r3_si_puede_valorar_no_se_refugia_en_un_contrato_sin_griegas():
    """El caso importante: habiendo datos para juzgar, el rechazo es FIRME.

    Si el unico contrato valorable quema demasiado, NO vale escurrirse al
    contrato sin theta/iv. Un agente que se salta su propia regla cuando le
    incomoda no tiene regla.
    """
    valorable_malo = _mk("X260924C00220000", "call", 220.0, 30, 1.44, 1.50, 0.40,
                         theta=-0.40)
    sin_griegas = _mk("X260924C00219000", "call", 219.0, 30, 1.44, 1.50, 0.40,
                      iv=None, theta=None)
    res = evaluate_options_filters([valorable_malo, sin_griegas],
                                   215.0, "long_call", today=TODAY)
    assert res["ok"] is False
    assert res["contract"] is None


# ── R2 · IV del contrato vs volatilidad realizada ─────────────────────────────

def test_r2_rechaza_prima_cara_frente_a_la_realizada():
    caro = _mk("X260924C00220000", "call", 220.0, 30, 1.44, 1.50, 0.40, iv=0.60)
    res = evaluate_options_filters([caro], 215.0, "long_call",
                                   today=TODAY, realized_vol=0.20)
    assert res["ok"] is False
    assert "realized" in res["reason"].lower()
    assert res["stats"]["realized_vol"] == 0.20


def test_r2_acepta_prima_barata_frente_a_la_realizada():
    barato = _mk("X260924C00220000", "call", 220.0, 30, 1.44, 1.50, 0.40, iv=0.22)
    res = evaluate_options_filters([barato], 215.0, "long_call",
                                   today=TODAY, realized_vol=0.20)
    assert res["ok"] is True
    assert res["contract"]["iv_rv_ratio"] == 1.1


def test_r2_sin_volatilidad_realizada_no_bloquea_la_operacion():
    """No hay RV => no se puede juzgar => no se inventa un veto."""
    c = _mk("X260924C00220000", "call", 220.0, 30, 1.44, 1.50, 0.40, iv=0.90)
    res = evaluate_options_filters([c], 215.0, "long_call", today=TODAY,
                                   realized_vol=None)
    assert res["ok"] is True
    assert res["contract"]["iv_rv_ratio"] is None


# ── R5 · estructura de plazos (evento precioado) ──────────────────────────────

def test_r5_detecta_inversion_por_evento_y_rechaza_el_simbolo():
    near = _mk("X260908C00220000", "call", 220.0, 14, 1.44, 1.50, 0.40, iv=0.40)
    far = _mk("X260924C00220000", "call", 220.0, 30, 2.40, 2.46, 0.40, iv=0.20)
    res = evaluate_options_filters([near, far], 215.0, "long_call", today=TODAY)
    assert res["ok"] is False
    assert "inverted" in res["reason"].lower()
    assert res["stats"]["term"]["ratio"] == 2.0


def test_r5_acepta_estructura_normal_de_mercado_tranquilo():
    near = _mk("X260908C00220000", "call", 220.0, 14, 1.44, 1.50, 0.40, iv=0.20)
    far = _mk("X260924C00220000", "call", 220.0, 30, 2.40, 2.46, 0.40, iv=0.25)
    res = evaluate_options_filters([near, far], 215.0, "long_call", today=TODAY)
    assert res["ok"] is True
    # El tramo dulce gana, asi que se opera el de 30 DTE.
    assert res["contract"]["dte"] == 30


def test_r5_no_se_aplica_si_falta_uno_de_los_tramos():
    solo_largo = _mk("X260924C00220000", "call", 220.0, 30, 2.40, 2.46, 0.40, iv=0.25)
    term = iv_term_structure([solo_largo], DEFAULT_OPTIONS_CONFIG)
    assert term["applied"] is False
    res = evaluate_options_filters([solo_largo], 215.0, "long_call", today=TODAY)
    assert res["ok"] is True


# ── degradacion documentada ───────────────────────────────────────────────────

def test_sin_griegas_en_toda_la_cadena_se_degrada_y_lo_declara():
    sin_griegas = _mk("X260924C00220000", "call", 220.0, 30, 1.44, 1.50, 0.40,
                      iv=None, theta=None)
    res = evaluate_options_filters([sin_griegas], 215.0, "long_call", today=TODAY)
    assert res["ok"] is True
    assert res["stats"]["greeks_degraded"] is True
    assert res["stats"]["priced"] == 0


def test_con_griegas_no_hay_degradacion():
    con_griegas = _mk("X260924C00220000", "call", 220.0, 30, 1.44, 1.50, 0.40)
    res = evaluate_options_filters([con_griegas], 215.0, "long_call", today=TODAY)
    assert res["ok"] is True
    assert res["stats"].get("greeks_degraded") is None


def test_rechazo_siempre_trae_motivo_y_estadisticas():
    res = evaluate_options_filters([], 215.0, "long_call", today=TODAY)
    assert res["ok"] is False
    assert res["reason"]
    assert "candidates" in res["stats"]


# ── wrapper histórico ─────────────────────────────────────────────────────────

def test_el_wrapper_pasa_la_volatilidad_realizada():
    caro = _mk("X260924C00220000", "call", 220.0, 30, 1.44, 1.50, 0.40, iv=0.60)
    assert select_option_contract([caro], 215.0, "long_call",
                                  today=TODAY, realized_vol=0.20) is None
    barato = _mk("X260924C00220000", "call", 220.0, 30, 1.44, 1.50, 0.40, iv=0.22)
    assert select_option_contract([barato], 215.0, "long_call",
                                  today=TODAY, realized_vol=0.20) is not None

"""Validez del backtest: dejar de coronar ganadores sobre datos degenerados.

Tres defectos reales en `backtest_engine.py`, confirmados leyendo el código:

1. **Cláusula de entrada degenerada.** Cada flag desactivado dejaba su variable
   en `True` dentro de un OR: `c1 and (c2 or c3 or c4 or c5) and c6`. Con
   `use_squeeze=False`, `c5` quedaba en `True` y la entrada se cumplía en TODA
   barra. `mean_reversion` (el preset que gana el torneo) tiene `use_squeeze`
   y `use_sma_trend` en False, así que básicamente compraba siempre.

2. **`c6` siempre verdadero.** `c6 = i in bullish or not cfg["use_sma_trend"]`.
   Con `use_sma_trend=False` el `not` da True y el filtro de velas desaparecía.
   Otro OR degenerado que el plan no detectó.

3. **Profit factor inventado.** Con cero pérdidas, `profit_factor` se fijaba en
   **3.0**. Ese 3.0 es justo el tope de la fórmula del composite
   (`min(3.0, pf) * 15.0`), así que una estrategia con dos operaciones
   ganadoras recibía los 45 puntos completos. El `1e-9` en el denominador
   inflaba además cualquier PF con pérdidas ínfimas.

Semántica correcta: sin pérdidas el profit factor está INDETERMINADO (None),
no es 3.0. Y un PF indeterminado no puede sumar puntos al score.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import (
    DEFAULT_COMMISSION_PER_TRADE,
    DEFAULT_MIN_TRADES,
    DEFAULT_SLIPPAGE_PCT,
    build_active_conditions,
    evaluate_entry_conditions,
    is_profile_eligible,
    safe_profit_factor,
)


# ── Condiciones activas ───────────────────────────────────────────────────────

def test_disabled_flag_is_not_a_condition():
    conds = build_active_conditions({"use_rsi_oversold": True,
                                     "use_squeeze": False,
                                     "use_sma_trend": False})
    names = [name for name, _ in conds]
    assert "rsi" in names
    assert "squeeze" not in names
    assert "sma_trend" not in names


def test_entry_requires_at_least_one_active_condition():
    """El bug: con todos los flags apagados, `c2 or c3 or c4 or c5` era True."""
    conds = build_active_conditions({"use_rsi_oversold": False,
                                     "use_squeeze": False})
    assert conds == []
    assert evaluate_entry_conditions(
        {"use_rsi_oversold": False, "use_squeeze": False}, {}) is False


def test_all_four_signal_flags_are_recognised():
    cfg = {"use_rsi_oversold": True, "use_stoch_oversold": True,
           "use_bollinger": True, "use_squeeze": True}
    names = {n for n, _ in build_active_conditions(cfg)}
    assert names == {"rsi", "stoch", "bollinger", "squeeze"}


def test_mandatory_filters_use_and_not_or(tmp_path=None):
    """`sma_trend` y `candlestick` son filtros obligatorios (AND), no señales."""
    cfg = {"use_sma_trend": True, "use_candlestick": True,
           "use_rsi_oversold": True}
    # Señal RSI se cumple, pero la tendencia NO -> no se entra.
    ctx = {"price": 90.0, "sma_50": 100.0, "rsi": 20.0,
           "bullish_candle": True}
    assert evaluate_entry_conditions(cfg, ctx) is False


def test_candlestick_filter_is_real(tmp_path=None):
    """Con `use_sma_trend` en False el filtro de velas ya no se anula."""
    cfg = {"use_candlestick": True, "use_rsi_oversold": True}
    base = {"rsi": 20.0}
    assert evaluate_entry_conditions(cfg, dict(base, bullish_candle=True)) is True
    assert evaluate_entry_conditions(cfg, dict(base, bullish_candle=False)) is False


def test_conditions_evaluate_real_values():
    """Con contexto, el callable evalúa de verdad (no devuelve True siempre)."""
    cfg = {"use_rsi_oversold": True}
    conds = dict(build_active_conditions(cfg, {"rsi": 20.0}))
    assert conds["rsi"]() is True  # 20 < 35
    conds = dict(build_active_conditions(cfg, {"rsi": 70.0}))
    assert conds["rsi"]() is False  # 70 no está en sobreventa


# ── Profit factor ─────────────────────────────────────────────────────────────

def test_profit_factor_undefined_without_losses():
    """Antes devolvía 3.0 (el tope del score) en lugar de 'indeterminado'."""
    assert safe_profit_factor(gross_profit=100.0, gross_loss=0.0) is None


def test_profit_factor_undefined_when_nothing_happened():
    assert safe_profit_factor(gross_profit=0.0, gross_loss=0.0) is None


def test_profit_factor_normal_case():
    assert safe_profit_factor(gross_profit=200.0, gross_loss=100.0) == 2.0


def test_profit_factor_negative_gross_loss_is_treated_as_undefined():
    assert safe_profit_factor(gross_profit=100.0, gross_loss=-5.0) is None


# ── Elegibilidad del perfil ───────────────────────────────────────────────────

def test_profile_ineligible_below_min_trades():
    out = is_profile_eligible({"total_trades": 3, "oos_trades": 0,
                               "profit_factor": 3.0, "max_drawdown": 5.0})
    assert out["eligible"] is False
    assert out["reason"] == "insufficient_trades"


def test_profile_ineligible_without_oos():
    out = is_profile_eligible({"total_trades": 40, "oos_trades": 0,
                               "profit_factor": 2.0, "max_drawdown": 5.0})
    assert out["eligible"] is False
    assert out["reason"] == "no_out_of_sample"


def test_profile_eligible():
    out = is_profile_eligible({"total_trades": 40, "oos_trades": 12,
                               "profit_factor": 1.8, "max_drawdown": 6.0})
    assert out["eligible"] is True
    assert out["reason"] == "eligible"


def test_profile_ineligible_with_undefined_profit_factor():
    """Un PF None no puede declararse ganador por muchas operaciones que tenga."""
    out = is_profile_eligible({"total_trades": 40, "oos_trades": 12,
                               "profit_factor": None, "max_drawdown": 6.0})
    assert out["eligible"] is False
    assert out["reason"] == "undefined_profit_factor"


def test_default_min_trades_is_conservative():
    assert DEFAULT_MIN_TRADES >= 20


# ── Costos de trading ─────────────────────────────────────────────────────────

def test_costs_are_configured_and_positive():
    """Sin comisión ni slippage el backtest sobrestima el edge."""
    assert DEFAULT_COMMISSION_PER_TRADE > 0
    assert DEFAULT_SLIPPAGE_PCT > 0

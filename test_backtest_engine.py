"""Tests del motor de backtest (metricas sin red)."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_engine import run_backtest


def _synthetic_bars(days=120):
    dates = pd.date_range("2026-01-01", periods=days, freq="D")
    base = np.linspace(100.0, 130.0, days)
    noise = np.sin(np.arange(days) / 5.0) * 3.0
    close = base + noise
    return pd.DataFrame({
        "t": dates,
        "Open": close - 0.5,
        "High": close + 1.0,
        "Low": close - 1.0,
        "Close": close,
        "Volume": [1_000_000] * days,
    })


def test_run_backtest_retorna_max_drawdown_sin_nameerror():
    df = _synthetic_bars()
    res = run_backtest(df, strategy="trend_following")
    assert res.get("available") is True
    assert "max_drawdown_pct" in res
    assert isinstance(res["max_drawdown_pct"], (int, float))
    assert res["max_drawdown_pct"] >= 0.0  # convencion: magnitud positiva


def test_composite_score_presente_y_acotado():
    df = _synthetic_bars()
    res = run_backtest(df, strategy="trend_following")
    assert "composite_score" in res
    assert 0.0 <= res["composite_score"] <= 100.0

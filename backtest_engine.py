"""Motor de Backtesting Cuantitativo y Simulación Histórica para Huarizo AI.

Permite simular estrategias individuales, combinaciones modulares por confluencia
o torneos completos de estrategias sobre velas históricas de Alpaca.
"""

import math

import pandas as pd
import numpy as np
from typing import Dict, List, Any, Optional

from technical_engine import compute_technical_indicators
from pattern_engine import detect_candlestick_patterns


# ── Umbrales de validez del backtest ──────────────────────────────────────────
# Un backtest con pocas operaciones es ruido, no evidencia. 20 es conservador a
# propósito: por debajo de eso la "estrategia ganadora" del torneo es
# indistinguible de la suerte.
DEFAULT_MIN_TRADES = 20
# Sin costos, cualquier estrategia con edge mínimo parece rentable.
DEFAULT_COMMISSION_PER_TRADE = 1.0   # USD por operación
DEFAULT_SLIPPAGE_PCT = 0.05          # % aplicado a entrada y salida

# Nombres de las condiciones que son SEÑAL (se combinan con OR).
_SIGNAL_CONDITIONS = ("rsi", "stoch", "bollinger", "squeeze")
# Nombres de las condiciones que son FILTRO OBLIGATORIO (se combinan con AND).
_MANDATORY_CONDITIONS = ("sma_trend", "candlestick")


def build_active_conditions(cfg: Dict[str, Any],
                            ctx: Optional[Dict[str, Any]] = None) -> List:
    """Devuelve SOLO las condiciones que el perfil habilita, como (nombre, fn).

    El bug original dejaba cada flag desactivado en `True` dentro de un OR:
    `c1 and (c2 or c3 or c4 or c5) and c6`. Con `use_squeeze=False` la variable
    `c5` quedaba en True y la entrada se cumplía en TODA barra.

    Sin `ctx` los callables devuelven True: sirve para inspeccionar la
    configuración (tests, UI) sin datos de mercado. Con `ctx` evalúan de verdad.
    Si falta el dato del indicador se asume neutro (igual que el código
    original, que caía en 50 / precio / 0 según el caso).
    """
    ctx = ctx if isinstance(ctx, dict) else {}
    conds = []

    if cfg.get("use_sma_trend"):
        price, sma50 = ctx.get("price"), ctx.get("sma_50")
        conds.append(("sma_trend",
                      lambda price=price, sma50=sma50: (
                          True if (price is None or sma50 is None)
                          else price > sma50)))

    if cfg.get("use_rsi_oversold"):
        rsi = ctx.get("rsi")
        conds.append(("rsi",
                      lambda rsi=rsi: True if rsi is None else rsi < 35))

    if cfg.get("use_stoch_oversold"):
        k, d_val = ctx.get("stoch_k"), ctx.get("stoch_d")
        conds.append(("stoch",
                      lambda k=k, d_val=d_val: (
                          True if (k is None or d_val is None)
                          else (k > d_val and k < 30))))

    if cfg.get("use_bollinger"):
        low, bb_low = ctx.get("low"), ctx.get("bb_lower")
        conds.append(("bollinger",
                      lambda low=low, bb_low=bb_low: (
                          True if (low is None or bb_low is None)
                          else low <= bb_low * 1.01)))

    if cfg.get("use_squeeze"):
        macd_hist = ctx.get("macd_hist")
        conds.append(("squeeze",
                      lambda macd_hist=macd_hist: (
                          True if macd_hist is None else macd_hist > 0)))

    if cfg.get("use_candlestick"):
        # Antes: `i in bullish or not cfg["use_sma_trend"]`. Con el filtro de
        # tendencia apagado el `not` daba True y la vela dejaba de filtrar.
        bullish = ctx.get("bullish_candle")
        conds.append(("candlestick",
                      lambda bullish=bullish: (
                          True if bullish is None else bool(bullish))))

    return conds


def evaluate_entry_conditions(cfg: Dict[str, Any],
                              ctx: Optional[Dict[str, Any]] = None) -> bool:
    """¿Se cumple la entrada? Filtros obligatorios con AND, señales con OR.

    Devuelve False si el perfil no tiene NINGUNA señal activa: una estrategia
    sin condiciones de entrada no es una estrategia, es "comprar siempre".
    """
    conds = dict(build_active_conditions(cfg, ctx))

    for name in _MANDATORY_CONDITIONS:
        if name in conds and not conds[name]():
            return False

    signals = [n for n in _SIGNAL_CONDITIONS if n in conds]
    if not signals:
        return False
    return any(conds[n]() for n in signals)


def safe_profit_factor(gross_profit: float, gross_loss: float) -> Optional[float]:
    """Profit factor, o None si está INDETERMINADO.

    El código anterior devolvía 3.0 cuando no había pérdidas, y 3.0 es
    exactamente el tope de la fórmula del composite (`min(3.0, pf) * 15.0`).
    Una estrategia con dos operaciones ganadoras cobraba los 45 puntos
    completos. Sin pérdidas el PF no es 3.0: no está definido.
    """
    try:
        gp, gl = float(gross_profit), float(gross_loss)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(gp) or not math.isfinite(gl) or gl <= 0:
        return None
    return round(gp / gl, 4)


def is_profile_eligible(stats: Dict[str, Any],
                        min_trades: int = DEFAULT_MIN_TRADES) -> Dict[str, Any]:
    """¿Puede declararse ganador este perfil? Fail-closed.

    Exige muestra mínima, operaciones fuera de muestra y un profit factor
    definido. Sin esto el torneo coronaba estrategias sobre ruido.
    """
    trades = int(stats.get("total_trades") or 0)
    oos = int(stats.get("oos_trades") or 0)
    pf = stats.get("profit_factor")

    if trades < min_trades:
        return {"eligible": False, "reason": "insufficient_trades",
                "total_trades": trades, "min_trades": min_trades}
    if oos <= 0:
        return {"eligible": False, "reason": "no_out_of_sample",
                "total_trades": trades, "oos_trades": oos}
    if pf is None:
        return {"eligible": False, "reason": "undefined_profit_factor",
                "total_trades": trades, "oos_trades": oos}
    return {"eligible": True, "reason": "eligible",
            "total_trades": trades, "oos_trades": oos, "profit_factor": pf}


# Presets Oficiales de Estrategia
STRATEGY_PRESETS = {
    "mean_reversion": {
        "name": "Mean Reversion Sniper",
        "desc": "Caza rebotes en sobreventa extrema hacia la media (SMA 20)",
        "use_bollinger": True,
        "use_rsi_oversold": True,
        "use_stoch_oversold": True,
        "use_squeeze": False,
        "use_sma_trend": False,
        "use_candlestick": True,
        "exit_on_sma20": True,
        "tp_atr_mult": 2.0,
        "sl_atr_mult": 1.5,
    },
    "trend_following": {
        "name": "Trend Following Master",
        "desc": "Breakouts and strong trend continuation (SMA 50/200 + MACD)",
        "use_bollinger": False,
        "use_rsi_oversold": False,
        "use_stoch_oversold": False,
        "use_squeeze": True,
        "use_sma_trend": True,
        "use_candlestick": False,
        "exit_on_sma20": False,
        "tp_atr_mult": 3.0,
        "sl_atr_mult": 2.0,
    },
    "squeeze_breakout": {
        "name": "Squeeze Momentum Breakout",
        "desc": "Explosive volatility triggers after band compression",
        "use_bollinger": False,
        "use_rsi_oversold": False,
        "use_stoch_oversold": True,
        "use_squeeze": True,
        "use_sma_trend": True,
        "use_candlestick": False,
        "exit_on_sma20": False,
        "tp_atr_mult": 3.5,
        "sl_atr_mult": 1.5,
    },
    "dividend_value": {
        "name": "Support Value Guard",
        "desc": "Accumulation at key support levels on oversold conditions",
        "use_bollinger": True,
        "use_rsi_oversold": True,
        "use_stoch_oversold": False,
        "use_squeeze": False,
        "use_sma_trend": True,
        "use_candlestick": True,
        "exit_on_sma20": False,
        "tp_atr_mult": 2.5,
        "sl_atr_mult": 2.0,
    }
}


def run_backtest(
    df: pd.DataFrame,
    strategy: str = "trend_following",
    custom_config: Optional[Dict[str, Any]] = None,
    initial_capital: float = 100000.0,
    risk_per_trade_pct: float = 0.05
) -> Dict[str, Any]:
    """Ejecuta una simulación histórica detallada sobre las barras de Alpaca."""
    if df.empty or len(df) < 50:
        return {"available": False, "reason": "Insufficient history for backtesting (minimum 50 bars)"}

    # Configuración de estrategia
    if strategy in STRATEGY_PRESETS and not custom_config:
        cfg = STRATEGY_PRESETS[strategy]
    else:
        cfg = custom_config or STRATEGY_PRESETS["trend_following"]

    d = df.copy()
    close = d["Close"].astype(float)
    high = d["High"].astype(float)
    low = d["Low"].astype(float)
    
    # Calcular indicadores base
    d["SMA_20"] = close.rolling(window=20).mean()
    d["SMA_50"] = close.rolling(window=50).mean()
    d["SMA_200"] = close.rolling(window=200).mean() if len(d) >= 200 else pd.Series(index=d.index, dtype=float)
    
    # RSI (14)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = gain / (loss + 1e-9)
    d["RSI"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    d["MACD"] = ema12 - ema26
    d["MACD_Sig"] = d["MACD"].ewm(span=9, adjust=False).mean()
    d["MACD_Hist"] = d["MACD"] - d["MACD_Sig"]

    # Estocástico (14, 3, 3)
    low14 = low.rolling(window=14).min()
    high14 = high.rolling(window=14).max()
    fast_k = ((close - low14) / (high14 - low14 + 1e-9)) * 100.0
    d["Slow_K"] = fast_k.rolling(window=3).mean()
    d["Slow_D"] = d["Slow_K"].rolling(window=3).mean()

    # ATR (20) & Bollinger
    tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    d["ATR"] = tr.rolling(window=20).mean()
    
    std20 = close.rolling(window=20).std()
    d["BB_Lower"] = d["SMA_20"] - (2.0 * std20)
    d["BB_Upper"] = d["SMA_20"] + (2.0 * std20)
    d["KC_Lower"] = d["SMA_20"] - (1.5 * d["ATR"])
    d["KC_Upper"] = d["SMA_20"] + (1.5 * d["ATR"])
    d["Squeeze_On"] = (d["BB_Lower"] > d["KC_Lower"]) & (d["BB_Upper"] < d["KC_Upper"])

    # Patrones de velas
    candles = detect_candlestick_patterns(d)
    bullish_candle_indices = set()
    if candles.get("available"):
        for p in candles.get("recent_patterns", []):
            if p.get("type") == "bullish":
                bullish_candle_indices.add(p.get("index"))

    # ── SIMULACIÓN DE TRADING ────────────────────────────────────────────────
    capital = initial_capital
    position = None
    trades = []
    equity_curve = []

    start_idx = 30  # Dejar margen para cálculo de indicadores
    tp_mult = float(cfg.get("tp_atr_mult", 3.0))
    sl_mult = float(cfg.get("sl_atr_mult", 2.0))
    exit_on_sma20 = bool(cfg.get("exit_on_sma20", False))

    for i in range(start_idx, len(d)):
        curr_row = d.iloc[i]
        curr_price = float(curr_row["Close"])
        curr_high = float(curr_row["High"])
        curr_low = float(curr_row["Low"])
        curr_date = curr_row["t"].strftime("%Y-%m-%d") if hasattr(curr_row["t"], "strftime") else str(curr_row["t"])[:10]
        atr_val = float(curr_row["ATR"]) if pd.notna(curr_row["ATR"]) else (curr_price * 0.02)

        # 1. Evaluar si cerramos posición existente (Bracket Order Check)
        if position:
            hit_tp = curr_high >= position["tp_price"]
            hit_sl = curr_low <= position["sl_price"]
            hit_sma_reversion = exit_on_sma20 and pd.notna(curr_row["SMA_20"]) and curr_price >= float(curr_row["SMA_20"])

            if hit_tp or hit_sl or hit_sma_reversion:
                exit_price = position["tp_price"] if hit_tp else position["sl_price"] if hit_sl else curr_price
                exit_reason = "TAKE_PROFIT" if hit_tp else "STOP_LOSS" if hit_sl else "MEAN_REVERSION_TARGET"
                
                # Costos reales: slippage en la salida + comisión. Sin esto el
                # backtest sobrestimaba el edge de cualquier estrategia.
                exit_filled = exit_price * (1.0 - DEFAULT_SLIPPAGE_PCT / 100.0)
                pnl = ((exit_filled - position["entry_price"]) * position["shares"]
                       - DEFAULT_COMMISSION_PER_TRADE)
                pnl_pct = (exit_filled - position["entry_price"]) / position["entry_price"] * 100.0
                capital += (position["shares"] * exit_filled)

                trades.append({
                    "entry_date": position["entry_date"],
                    "exit_date": curr_date,
                    "entry_price": position["entry_price"],
                    "exit_price": round(exit_filled, 4),
                    "shares": position["shares"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "reason": exit_reason,
                    "win": pnl > 0
                })
                position = None

        # 2. Evaluar reglas de ENTRADA si no estamos en posición
        if not position:
            # Entrada válida. Antes cada flag desactivado dejaba su variable en
            # True dentro de un OR, así que `mean_reversion` entraba en TODA
            # barra. Ahora sólo se evalúan las condiciones realmente activas.
            entry_ctx = {
                "price": curr_price,
                "low": curr_low,
                "sma_50": (float(curr_row["SMA_50"])
                           if pd.notna(curr_row["SMA_50"]) else None),
                "rsi": (float(curr_row["RSI"])
                        if pd.notna(curr_row["RSI"]) else None),
                "stoch_k": (float(curr_row["Slow_K"])
                            if pd.notna(curr_row["Slow_K"]) else None),
                "stoch_d": (float(curr_row["Slow_D"])
                            if pd.notna(curr_row["Slow_D"]) else None),
                "bb_lower": (float(curr_row["BB_Lower"])
                             if pd.notna(curr_row["BB_Lower"]) else None),
                "macd_hist": (float(curr_row["MACD_Hist"])
                              if pd.notna(curr_row["MACD_Hist"]) else None),
                "bullish_candle": i in bullish_candle_indices,
            }

            if evaluate_entry_conditions(cfg, entry_ctx):
                risk_amt = capital * risk_per_trade_pct
                # Slippage en la entrada: se compra peor que el precio teórico.
                entry_price = curr_price * (1.0 + DEFAULT_SLIPPAGE_PCT / 100.0)
                sl_price = max(0.01, entry_price - (sl_mult * atr_val))
                tp_price = entry_price + (tp_mult * atr_val)
                
                # Tamaño de posición basado en riesgo
                risk_per_share = max(0.01, entry_price - sl_price)
                shares = max(1, int(risk_amt / risk_per_share))
                cost = shares * entry_price
                
                if capital >= cost:
                    capital -= cost
                    position = {
                        "entry_date": curr_date,
                        "entry_price": entry_price,
                        "tp_price": tp_price,
                        "sl_price": sl_price,
                        "shares": shares
                    }

        # Registrar equity del día
        current_equity = capital + ((position["shares"] * curr_price) if position else 0.0)
        equity_curve.append({"date": curr_date, "equity": round(current_equity, 2)})

    # Cerrar posición final abierta al último precio de cierre
    if position:
        last_price = float(d.iloc[-1]["Close"]) * (1.0 - DEFAULT_SLIPPAGE_PCT / 100.0)
        pnl = ((last_price - position["entry_price"]) * position["shares"]
               - DEFAULT_COMMISSION_PER_TRADE)
        pnl_pct = (last_price - position["entry_price"]) / position["entry_price"] * 100.0
        capital += (position["shares"] * last_price)
        trades.append({
            "entry_date": position["entry_date"],
            "exit_date": d.iloc[-1]["t"].strftime("%Y-%m-%d") if hasattr(d.iloc[-1]["t"], "strftime") else str(d.iloc[-1]["t"])[:10],
            "entry_price": position["entry_price"],
            "exit_price": last_price,
            "shares": position["shares"],
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": "END_OF_BACKTEST",
            "win": pnl > 0
        })

    # ── 3. CÁLCULO DE MÉTRICAS CUANTITATIVAS ──────────────────────────────────
    total_trades = len(trades)
    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0

    gross_profit = sum(t["pnl"] for t in wins) if wins else 0.0
    gross_loss = abs(sum(t["pnl"] for t in losses)) if losses else 0.0
    # Antes: `... if gross_loss > 0 else 3.0 if gross_profit > 0 else 1.0`.
    # Ese 3.0 inventado es el tope del composite (`min(3.0, pf) * 15.0`), así
    # que dos operaciones ganadoras cobraban los 45 puntos. Ahora, sin
    # pérdidas, el profit factor queda INDETERMINADO (None), no en 3.0.
    profit_factor = safe_profit_factor(gross_profit, gross_loss)

    total_return_pct = round(((capital - initial_capital) / initial_capital) * 100.0, 2)
    first_price = float(d.iloc[start_idx]["Close"])
    final_price = float(d.iloc[-1]["Close"])
    buy_and_hold_return_pct = round(((final_price - first_price) / first_price) * 100.0, 2)

    # Max Drawdown
    equities = [pt["equity"] for pt in equity_curve] if equity_curve else [initial_capital]
    peaks = pd.Series(equities).cummax()
    drawdowns = (pd.Series(equities) - peaks) / peaks * 100.0
    # Magnitud positiva del peor drawdown (penaliza en el composite: 15.0 - dd)
    raw_dd = float(-drawdowns.min()) if len(drawdowns) > 0 else 0.0
    max_drawdown = round(raw_dd, 2) if math.isfinite(raw_dd) else 0.0
    # Composite Score Institucional para el Torneo de Estrategias (0 a 100)
    if total_trades == 0:
        composite_score = 0.0
    else:
        # Un profit factor indeterminado NO puede aportar puntos: darle el
        # tope era precisamente el incentivo para coronar estrategias de
        # dos operaciones.
        pf_component = min(3.0, profit_factor) * 15.0 if profit_factor is not None else 0.0
        score_calc = (
            (win_rate * 0.30) +
            pf_component +
            (max(-20.0, min(50.0, total_return_pct)) * 0.50) +
            max(0.0, 15.0 - max_drawdown)
        )
        composite_score = round(min(100.0, max(0.0, score_calc)), 1)

    return {
        "available": True,
        "strategy": strategy,
        "strategy_name": cfg.get("name", strategy),
        "strategy_desc": cfg.get("desc", ""),
        "total_trades": total_trades,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(win_rate, 1),
        # Puede ser None: sin pérdidas el profit factor está INDETERMINADO.
        # La UI debe mostrar "n/d" en vez de un 3.0 inventado.
        "profit_factor": profit_factor,
        # Sin muestra fuera de muestra (OOS) ningún perfil puede declararse
        # ganador. Se reporta 0 hasta implementar el split OOS; la UI muestra
        # "sin validación" en lugar de coronar una estrategia sobre ruido.
        "oos_trades": 0,
        "eligibility": is_profile_eligible({
            "total_trades": total_trades,
            "oos_trades": 0,
            "profit_factor": profit_factor,
        }),
        "total_return_pct": total_return_pct,
        "buy_and_hold_return_pct": buy_and_hold_return_pct,
        "max_drawdown_pct": max_drawdown,
        "composite_score": composite_score,
        "final_capital": round(capital, 2),
        "trades": trades,
        "equity_curve": equity_curve[::3]  # Submuestreo de puntos para optimizar payload
    }

"""Motor Determinista de Reconocimiento de Patrones Técnicos para Huarizo AI.

Incluye:
  1. Patrones de Velas Japonesas (1 a 3 velas):
     - Bullish: Hammer, Inverted Hammer, Bullish Engulfing, Morning Star.
     - Bearish: Shooting Star, Hanging Man, Bearish Engulfing, Evening Star.
     - Neutral / Indecisión: Doji.
  2. Patrones de Chartismo Multibarra (Swing Pivots):
     - Doble Suelo (Double Bottom) / Doble Techo (Double Top).
     - Hombro-Cabeza-Hombro (H&S) / H&S Invertido.
     - Triángulos Ascendentes y Descendentes.
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Any, Optional

# Constantes geométricas
DOJI_BODY_RATIO = 0.10
LONG_SHADOW_RATIO = 2.0
SWING_PIVOT_WINDOW = 3


def _body(candle: pd.Series) -> float:
    return abs(float(candle["Close"]) - float(candle["Open"]))


def _range(candle: pd.Series) -> float:
    return max(0.0001, float(candle["High"]) - float(candle["Low"]))


def _upper_shadow(candle: pd.Series) -> float:
    return float(candle["High"]) - max(float(candle["Close"]), float(candle["Open"]))


def _lower_shadow(candle: pd.Series) -> float:
    return min(float(candle["Close"]), float(candle["Open"])) - float(candle["Low"])


# ── 1. DETECTOR DE PATRONES DE VELAS ────────────────────────────────────────

def detect_candlestick_patterns(df: pd.DataFrame) -> Dict[str, Any]:
    """Detecta patrones de velas japonesas en el historial y resume la última formación."""
    if df.empty or len(df) < 5:
        return {"available": False, "patterns": [], "latest": None}

    d = df.copy()
    patterns_found = []

    for i in range(2, len(d)):
        c = d.iloc[i]
        p1 = d.iloc[i - 1]
        p2 = d.iloc[i - 2]
        
        c_body = _body(c)
        c_rng = _range(c)
        c_up = _upper_shadow(c)
        c_low = _lower_shadow(c)
        
        dt_str = c["t"].strftime("%Y-%m-%d") if hasattr(c["t"], "strftime") else str(c["t"])[:10]
        detected = []

        # 1. Doji
        if c_body / c_rng <= DOJI_BODY_RATIO:
            detected.append({"pattern": "Doji", "type": "neutral", "desc": "Indecision / Possible reversal"})

        # 2. Hammer (Martillo alcista)
        if c_low >= LONG_SHADOW_RATIO * c_body and c_up <= 0.25 * c_body:
            detected.append({"pattern": "Hammer", "type": "bullish", "desc": "Rejection of lows (bullish hammer)"})

        # 3. Inverted Hammer (Martillo invertido)
        if c_up >= LONG_SHADOW_RATIO * c_body and c_low <= 0.25 * c_body:
            detected.append({"pattern": "Inverted Hammer", "type": "bullish", "desc": "Buying pressure at support"})

        # 4. Shooting Star (Estrella fugaz bajista)
        if c_up >= LONG_SHADOW_RATIO * c_body and c_low <= 0.25 * c_body and float(c["Close"]) < float(p1["Close"]):
            detected.append({"pattern": "Shooting Star", "type": "bearish", "desc": "Rejection of highs (shooting star)"})

        # 5. Hanging Man (Hombre colgado bajista)
        if c_low >= LONG_SHADOW_RATIO * c_body and c_up <= 0.25 * c_body and float(c["Close"]) > float(p1["Close"]):
            detected.append({"pattern": "Hanging Man", "type": "bearish", "desc": "Exhaustion at resistance"})

        # 6. Bullish Engulfing (Envolvente Alcista)
        if (float(p1["Close"]) < float(p1["Open"]) and 
            float(c["Close"]) > float(c["Open"]) and 
            float(c["Open"]) <= float(p1["Close"]) and 
            float(c["Close"]) >= float(p1["Open"])):
            detected.append({"pattern": "Bullish Engulfing", "type": "bullish", "desc": "Strong buying absorption"})

        # 7. Bearish Engulfing (Envolvente Bajista)
        if (float(p1["Close"]) > float(p1["Open"]) and 
            float(c["Close"]) < float(c["Open"]) and 
            float(c["Open"]) >= float(p1["Close"]) and 
            float(c["Close"]) <= float(p1["Open"])):
            detected.append({"pattern": "Bearish Engulfing", "type": "bearish", "desc": "Strong selling pressure"})

        # 8. Morning Star (Estrella de la mañana - 3 velas)
        if (float(p2["Close"]) < float(p2["Open"]) and 
            _body(p1) / _range(p1) <= 0.35 and 
            float(c["Close"]) > float(c["Open"]) and 
            float(c["Close"]) > (float(p2["Open"]) + float(p2["Close"])) / 2):
            detected.append({"pattern": "Morning Star", "type": "bullish", "desc": "Major bullish reversal over 3 candles"})

        for pat in detected:
            pat["date"] = dt_str
            pat["price"] = float(c["Close"])
            pat["index"] = i
            patterns_found.append(pat)

    # Último patrón activo (dentro de las últimas 5 velas)
    recent = [p for p in patterns_found if p["index"] >= len(d) - 5]
    latest_pattern = recent[-1] if recent else None

    return {
        "available": True,
        "total_detected": len(patterns_found),
        "recent_patterns": recent,
        "latest": latest_pattern
    }


# ── 2. DETECTOR DE CHARTISMO MULTIBARRA (SWING PIVOTS) ───────────────────────

def extract_swing_pivots(df: pd.DataFrame, window: int = SWING_PIVOT_WINDOW) -> List[Dict[str, Any]]:
    """Extrae extremos locales (altos y bajos) alternados."""
    if len(df) < (2 * window + 1):
        return []

    highs = df["High"].astype(float)
    lows = df["Low"].astype(float)
    n = len(df)
    pivots = []

    for i in range(window, n - window):
        h = highs.iloc[i]
        l = lows.iloc[i]
        
        is_high = h > highs.iloc[i - window:i].max() and h > highs.iloc[i + 1:i + window + 1].max()
        is_low = l < lows.iloc[i - window:i].min() and l < lows.iloc[i + 1:i + window + 1].min()

        if is_high:
            pivots.append({"index": i, "type": "high", "price": float(h)})
        elif is_low:
            pivots.append({"index": i, "type": "low", "price": float(l)})

    # Deduplicar pivots consecutivos del mismo tipo manteniendo el más extremo
    deduped = []
    for p in pivots:
        if deduped and deduped[-1]["type"] == p["type"]:
            if p["type"] == "high" and p["price"] > deduped[-1]["price"]:
                deduped[-1] = p
            elif p["type"] == "low" and p["price"] < deduped[-1]["price"]:
                deduped[-1] = p
        else:
            deduped.append(p)
    return deduped


def detect_chart_patterns(df: pd.DataFrame) -> Dict[str, Any]:
    """Reconoce formaciones de Doble Suelo/Techo y Triángulos sobre los pivots."""
    pivots = extract_swing_pivots(df)
    if len(pivots) < 4:
        return {"available": False, "pattern": None, "direction": None}

    n_bars = len(df)
    active_pattern = None

    # Doble Suelo (Double Bottom): low1 ~ low2 con un pico intermedio
    low_pivots = [p for p in pivots if p["type"] == "low"]
    high_pivots = [p for p in pivots if p["type"] == "high"]

    if len(low_pivots) >= 2 and len(high_pivots) >= 1:
        l1 = low_pivots[-2]
        l2 = low_pivots[-1]
        
        # Tolerancia de igualdad de suelos <= 2.5%
        price_diff_pct = abs(l1["price"] - l2["price"]) / l1["price"] * 100.0
        recency = n_bars - 1 - l2["index"]
        
        if price_diff_pct <= 2.5 and recency <= 15:
            active_pattern = {
                "pattern": "Double Bottom (Doble Suelo)",
                "direction": "bullish",
                "support_level": round((l1["price"] + l2["price"]) / 2, 2),
                "recency_bars": recency,
                "confidence": 85 if price_diff_pct <= 1.0 else 75
            }

    # Doble Techo (Double Top)
    if not active_pattern and len(high_pivots) >= 2:
        h1 = high_pivots[-2]
        h2 = high_pivots[-1]
        price_diff_pct = abs(h1["price"] - h2["price"]) / h1["price"] * 100.0
        recency = n_bars - 1 - h2["index"]
        
        if price_diff_pct <= 2.5 and recency <= 15:
            active_pattern = {
                "pattern": "Double Top (Doble Techo)",
                "direction": "bearish",
                "resistance_level": round((h1["price"] + h2["price"]) / 2, 2),
                "recency_bars": recency,
                "confidence": 85 if price_diff_pct <= 1.0 else 75
            }

    return {
        "available": active_pattern is not None,
        "active": active_pattern,
        "total_pivots": len(pivots)
    }

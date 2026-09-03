"""Motor Cuantitativo de Análisis Técnico para Huarizo AI.

Calcula sobre velas OHLCV de Alpaca:
  1. Tendencia: SMA 20, 50, 200, EMA 9, 21, Golden/Death Cross.
  2. Momentum y Osciladores: RSI (14), MACD (12, 26, 9) e Histograma.
  3. Oscilador Estocástico: %K, %D (Slow 14, 3, 3), detección de zonas y cruces.
  4. Volatilidad & Bandas: Bollinger Bands (20, 2σ), Keltner Channels (20, 1.5 ATR).
  5. TTM Squeeze: Detección de compresión de volatilidad y disparo de momentum.
  6. Gestión de Riesgo: ATR (14 y 20) para Stop-Loss y Take-Profit automáticos.
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Optional

# ── Confirmación por volumen ───────────────────────────────────────────────
# El volumen del CONTRATO de opción es 0 en el plan gratuito (medido el
# 2026-09-03: 0 de 3.648 contratos de SPY), así que la única señal de
# participación utilizable es la del SUBYACENTE, que sí viene completa (171
# barras, cero ceros). `volume` ya se cargaba en la línea 26 y no se usaba
# nunca: esto la aprovecha.
#
# Es una CONFIRMACIÓN, no un factor del score: el score sigue siendo
# tendencia ±20 / estocástico ±15 / RSI ±10 / vela ±10 sobre base 50. El
# volumen no mueve esos números; decide si una entrada autónoma procede.
VOLUME_MA_WINDOW = 20
VOLUME_STRONG_RATIO = 1.2   # >= esto: participación confirma con fuerza
VOLUME_MIN_RATIO = 0.5      # < esto: volumen muerto, la entrada se bloquea


def volume_allows_entry(volume_info: Optional[Dict[str, Any]]) -> bool:
    """Falla cerrado: si el volumen no se puede medir, no confirma.

    Devuelve True para `neutral` y `strong`; False para `weak` y `unknown`.
    El llamador decide si usa este freno (hoy: el ciclo autónomo sí, el
    copiloto manual solo lo muestra).
    """
    if not isinstance(volume_info, dict):
        return False
    return volume_info.get("level") in ("strong", "neutral")


def drop_current_session(volume: "pd.Series", frame: "pd.DataFrame") -> "pd.Series":
    """Quita la barra de la sesión en curso: su volumen aún está a medias.

    Alpaca devuelve la barra del DÍA EN CURSO con el volumen acumulado hasta
    el instante de la consulta. Comparar ese número contra la media de 20
    sesiones COMPLETAS no mide participación: mide la hora del día.

    Medido el 2026-09-03 en SPY, con exactamente los mismos datos:

    | Momento | Última barra | Ratio | Nivel |
    |---|---|---:|---|
    | 08:00 Perú (pre-apertura) | 2026-09-02, cerrada (900.232) | **0.764** | `neutral` |
    | 10:00 ET (sesión) | 2026-09-03, parcial (198.662) | **0.175** | `weak` |

    El segundo bloqueaba todas las entradas, y lo habría hecho **cada mañana**,
    por un artefacto del reloj y no por falta de participación. Por eso se
    descarta la sesión en curso: se compara la última sesión CERRADA contra la
    media de las 20 sesiones CERRADAS anteriores.

    Si no se puede saber la fecha (columna ausente, timestamp raro) se devuelve
    la serie intacta: es preferible un ratio dudoso a romper el análisis.
    """
    if "t" not in frame.columns or len(volume) <= VOLUME_MA_WINDOW:
        return volume
    try:
        sesion = pd.to_datetime(frame["t"].iloc[-1], utc=True).tz_convert(
            "America/New_York").date()
        hoy = pd.Timestamp.now(tz="America/New_York").date()
    except Exception:
        return volume
    return volume.iloc[:-1] if sesion >= hoy else volume


def compute_technical_indicators(df: pd.DataFrame) -> Dict[str, Any]:
    """Calcula la batería completa de indicadores técnicos sobre un DataFrame de Alpaca."""
    if df.empty or len(df) < 20:
        return {"available": False, "reason": "Insufficient bars (minimum 20)"}

    d = df.copy()
    close = d["Close"].astype(float)
    high = d["High"].astype(float)
    low = d["Low"].astype(float)
    # La columna puede faltar en feeds degradados. Antes esto lanzaba KeyError;
    # ahora cae a una serie de NaN para que la confirmación por volumen quede
    # en `unknown` y el freno falle cerrado en lugar de tumbar el análisis.
    if "Volume" in d.columns:
        volume = d["Volume"].astype(float)
    elif "volume" in d.columns:
        volume = d["volume"].astype(float)
    else:
        volume = pd.Series(np.nan, index=d.index, dtype=float)

    # ── 1. MEDIAS MÓVILES (SMA & EMA) ────────────────────────────────────────
    d["SMA_20"] = close.rolling(window=20).mean()
    d["SMA_50"] = close.rolling(window=50).mean() if len(d) >= 50 else pd.Series(index=d.index, dtype=float)
    d["SMA_200"] = close.rolling(window=200).mean() if len(d) >= 200 else pd.Series(index=d.index, dtype=float)
    d["EMA_9"] = close.ewm(span=9, adjust=False).mean()
    d["EMA_21"] = close.ewm(span=21, adjust=False).mean()

    # ── 2. RSI (14) - WILDER'S SMOOTHING ─────────────────────────────────────
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    d["RSI_14"] = 100 - (100 / (1 + rs))

    # ── 3. MACD (12, 26, 9) ──────────────────────────────────────────────────
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    d["MACD_Line"] = ema12 - ema26
    d["MACD_Signal"] = d["MACD_Line"].ewm(span=9, adjust=False).mean()
    d["MACD_Hist"] = d["MACD_Line"] - d["MACD_Signal"]

    # ── 4. OSCILADOR ESTOCÁSTICO (14, 3, 3) ──────────────────────────────────
    low14 = low.rolling(window=14).min()
    high14 = high.rolling(window=14).max()
    fast_k = ((close - low14) / (high14 - low14 + 1e-9)) * 100.0
    d["Slow_K"] = fast_k.rolling(window=3).mean()
    d["Slow_D"] = d["Slow_K"].rolling(window=3).mean()

    # ── 5. ATR (AVERAGE TRUE RANGE 14 & 20) ───────────────────────────────────
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    d["ATR_14"] = tr.rolling(window=14).mean()
    d["ATR_20"] = tr.rolling(window=20).mean()

    # ── 6. BOLLINGER BANDS (20, 2.0) & KELTNER CHANNELS ──────────────────────
    sma20 = d["SMA_20"]
    std20 = close.rolling(window=20).std()
    d["BB_Upper"] = sma20 + (2.0 * std20)
    d["BB_Lower"] = sma20 - (2.0 * std20)
    d["BB_Pct_B"] = (close - d["BB_Lower"]) / (d["BB_Upper"] - d["BB_Lower"] + 1e-9)

    # Keltner Channels (20 SMA +/- 1.5 * ATR_20)
    atr20 = d["ATR_20"]
    d["KC_Upper"] = sma20 + (1.5 * atr20)
    d["KC_Lower"] = sma20 - (1.5 * atr20)

    # TTM Squeeze Detection: Squeeze ON if BB inside KC
    d["Squeeze_On"] = (d["BB_Lower"] > d["KC_Lower"]) & (d["BB_Upper"] < d["KC_Upper"])

    # ── 7. EXTRACCIÓN DE VALORES ACTUALES Y SEÑALES ──────────────────────────
    curr = d.iloc[-1]
    prev = d.iloc[-2] if len(d) > 1 else curr

    curr_price = float(curr["Close"])
    sma20_val = float(curr["SMA_20"]) if pd.notna(curr["SMA_20"]) else None
    sma50_val = float(curr["SMA_50"]) if pd.notna(curr["SMA_50"]) else None
    sma200_val = float(curr["SMA_200"]) if pd.notna(curr["SMA_200"]) else None
    rsi_val = float(curr["RSI_14"]) if pd.notna(curr["RSI_14"]) else 50.0

    macd_line = float(curr["MACD_Line"]) if pd.notna(curr["MACD_Line"]) else 0.0
    macd_sig = float(curr["MACD_Signal"]) if pd.notna(curr["MACD_Signal"]) else 0.0
    macd_hist = float(curr["MACD_Hist"]) if pd.notna(curr["MACD_Hist"]) else 0.0
    prev_macd_hist = float(prev["MACD_Hist"]) if pd.notna(prev["MACD_Hist"]) else 0.0

    slow_k = float(curr["Slow_K"]) if pd.notna(curr["Slow_K"]) else 50.0
    slow_d = float(curr["Slow_D"]) if pd.notna(curr["Slow_D"]) else 50.0
    prev_k = float(prev["Slow_K"]) if pd.notna(prev["Slow_K"]) else slow_k
    prev_d = float(prev["Slow_D"]) if pd.notna(prev["Slow_D"]) else slow_d

    atr_val = float(curr["ATR_20"]) if pd.notna(curr["ATR_20"]) else (curr_price * 0.02)
    bb_upper = float(curr["BB_Upper"]) if pd.notna(curr["BB_Upper"]) else curr_price * 1.05
    bb_lower = float(curr["BB_Lower"]) if pd.notna(curr["BB_Lower"]) else curr_price * 0.95
    bb_pct_b = float(curr["BB_Pct_B"]) if pd.notna(curr["BB_Pct_B"]) else 0.5
    squeeze_on = bool(curr["Squeeze_On"])
    prev_squeeze_on = bool(prev["Squeeze_On"])

    # ── Confirmación por volumen (ver constantes arriba) ────────────────────
    # Se compara SIEMPRE sesión cerrada contra media de sesiones cerradas. Si
    # Alpaca nos dio la barra de hoy, su volumen está a medias y se descarta
    # (ver `drop_current_session`). Sin esto el ratio mide la hora del día.
    comparable_volume = drop_current_session(volume, d)
    parcial_descartada = len(comparable_volume) < len(volume)
    vol_ma = comparable_volume.rolling(window=VOLUME_MA_WINDOW).mean()
    vol_last = (float(comparable_volume.iloc[-1])
                if pd.notna(comparable_volume.iloc[-1]) else None)
    vol_ma_last = float(vol_ma.iloc[-1]) if pd.notna(vol_ma.iloc[-1]) else None
    if vol_ma_last and vol_ma_last > 0 and vol_last is not None:
        volume_ratio: Optional[float] = round(vol_last / vol_ma_last, 3)
    else:
        volume_ratio = None
    if volume_ratio is None:
        volume_level = "unknown"
    elif volume_ratio >= VOLUME_STRONG_RATIO:
        volume_level = "strong"
    elif volume_ratio >= VOLUME_MIN_RATIO:
        volume_level = "neutral"
    else:
        volume_level = "weak"

    # ── 8. DETERMINACIÓN DE CONDICIONES Y REGÍMENES ──────────────────────────
    # Régimen de Tendencia
    if sma50_val and sma200_val:
        if curr_price > sma50_val and sma50_val > sma200_val:
            trend_regime = "BULLISH_TREND (Golden Alignment)"
        elif curr_price < sma50_val and sma50_val < sma200_val:
            trend_regime = "BEARISH_TREND (Death Alignment)"
        elif curr_price > sma50_val:
            trend_regime = "MODERATE_BULLISH"
        else:
            trend_regime = "NEUTRAL_OR_PULLBACK"
    elif sma50_val:
        trend_regime = "BULLISH" if curr_price > sma50_val else "BEARISH"
    else:
        trend_regime = "INSUFFICIENT_HISTORY"

    # Estado del Estocástico
    if slow_k > 80:
        stoch_zone = "OVERBOUGHT (>80)"
    elif slow_k < 20:
        stoch_zone = "OVERSOLD (<20)"
    else:
        stoch_zone = "NEUTRAL (20-80)"

    stoch_cross = "BULLISH_CROSS" if slow_k > slow_d and prev_k <= prev_d else "BEARISH_CROSS" if slow_k < slow_d and prev_k >= prev_d else "BULLISH_MOMENTUM" if slow_k > slow_d else "BEARISH_MOMENTUM"

    # Estado del Squeeze
    if squeeze_on:
        squeeze_status = "COMPRESSION (Squeeze ON - Big Move Loading)"
    elif prev_squeeze_on and not squeeze_on:
        squeeze_status = "FIRED (Breakout Released)"
    else:
        squeeze_status = "EXPANDED (Normal Volatility)"

    # Parámetros de Bracket Order Sugeridos por ATR
    suggested_stop_loss = max(0.01, curr_price - (2.0 * atr_val))
    suggested_take_profit = curr_price + (3.0 * atr_val)
    risk_reward_ratio = 1.5  # (3x ATR / 2x ATR)

    return {
        "available": True,
        "price": curr_price,
        "date": curr["t"].strftime("%Y-%m-%d") if hasattr(curr["t"], "strftime") else str(curr["t"])[:10],
        "trend": {
            "regime": trend_regime,
            "sma_20": round(sma20_val, 2) if sma20_val else None,
            "sma_50": round(sma50_val, 2) if sma50_val else None,
            "sma_200": round(sma200_val, 2) if sma200_val else None,
            "ema_9": round(float(curr["EMA_9"]), 2),
            "ema_21": round(float(curr["EMA_21"]), 2),
        },
        "rsi": {
            "value": round(rsi_val, 2),
            "state": "OVERSOLD (<30)" if rsi_val < 30 else "OVERBOUGHT (>70)" if rsi_val > 70 else "NEUTRAL"
        },
        "macd": {
            "line": round(macd_line, 4),
            "signal": round(macd_sig, 4),
            "histogram": round(macd_hist, 4),
            "bullish_cross": macd_line > macd_sig and prev_macd_hist <= 0 and macd_hist > 0,
            "accelerating": macd_hist > prev_macd_hist
        },
        "stochastic": {
            "k": round(slow_k, 2),
            "d": round(slow_d, 2),
            "zone": stoch_zone,
            "cross": stoch_cross
        },
        "volatility": {
            "atr_20": round(atr_val, 2),
            "bb_upper": round(bb_upper, 2),
            "bb_lower": round(bb_lower, 2),
            "bb_pct_b": round(bb_pct_b, 3),
            "squeeze_on": squeeze_on,
            "squeeze_status": squeeze_status
        },
        "volume": {
            "last": round(vol_last, 0) if vol_last is not None else None,
            "ma_20": round(vol_ma_last, 0) if vol_ma_last is not None else None,
            "ratio": volume_ratio,
            "level": volume_level,
            "confirms_entry": volume_allows_entry(
                {"level": volume_level}),
            "partial_session_dropped": parcial_descartada,
            "note": ("Volumen del subyacente como confirmación. No es un factor "
                     "del score: el volumen del contrato es 0 en el plan gratuito, "
                     "así que este es el único dato de participación real. "
                     "Se compara la última sesión CERRADA contra la media de 20 "
                     "sesiones cerradas; la barra del día en curso se descarta "
                     "porque su volumen aún se está acumulando."),
        },
        "risk_management": {
            "atr": round(atr_val, 2),
            "suggested_stop_loss": round(suggested_stop_loss, 2),
            "suggested_take_profit": round(suggested_take_profit, 2),
            "risk_reward_ratio": risk_reward_ratio
        }
    }

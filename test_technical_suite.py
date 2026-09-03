"""Test suite para technical_engine.py y pattern_engine.py sobre datos de Alpaca."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alpaca_service import AlpacaService
from technical_engine import compute_technical_indicators
from pattern_engine import detect_candlestick_patterns, detect_chart_patterns

def test_technical_suite_live():
    print("==================================================")
    print("PROBANDO MOTORES TÉCNICOS Y PATRONES PARA HUARIZO AI")
    print("==================================================")
    service = AlpacaService()
    
    ticker = "NVDA"
    df = service.get_stock_bars(ticker, days=250)
    print(f"Descargadas {len(df)} barras de {ticker} desde Alpaca.")
    assert not df.empty and len(df) >= 50, "Datos insuficientes para el test"
    
    # 1. Test Indicadores Técnicos
    tech = compute_technical_indicators(df)
    print("\n--- 1. INDICADORES TÉCNICOS (NVDA) ---")
    print(f"• Precio: ${tech['price']:.2f}")
    print(f"• Régimen de Tendencia: {tech['trend']['regime']}")
    print(f"• RSI (14): {tech['rsi']['value']} ({tech['rsi']['state']})")
    print(f"• MACD Line: {tech['macd']['line']}, Hist: {tech['macd']['histogram']}")
    print(f"• Estocástico %K: {tech['stochastic']['k']}, %D: {tech['stochastic']['d']} -> {tech['stochastic']['cross']} ({tech['stochastic']['zone']})")
    print(f"• TTM Squeeze: {tech['volatility']['squeeze_status']}")
    print(f"• Stop-Loss sugerido (2x ATR): ${tech['risk_management']['suggested_stop_loss']:.2f}")
    print(f"• Take-Profit sugerido (3x ATR): ${tech['risk_management']['suggested_take_profit']:.2f}")
    
    assert tech["available"] is True, "Cálculo técnico falló"
    assert tech["rsi"]["value"] > 0, "RSI inválido"
    assert tech["stochastic"]["k"] >= 0, "Estocástico inválido"

    # 2. Test Patrones de Velas
    candles = detect_candlestick_patterns(df)
    print("\n--- 2. PATRONES DE VELAS JAPONESAS ---")
    print(f"• Total de patrones detectados en el año: {candles['total_detected']}")
    if candles['latest']:
        print(f"• Último patrón activo: {candles['latest']['pattern']} ({candles['latest']['type']}) el {candles['latest']['date']}")
    else:
        print("• Sin patrón activo en las últimas 5 velas.")
    assert candles["available"] is True, "Detector de velas falló"

    # 3. Test Chart Patterns
    charts = detect_chart_patterns(df)
    print("\n--- 3. PATRONES DE CHARTISMO MULTIBARRA ---")
    print(f"• Total de pivots de swing extraídos: {charts['total_pivots']}")
    if charts['active']:
        print(f"• Patrón activo: {charts['active']['pattern']} -> {charts['active']['direction']}")
    else:
        print("• Sin formación de chartismo confirmada recientemente.")
    assert charts["total_pivots"] > 0, "No se extrajeron pivots"

    print("\n==================================================")
    print("[OK] SUITE TÉCNICA Y PATRONES FUNCIONANDO AL 100%")
    print("==================================================")

if __name__ == "__main__":
    test_technical_suite_live()

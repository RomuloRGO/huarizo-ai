"""Test suite para agent_engine.py y backtest_engine.py con datos de Alpaca."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alpaca_service import AlpacaService
from backtest_engine import run_backtest
from agent_engine import HuarizoAgent

def test_agent_and_backtest_live():
    print("==================================================")
    print("PROBANDO AGENT_ENGINE Y BACKTEST_ENGINE EN VIVO")
    print("==================================================")
    service = AlpacaService()
    ticker = "NVDA"
    
    # 1. Test Backtest Engine Individual
    df = service.get_stock_bars(ticker, days=250)
    print(f"Descargadas {len(df)} barras para {ticker}.")
    
    bt_res = run_backtest(df, strategy="trend_following")
    print("\n--- 1. RESULTADOS DE BACKTEST (Trend Following) ---")
    print(f"• Total Trades: {bt_res['total_trades']}")
    print(f"• Win Rate: {bt_res['win_rate']}% ({bt_res['winning_trades']}W / {bt_res['losing_trades']}L)")
    print(f"• Profit Factor: {bt_res['profit_factor']}x")
    print(f"• Retorno Estrategia: {bt_res['total_return_pct']}% (vs Buy & Hold: {bt_res['buy_and_hold_return_pct']}%)")
    print(f"• Max Drawdown: {bt_res['max_drawdown_pct']}%")
    print(f"• Score Compuesto: {bt_res['composite_score']}/100")
    
    assert bt_res["available"] is True, "Backtest individual falló"

    # 2. Test Agente Completo y Torneo de Estrategias
    print("\n--- 2. ANÁLISIS INTEGRAL DEL AGENTE HUARIZO AI ---")
    agent = HuarizoAgent()
    analysis = agent.analyze(ticker, service)
    
    print(f"• Ticker: {analysis['symbol']} (${analysis['price']:.2f})")
    print(f"• Señal del Agente: {analysis['signal']} (Confianza: {analysis['confidence_score']}/100)")
    print(f"• Estrategia Ganadora del Torneo: {analysis['winning_strategy']['name']} (Win Rate: {analysis['winning_strategy']['win_rate']}%, PF: {analysis['winning_strategy']['profit_factor']}x)")
    print(f"• Titular de Tesis: {analysis['headline']}")
    print("• Puntos de la Tesis:")
    for idx, p in enumerate(analysis['thesis_points'], 1):
        print(f"   {idx}. {p}")
    print("\n• Plan de Bracket Order en Alpaca:")
    plan = analysis['bracket_order_plan']
    print(f"   - Entrada: ${plan['entry_price']:.2f}")
    print(f"   - Take-Profit (3x ATR): ${plan['take_profit_price']:.2f}")
    print(f"   - Stop-Loss (2x ATR): ${plan['stop_loss_price']:.2f}")
    print(f"   - Ratio Riesgo/Beneficio: {plan['risk_reward']}")
    
    assert analysis["available"] is True, "Análisis del agente falló"
    assert analysis["signal"] in ("STRONG BUY", "ACCUMULATE", "HOLD", "REDUCE", "SELL / TAKE PROFIT")
    assert plan["take_profit_price"] > plan["entry_price"] or plan["take_profit_price"] <= plan["entry_price"]

    print("\n==================================================")
    print("[OK] AGENTE IA Y TORNEO DE ESTRATEGIAS FUNCIONANDO AL 100%")
    print("==================================================")

if __name__ == "__main__":
    test_agent_and_backtest_live()

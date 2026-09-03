"""Test suite para alpaca_service.py de Huarizo AI."""

import os
import sys
from unittest.mock import MagicMock, patch

# Asegurar path local
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alpaca_service import AlpacaService


def _svc():
    return AlpacaService(api_key="k", secret_key="s", paper=True)


def _mock_response(status_code=200, json_data=None):
    m = MagicMock()
    m.status_code = status_code
    m.headers = {"Content-Type": "application/json"}
    m.json.return_value = json_data if json_data is not None else {}
    m.text = ""
    return m

def test_service_live():
    print("==================================================")
    print("PROBANDO ALPACASERVICE EN VIVO PARA HUARIZO AI")
    print("==================================================")
    service = AlpacaService()
    
    # 1. Test Cuenta
    acct = service.get_account()
    print(f"1. Cuenta: ID={acct['id']}, Cash=${acct['cash']:,.2f}, Buying Power=${acct['buying_power']:,.2f}, Status={acct['status']}")
    assert acct["status"] == "ACTIVE", "Cuenta no está activa"
    
    # 2. Test Posiciones
    positions = service.get_positions()
    print(f"2. Posiciones abiertas: {len(positions)} encontradas.")
    
    # 3. Test Portfolio History
    history = service.get_portfolio_history(period="1M")
    print(f"3. Historial de Portafolio: {len(history.get('timestamp', []))} puntos retornados.")
    
    # 4. Test Barras OHLCV
    bars = service.get_stock_bars("NVDA", days=100)
    print(f"4. Barras OHLCV (NVDA): {len(bars)} barras descargadas. Último Cierre: ${bars.iloc[-1]['Close']:.2f}")
    assert not bars.empty, "DataFrame de barras está vacío"
    assert "Open" in bars.columns and "Close" in bars.columns and "Volume" in bars.columns
    
    # 5. Test Snapshot
    snap = service.get_stock_snapshot("AAPL")
    print(f"5. Snapshot (AAPL): Precio=${snap['price']:.2f}, Var%={snap['change_pct']}%, Bid=${snap['bid']:.2f}, Ask=${snap['ask']:.2f}")
    assert snap["price"] > 0, "Precio de snapshot inválido"
    
    # 6. Test Noticias Benzinga
    news = service.get_stock_news("NVDA", limit=3)
    print(f"6. Noticias Benzinga (NVDA): {len(news)} artículos. Titular 1: '{news[0]['headline'][:60]}...'")
    assert len(news) > 0, "No se recibieron noticias"
    
    # 7. Test Dividendos Corporate Actions
    divs = service.get_dividends("AAPL", days=365)
    print(f"7. Dividendos Corporate Actions (AAPL): {len(divs)} eventos. Último Rate=${divs[0]['rate']:.2f}/acción ({divs[0]['payable_date']})")
    assert len(divs) > 0, "No se recibieron dividendos para AAPL"
    
    # 8. Test Screener Movers & Most Actives
    movers = service.get_top_movers(top=3)
    print(f"8. Screener Movers: {len(movers['gainers'])} gainers, {len(movers['losers'])} losers.")
    actives = service.get_most_active(top=3)
    print(f"9. Screener Most Active: {len(actives)} tickers.")
    
    print("\n==================================================")
    print("[OK] TODAS LAS PRUEBAS DE ALPACASERVICE PASARON (100% OK)")
    print("==================================================")

if __name__ == "__main__":
    test_service_live()


# ── premium: get_stock_position + get_open_short_call_contracts ────────────────

def test_get_stock_position_suma_qty():
    payload = [{"symbol": "AAPL", "qty": "250"}]
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=payload)):
        assert _svc().get_stock_position("AAPL") == 250.0


def test_get_stock_position_lista_vacia_es_cero():
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=[])):
        assert _svc().get_stock_position("AAPL") == 0.0


def test_get_stock_position_http_error_es_none():
    with patch("alpaca_service.requests.get", return_value=_mock_response(status_code=500)):
        assert _svc().get_stock_position("AAPL") is None


def test_get_stock_position_excepcion_es_none():
    with patch("alpaca_service.requests.get", side_effect=RuntimeError("boom")):
        assert _svc().get_stock_position("AAPL") is None


def test_get_stock_position_ignora_filas_de_opciones():
    payload = [{"symbol": "AAPL260918C00220000", "qty": "-2"}]
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=payload)):
        assert _svc().get_stock_position("AAPL") == 0.0


def test_get_open_short_call_contracts_cuenta_cortos():
    payload = [{"symbol": "AAPL260918C00220000", "qty": "-2"}]
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=payload)):
        assert _svc().get_open_short_call_contracts("AAPL") == 2


def test_get_open_short_call_contracts_ignora_long_put_y_otro_underlying():
    payload = [
        {"symbol": "AAPL260918C00220000", "qty": "1"},
        {"symbol": "AAPL260918P00200000", "qty": "-3"},
        {"symbol": "MSFT260918C00400000", "qty": "-4"},
    ]
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=payload)):
        assert _svc().get_open_short_call_contracts("AAPL") == 0


def test_get_open_short_call_contracts_suma_varios_vencimientos():
    payload = [
        {"symbol": "AAPL260918C00220000", "qty": "-1"},
        {"symbol": "AAPL261016C00225000", "qty": "-3"},
    ]
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=payload)):
        assert _svc().get_open_short_call_contracts("AAPL") == 4


def test_get_open_short_call_contracts_http_error_es_none():
    with patch("alpaca_service.requests.get", return_value=_mock_response(status_code=500)):
        assert _svc().get_open_short_call_contracts("AAPL") is None


def test_get_open_short_call_contracts_excepcion_es_none():
    with patch("alpaca_service.requests.get", side_effect=RuntimeError("boom")):
        assert _svc().get_open_short_call_contracts("AAPL") is None


def test_get_open_short_call_contracts_simbolo_malformado_se_omite():
    payload = [
        {"symbol": "AAPL", "qty": "-2"},
        {"symbol": "XX", "qty": "-1"},
        {"symbol": "AAPL260918C00220000", "qty": "-1"},
    ]
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=payload)):
        assert _svc().get_open_short_call_contracts("AAPL") == 1

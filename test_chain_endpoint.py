"""Test del endpoint de cadena con enriquecimiento multi-vencimiento."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module

client = app_module.app.test_client()


def _contract(expiry, strike, typ, oi=None):
    letter = "C" if typ == "call" else "P"
    occ = f"XYZ{expiry.replace('-', '')}{letter}{int(round(strike * 1000)):08d}"
    return {"occ_symbol": occ, "type": typ, "strike": strike, "expiry": expiry,
            "bid": 1.0, "ask": 1.1, "delta": 0.5, "iv": 0.3, "volume": 100,
            "open_interest": oi}


def test_chain_endpoint_multi_expiry_oi_expiries():
    contracts = []
    for e in ["2026-09-18", "2026-10-16"]:
        for k in [210.0, 215.0]:
            contracts.append(_contract(e, k, "call", oi=42 if k == 215.0 else None))
            # OI=0 en un put del primer vencimiento: 0 NO es None → el vencimiento cuenta.
            contracts.append(_contract(e, k, "put",
                                       oi=(0 if (e == "2026-09-18" and k == 210.0) else None)))
    with patch.object(app_module.alpaca_service, "get_options_chain",
                      return_value=contracts), \
         patch.object(app_module.alpaca_service, "get_stock_snapshot",
                       return_value={"price": 215.0}), \
         patch.object(app_module.alpaca_service, "enrich_chain_with_oi",
                       side_effect=lambda c, spot, n_strikes=5, **kw: c) as m_enrich:
        r = client.get("/api/options/chain/XYZ")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True
    kwargs = m_enrich.call_args.kwargs
    assert kwargs.get("n_strikes") is None
    assert kwargs.get("expiries_limit") == 4
    # Solo el strike 215 tiene OI real en el stub → ese vencimiento cuenta.
    assert body["oi_expiries"] == ["2026-09-18", "2026-10-16"]


def test_chain_endpoint_sin_oi_expiries_vacia():
    contracts = [_contract("2026-09-18", 215.0, "call", oi=None)]
    with patch.object(app_module.alpaca_service, "get_options_chain",
                       return_value=contracts), \
         patch.object(app_module.alpaca_service, "get_stock_snapshot",
                       return_value={"price": 215.0}), \
         patch.object(app_module.alpaca_service, "enrich_chain_with_oi",
                       side_effect=lambda c, spot, n_strikes=5, **kw: c):
        r = client.get("/api/options/chain/XYZ")
    body = r.get_json()
    assert body["success"] is True
    assert body["oi_expiries"] == []

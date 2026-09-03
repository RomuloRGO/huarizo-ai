"""Tests de la capa de servicio de opciones con requests mockeado.

Fixtures con el shape REAL del wire (verificado en vivo contra
/v1beta1/options/snapshots?feed=indicative): los snapshots NO traen 'details';
los hechos del contrato se derivan del simbolo OCC.
"""
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

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


def _snap(**overrides):
    base = {
        "latestQuote": {"bp": 1.44, "ap": 1.50},
        "latestTrade": {"p": 1.47},
        "greeks": {"delta": 0.41, "theta": -0.05},
        "impliedVolatility": 0.28,
        "dayVolume": 1200,
    }
    base.update(overrides)
    return base


SNAPSHOT_BODY = {
    "snapshots": {
        "AAPL260918C00220000": _snap(),
        "AAPL260918P00200000": {**_snap(), "impliedVolatility": 0.31},
        "BADSYM": {"latestQuote": {"bp": 1.0, "ap": 1.1}},  # OCC invalido -> se descarta
    }
}


def test_parse_occ_symbol_formato_estandar_e_invalidos():
    parsed = AlpacaService._parse_occ_symbol("AAPL260918C00220000")
    assert parsed == ("AAPL", "2026-09-18", "call", 220.0)
    put = AlpacaService._parse_occ_symbol("SPY  261218P00450000")
    assert put == ("SPY", "2026-12-18", "put", 450.0)  # root paddeado OCC
    assert AlpacaService._parse_occ_symbol(None) is None
    assert AlpacaService._parse_occ_symbol("") is None
    assert AlpacaService._parse_occ_symbol("BADSYM") is None
    assert AlpacaService._parse_occ_symbol("AAPL260918X00220000") is None  # derecho invalido
    assert AlpacaService._parse_occ_symbol("AAPL260918C00000000") is None  # strike 0


def test_get_options_chain_normaliza_contratos():
    svc = _svc()
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=SNAPSHOT_BODY)) as mg:
        out = svc.get_options_chain("AAPL")
    assert len(out) == 2  # BADSYM se descarta por OCC invalido
    # Orden por (expiry, strike, type): el put strike 200 precede al call strike 220.
    assert [x["type"] for x in out] == ["put", "call"]
    c = next(x for x in out if x["occ_symbol"] == "AAPL260918C00220000")
    assert c["type"] == "call"
    assert c["strike"] == 220.0 and c["expiry"] == "2026-09-18"
    assert c["bid"] >= 0 and c["ask"] > 0
    assert c["iv"] == 0.31 or c["iv"] == 0.28
    url = mg.call_args[0][0]
    assert "/v1beta1/options/snapshots/AAPL" in url


def test_get_options_chain_deriva_hechos_del_simbolo_occ_sin_details():
    body = {"snapshots": {
        "AAPL260918C00220000": {"latestQuote": {"bp": 1.44, "ap": 1.50},
                                 "greeks": {"delta": 0.41}, "impliedVolatility": 0.28},
        "AAPL260918P00200000": {"latestTrade": {"p": 0.58}},
    }}
    svc = _svc()
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=body)):
        out = svc.get_options_chain("AAPL")
    assert len(out) == 2
    call = next(c for c in out if c["occ_symbol"] == "AAPL260918C00220000")
    assert call["type"] == "call" and call["strike"] == 220.0
    assert call["expiry"] == "2026-09-18"
    put = next(c for c in out if c["occ_symbol"] == "AAPL260918P00200000")
    assert put["type"] == "put" and put["ask"] == 0.58  # ask fallback desde trade


def test_get_options_chain_details_camelcase_sobre_escribe_al_occ():
    body = {"snapshots": {
        # OCC embebe strike 230; details camelCase dicen 225 -> details gana.
        "AAPL260918C00230000": {
            "details": {"contractType": "call", "expirationDate": "2026-09-18",
                        "strikePrice": 225.0},
            "latestQuote": {"bp": 2.0, "ap": 2.1},
        },
    }}
    svc = _svc()
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=body)):
        out = svc.get_options_chain("AAPL")
    assert len(out) == 1
    assert out[0]["strike"] == 225.0
    assert out[0]["type"] == "call" and out[0]["expiry"] == "2026-09-18"


def test_get_options_chain_sigue_paginacion_next_page_token():
    page1 = {**SNAPSHOT_BODY, "next_page_token": "TOK1"}
    svc = _svc()
    responses = [
        _mock_response(json_data=page1),
        _mock_response(json_data={"snapshots": {"AAPL261016C00300000": _snap()}}),
    ]
    with patch("alpaca_service.requests.get", side_effect=responses) as mg:
        out = svc.get_options_chain("AAPL")
    assert len(out) == 3  # 2 de pagina 1 + 1 de pagina 2
    assert any(c["occ_symbol"] == "AAPL261016C00300000" for c in out)
    second_url = mg.call_args_list[1][0][0]
    assert "page_token=TOK1" in second_url


def test_get_options_chain_cap_de_5_paginas():
    # Cadenas infinitas: cada respuesta pide otra pagina; el cap debe cortar en 5 llamadas.
    def make_page(token):
        body = {"snapshots": {"AAPL261218C00400000": _snap()}}
        if token < 9:
            body["next_page_token"] = f"T{token + 1}"
        return _mock_response(json_data=body)
    responses = [make_page(i) for i in range(20)]
    svc = _svc()
    with patch("alpaca_service.requests.get", side_effect=responses) as mg:
        out = svc.get_options_chain("AAPL")
    assert mg.call_count == 5
    assert len(out) == 5  # 1 contrato valido por pagina


def test_get_options_chain_iv_snake_case_fallback():
    legacy = {"snapshots": {"AAPL260918C00220000": {**_snap(), "impliedVolatility": None,
                                                    "implied_volatility": 0.25}}}
    svc = _svc()
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=legacy)):
        out = svc.get_options_chain("AAPL")
    assert out[0]["iv"] == 0.25


def test_get_options_chain_acota_la_ventana_de_vencimientos_en_la_url():
    svc = _svc()
    with patch("alpaca_service.requests.get",
               return_value=_mock_response(json_data=SNAPSHOT_BODY)) as mg:
        svc.get_options_chain("AAPL", dte_min=7, dte_max=45, today=date(2026, 8, 28))
    url = mg.call_args[0][0]
    assert "expiration_date_gte=2026-09-04" in url  # hoy + 7
    assert "expiration_date_lte=2026-10-12" in url  # hoy + 45


def test_get_options_chain_sin_ventana_no_filtra_vencimientos():
    svc = _svc()
    with patch("alpaca_service.requests.get",
               return_value=_mock_response(json_data=SNAPSHOT_BODY)) as mg:
        svc.get_options_chain("AAPL")
    url = mg.call_args[0][0]
    assert "expiration_date_gte" not in url and "expiration_date_lte" not in url


def test_get_options_chain_fail_closed_ante_error():
    svc = _svc()
    with patch("alpaca_service.requests.get", return_value=_mock_response(status_code=401)):
        assert svc.get_options_chain("AAPL") == []


def test_get_options_chain_fallo_mitad_de_paginacion_retorna_vacio():
    responses = [
        _mock_response(json_data={"snapshots": {}, "next_page_token": "T2"}),
        _mock_response(status_code=500),
    ]
    svc = _svc()
    with patch("alpaca_service.requests.get", side_effect=responses):
        assert svc.get_options_chain("AAPL") == []


def test_get_option_quote_retorna_mid_por_contrato():
    svc = _svc()
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=SNAPSHOT_BODY)) as mg:
        quotes = svc.get_option_quote(["AAPL260918C00220000"])
    assert quotes["AAPL260918C00220000"] == (1.44 + 1.50) / 2
    url = mg.call_args[0][0]
    assert "symbols=AAPL260918C00220000" in url


def test_get_option_quote_cae_al_last_trade_sin_quote():
    body = {"snapshots": {"X1": {"latestTrade": {"p": 3.25}}}}
    svc = _svc()
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=body)):
        assert svc.get_option_quote(["X1"])["X1"] == 3.25


CONTRACT_BODY = {
    "symbol": "AAPL260918C00220000",
    "root_symbol": "AAPL",
    "open_interest": "17074",   # string en la respuesta real
    "open_interest_date": "2026-08-23",
}


def test_get_option_contract_trae_open_interest_como_int():
    svc = _svc()
    with patch("alpaca_service.requests.get", return_value=_mock_response(json_data=CONTRACT_BODY)) as mg:
        out = svc.get_option_contract("AAPL260918C00220000")
    assert out["open_interest"] == 17074  # convertido desde string
    assert out["open_interest_date"] == "2026-08-23"
    url = mg.call_args[0][0]
    assert "/options/contracts/AAPL260918C00220000" in url


def test_get_option_contract_fail_closed():
    svc = _svc()
    with patch("alpaca_service.requests.get", return_value=_mock_response(status_code=403)):
        assert svc.get_option_contract("X") == {}


def test_enrich_chain_con_oi_solo_strikes_cercanos_al_spot():
    svc = _svc()
    chain = [
        {"occ_symbol": f"C{int(s)}", "type": "call", "strike": s, "expiry": "2026-09-18",
         "bid": 1.0, "ask": 1.1, "volume": 10, "open_interest": None}
        for s in (180.0, 190.0, 200.0, 210.0, 220.0, 230.0, 240.0)
    ]
    responses = [
        _mock_response(json_data={**CONTRACT_BODY, "symbol": f"C{int(s)}",
                                  "open_interest": str(100 + int(s))})
        for s in (210.0, 220.0, 230.0)  # 3 strikes mas cercanos a 218
    ]
    with patch("alpaca_service.requests.get", side_effect=responses):
        out = svc.enrich_chain_with_oi(chain, spot=218.0, n_strikes=3)
    enriched = {c["occ_symbol"]: c["open_interest"] for c in out}
    assert enriched["C210"] == 310 and enriched["C220"] == 320 and enriched["C230"] == 330
    assert enriched["C180"] is None  # lejos del dinero: sin llamada


def test_place_option_order_payload_occ():
    svc = _svc()
    with patch("alpaca_service.requests.post",
               return_value=_mock_response(status_code=201, json_data={"id": "abc"})) as mg:
        res = svc.place_option_order("AAPL260918C00220000", qty=2,
                                     action="buy_to_open", limit_price=1.55)
    assert res["id"] == "abc"
    payload = mg.call_args[1]["json"]
    assert payload["symbol"] == "AAPL260918C00220000"
    assert payload["qty"] == "2"
    assert payload["side"] == "buy"
    assert payload["position_intent"] == "buy_to_open"
    assert payload["type"] == "limit" and payload["limit_price"] == "1.55"
    assert payload["order_class"] == "simple"
    assert payload["time_in_force"] == "day"


def test_place_option_order_rechaza_accion_invalida():
    svc = _svc()
    with pytest.raises(ValueError):
        svc.place_option_order("X1", qty=1, action="sell_to_open")


def test_close_option_position_es_sell_to_close_market():
    svc = _svc()
    svc.get_positions = MagicMock(return_value=[
        {"symbol": "AAPL260918C00220000", "qty": "2"}])
    with patch("alpaca_service.requests.post",
               return_value=_mock_response(status_code=201, json_data={"id": "xyz"})) as mg:
        svc.close_option_position("AAPL260918C00220000", qty=2)
    payload = mg.call_args[1]["json"]
    assert payload["side"] == "sell"
    assert payload["position_intent"] == "sell_to_close"
    assert payload["type"] == "market"
    assert payload["qty"] == "2"  # dentro de la tenida: sin clampeo


def test_get_order_por_id():
    svc = _svc()
    with patch("alpaca_service.requests.get",
               return_value=_mock_response(json_data={"id": "abc", "status": "filled"})):
        assert svc.get_order("abc")["status"] == "filled"


def test_place_option_order_error_http_lanza_runtimeerror():
    svc = _svc()
    with patch("alpaca_service.requests.post",
               return_value=_mock_response(status_code=422)):
        try:
            svc.place_option_order("X1", qty=1, action="buy_to_open")
            raised = False
        except RuntimeError:
            raised = True
        assert raised


def _oi_batch_body(contracts, oi):
    """Respuesta de /v2/options/contracts en lote (OI como string, igual que la API)."""
    return {
        "option_contracts": [
            {"symbol": c["occ_symbol"], "open_interest": str(oi),
             "open_interest_date": "2026-08-26"}
            for c in contracts
        ],
        "next_page_token": None,
    }


def test_enrich_limita_llamadas_al_vencimiento_mas_cercano():
    svc = _svc()
    # Dos expiraciones x 3 strikes x 2 tipos = 12 contratos;
    # solo el vencimiento 2026-09-18 (mas cercano) con top-2 strikes -> 1 llamada en lote
    chain = []
    for exp in ("2026-09-18", "2026-12-18"):
        for s in (200.0, 210.0, 220.0):
            for t in ("call", "put"):
                suffix = "C" if t == "call" else "P"
                chain.append({"occ_symbol": f"X{exp.replace('-', '')}{suffix}{int(s)*1000:08d}",
                              "type": t, "strike": s, "expiry": exp,
                              "bid": 1.0, "ask": 1.1, "volume": 0, "open_interest": None})
    objetivo = [c for c in chain
                if c["expiry"] == "2026-09-18" and c["strike"] in (210.0, 220.0)]
    mock_get = MagicMock(return_value=_mock_response(json_data=_oi_batch_body(objetivo, 500)))
    with patch("alpaca_service.requests.get", mock_get):
        out = svc.enrich_chain_with_oi(chain, spot=215.0, n_strikes=2)
    enriched_near = [c for c in out if c["expiry"] == "2026-09-18"
                     and c["strike"] in (210.0, 220.0)]
    assert len(enriched_near) == 4 and all(c["open_interest"] == 500 for c in enriched_near)
    far = [c for c in out if c["open_interest"] is None]
    assert len(far) == 8  # nada del vencimiento lejano fue tocado
    # Regresion: contrato-por-contrato serian 4+ llamadas; el lote resuelve en 1
    assert mock_get.call_count == 1
    params = mock_get.call_args.kwargs["params"]
    assert params["expiration_date"] == "2026-09-18"
    assert params["strike_price_gte"] == "210.0" and params["strike_price_lte"] == "220.0"


def test_enrich_cae_a_contrato_por_contrato_si_el_lote_falla():
    svc = _svc()
    chain = [
        {"occ_symbol": f"X20260918{s_suffix}{int(s)*1000:08d}", "type": t, "strike": s,
         "expiry": "2026-09-18", "bid": 1.0, "ask": 1.1, "volume": 0, "open_interest": None}
        for s in (210.0, 220.0)
        for t, s_suffix in (("call", "C"), ("put", "P"))
    ]
    # Primera respuesta: lote vacio (fail-closed) -> fallback por contrato.
    responses = [_mock_response(json_data={"option_contracts": [], "next_page_token": None})]
    responses += [
        _mock_response(json_data={**CONTRACT_BODY, "symbol": c["occ_symbol"],
                                  "open_interest": "310"})
        for c in chain
    ]
    mock_get = MagicMock(side_effect=responses)
    with patch("alpaca_service.requests.get", mock_get):
        out = svc.enrich_chain_with_oi(chain, spot=215.0, n_strikes=2)
    assert all(c["open_interest"] == 310 for c in out)
    assert mock_get.call_count == 5  # 1 lote fallido + 4 individuales


def test_enrich_usa_ventana_dte_no_el_vencimiento_absoluto_mas_cercano():
    svc = _svc()
    # 2026-08-28 es el vencimiento mas cercano (DTE 0) pero fuera de la ventana
    # [7,45]; 2026-09-04 tiene DTE 7 -> es el objetivo de enriquecimiento.
    chain = []
    for exp in ("2026-08-28", "2026-09-04"):
        for s in (210.0, 220.0):
            for t in ("call", "put"):
                suffix = "C" if t == "call" else "P"
                chain.append({"occ_symbol": f"X{exp.replace('-', '')}{suffix}{int(s)*1000:08d}",
                              "type": t, "strike": s, "expiry": exp,
                              "bid": 1.0, "ask": 1.1, "volume": 0, "open_interest": None})
    objetivo = [c for c in chain if c["expiry"] == "2026-09-04"]
    mock_get = MagicMock(return_value=_mock_response(json_data=_oi_batch_body(objetivo, 640)))
    with patch("alpaca_service.requests.get", mock_get):
        out = svc.enrich_chain_with_oi(
            chain, spot=215.0, n_strikes=2,
            dte_min=7, dte_max=45, today=date(2026, 8, 28))
    enriched = [c for c in out if c["open_interest"] == 640]
    assert len(enriched) == 4  # solo el vencimiento dentro de la ventana
    assert all(c["expiry"] == "2026-09-04" for c in enriched)
    # El DTE 0 NO se toca: el lote se pide para 2026-09-04
    assert mock_get.call_args.kwargs["params"]["expiration_date"] == "2026-09-04"


def test_place_option_order_rechaza_limit_price_cero_o_negativa():
    svc = _svc()
    with pytest.raises(ValueError):
        svc.place_option_order("X1", qty=1, action="buy_to_open", limit_price=0)


def test_close_option_position_clampea_a_posicion_tenida():
    svc = _svc()
    svc.get_positions = MagicMock(return_value=[
        {"symbol": "AAPL260918C00220000", "qty": "1"}])
    with patch("alpaca_service.requests.post",
               return_value=_mock_response(status_code=201, json_data={"id": "ok"})) as mg:
        svc.close_option_position("AAPL260918C00220000", qty=5)
    assert mg.call_args[1]["json"]["qty"] == "1"


def test_close_option_position_sin_posicion_lanza_valueerror():
    svc = _svc()
    svc.get_positions = MagicMock(return_value=[])
    with pytest.raises(ValueError):
        svc.close_option_position("AAPL260918C00220000", qty=1)

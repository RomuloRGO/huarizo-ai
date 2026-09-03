"""Cliente Unificado de Servicios Alpaca para Huarizo AI.

Integra en una sola clase limpia y tipada:
  1. Trading API (Cuentas, Posiciones, Órdenes Bracket y Portfolio History).
  2. Market Data v2 (Barras Históricas OHLCV, Snapshots en Vivo y Spreads).
  3. News API v1beta1 (Noticias en tiempo real de Benzinga).
  4. Corporate Actions v1 (Dividendos en efectivo y eventos corporativos).
  5. Screener v1beta1 (Top Movers Gainers/Losers y Most Active).
  6. Opciones v1beta1 (Cadena de snapshots, quotes multi-simbolo, feed indicativo).
"""

import os
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv

# Cargar variables de entorno locales
load_dotenv()


class AlpacaService:
    """Cliente unificado para interactuar con todos los endpoints de Alpaca Markets."""

    def __init__(self, api_key: Optional[str] = None, secret_key: Optional[str] = None, paper: bool = True):
        self.api_key = api_key or os.getenv("APCA_API_KEY_ID", "")
        self.secret_key = secret_key or os.getenv("APCA_API_SECRET_KEY", "")
        self.paper = paper if paper is not None else os.getenv("APCA_PAPER", "True").lower() == "true"

        self.trading_base_url = "https://paper-api.alpaca.markets/v2" if self.paper else "https://api.alpaca.markets/v2"
        self.data_base_url = "https://data.alpaca.markets/v2"
        self.data_v1_base_url = "https://data.alpaca.markets/v1"
        self.data_v1beta1_base_url = "https://data.alpaca.markets/v1beta1"

        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "accept": "application/json",
            "Content-Type": "application/json"
        }

    # ── 1. GESTIÓN DE CUENTA Y PORTAFOLIO ───────────────────────────────────

    def get_account(self) -> Dict[str, Any]:
        """Obtiene información financiera y estado de la cuenta."""
        url = f"{self.trading_base_url}/account"
        r = requests.get(url, headers=self.headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {
                "id": data.get("id"),
                "account_number": data.get("account_number"),
                "status": data.get("status"),
                "currency": data.get("currency", "USD"),
                "cash": float(data.get("cash", 0.0)),
                "portfolio_value": float(data.get("portfolio_value", 0.0)),
                "buying_power": float(data.get("buying_power", 0.0)),
                "regt_buying_power": float(data.get("regt_buying_power", 0.0)),
                "daytrading_buying_power": float(data.get("daytrading_buying_power", 0.0)),
                "equity": float(data.get("equity", 0.0)),
                "last_equity": float(data.get("last_equity", 0.0)),
                "daytrade_count": data.get("daytrade_count", 0),
                "is_paper": self.paper,
                # Campos que exige la validación de cuenta de concurso
                # (account_onboarding). Sin ellos no se puede afirmar ni que la
                # cuenta está vacía (frescura) ni que puede operar opciones.
                "long_market_value": float(data.get("long_market_value", 0.0)),
                "short_market_value": float(data.get("short_market_value", 0.0)),
                "options_approved_level": data.get("options_approved_level"),
                "options_trading_level": data.get("options_trading_level"),
                # El risk gate lo usa como freno duro para ordenes de opciones.
                # Sin el, `evaluate_new_entry` falla cerrado con
                # `options_buying_power_unknown` y el bot deja de operar.
                "options_buying_power": float(data.get("options_buying_power", 0.0)),
                "account_blocked": bool(data.get("account_blocked", False)),
                "trading_blocked": bool(data.get("trading_blocked", False)),
            }
        raise RuntimeError(f"Error fetching account from Alpaca: {r.status_code} - {r.text}")

    def get_positions(self) -> List[Dict[str, Any]]:
        """Obtiene la lista de posiciones abiertas actualmente."""
        url = f"{self.trading_base_url}/positions"
        r = requests.get(url, headers=self.headers, timeout=10)
        if r.status_code == 200:
            positions = []
            for p in r.json():
                positions.append({
                    "symbol": p.get("symbol"),
                    "qty": float(p.get("qty", 0.0)),
                    "side": p.get("side"),
                    "market_value": float(p.get("market_value", 0.0)),
                    "cost_basis": float(p.get("cost_basis", 0.0)),
                    "avg_entry_price": float(p.get("avg_entry_price", 0.0)),
                    "current_price": float(p.get("current_price", 0.0)),
                    "lastday_price": float(p.get("lastday_price", 0.0)),
                    "change_today": float(p.get("change_today", 0.0)),
                    "unrealized_pl": float(p.get("unrealized_pl", 0.0)),
                    "unrealized_plpc": float(p.get("unrealized_plpc", 0.0)) * 100.0,
                })
            return positions
        return []

    def _raw_positions(self) -> Optional[List[Dict[str, Any]]]:
        """Raw Alpaca positions. None means the read failed; [] means no positions."""
        url = f"{self.trading_base_url}/positions"
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else None
            return None
        except Exception:
            return None

    def get_stock_position(self, symbol: str) -> Optional[float]:
        raw = self._raw_positions()
        if raw is None:
            return None
        want = str(symbol or "").upper()
        total = 0.0
        for p in raw:
            row_sym = str(p.get("symbol") or "").upper()
            if row_sym != want:
                continue
            if self._parse_occ_symbol(p.get("symbol") or "") is not None:
                continue
            try:
                total += float(p.get("qty", 0.0))
            except (TypeError, ValueError):
                continue
        return total

    def get_open_short_call_contracts(self, symbol: str) -> Optional[int]:
        # OCC parsing is required because get_positions does not expose asset_class.
        raw = self._raw_positions()
        if raw is None:
            return None
        want = str(symbol or "").upper()
        total = 0
        for p in raw:
            parsed = self._parse_occ_symbol(p.get("symbol") or "")
            if parsed is None:
                continue
            root, _expiry, right, _strike = parsed
            if root.upper() != want or right != "call":
                continue
            try:
                qty = int(float(p.get("qty", 0)))
            except (TypeError, ValueError):
                continue
            if qty < 0:
                total += abs(qty)
        return total

    def get_portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> Dict[str, Any]:
        """Obtiene la curva histórica de valor del portafolio (para gráficos de patrimonio)."""
        url = f"{self.trading_base_url}/account/portfolio/history?period={period}&timeframe={timeframe}"
        r = requests.get(url, headers=self.headers, timeout=10)
        if r.status_code == 200:
            return r.json()
        return {"timestamp": [], "equity": [], "profit_loss": [], "profit_loss_pct": []}

    # ── 2. DATOS DE MERCADO (OHLCV, SNAPSHOTS Y SPREADS) ─────────────────────

    def get_stock_bars(self, ticker: str, days: int = 250, timeframe: str = "1Day") -> pd.DataFrame:
        """Obtiene el historial de barras OHLCV desde Alpaca Market Data v2."""
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        url = f"{self.data_base_url}/stocks/bars?symbols={ticker}&timeframe={timeframe}&start={start_date}&limit=1000&feed=iex"
        r = requests.get(url, headers=self.headers, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
            raw_bars = data.get("bars", {}).get(ticker, [])
            if not raw_bars:
                return pd.DataFrame()
            
            df = pd.DataFrame(raw_bars)
            df["t"] = pd.to_datetime(df["t"])
            df.rename(columns={
                "o": "Open",
                "h": "High",
                "l": "Low",
                "c": "Close",
                "v": "Volume",
                "vw": "VWAP"
            }, inplace=True)
            df.sort_values("t", inplace=True)
            df.reset_index(drop=True, inplace=True)
            return df
        return pd.DataFrame()

    def get_stock_snapshot(self, ticker: str) -> Dict[str, Any]:
        """Obtiene la cotización instantánea, bid/ask spread y cambio del día."""
        url = f"{self.data_base_url}/stocks/snapshots?symbols={ticker}&feed=iex"
        r = requests.get(url, headers=self.headers, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
            snap = data.get(ticker, {})
            latest_trade = snap.get("latestTrade", {})
            latest_quote = snap.get("latestQuote", {})
            daily_bar = snap.get("dailyBar", {})
            prev_daily = snap.get("prevDailyBar", {})

            price = float(latest_trade.get("p", 0.0)) or float(daily_bar.get("c", 0.0))
            prev_close = float(prev_daily.get("c", price))
            change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0

            return {
                "symbol": ticker,
                "price": price,
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2),
                "open": float(daily_bar.get("o", 0.0)),
                "high": float(daily_bar.get("h", 0.0)),
                "low": float(daily_bar.get("l", 0.0)),
                "volume": int(daily_bar.get("v", 0)),
                "vwap": float(daily_bar.get("vw", 0.0)),
                "bid": float(latest_quote.get("bp", 0.0)),
                "ask": float(latest_quote.get("ap", 0.0)),
                "bid_size": int(latest_quote.get("bs", 0)),
                "ask_size": int(latest_quote.get("as", 0)),
                "timestamp": latest_trade.get("t") or daily_bar.get("t")
            }
        return {"symbol": ticker, "price": 0.0, "change_pct": 0.0}

    # ── 3. NOTICIAS Y CORPORATE ACTIONS (DIVIDENDOS) ─────────────────────────

    def get_stock_news(self, ticker: Optional[str] = None, limit: int = 6) -> List[Dict[str, Any]]:
        """Obtiene noticias en tiempo real de Benzinga a través de Alpaca News API."""
        symbol_param = f"&symbols={ticker}" if ticker else ""
        url = f"{self.data_v1beta1_base_url}/news?limit={limit}{symbol_param}"
        r = requests.get(url, headers=self.headers, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
            articles = []
            for n in data.get("news", []):
                articles.append({
                    "id": n.get("id"),
                    "headline": n.get("headline"),
                    "summary": n.get("summary"),
                    "author": n.get("author"),
                    "created_at": n.get("created_at"),
                    "url": n.get("url"),
                    "symbols": n.get("symbols", []),
                    "source": n.get("source", "Benzinga")
                })
            return articles
        return []

    def get_dividends(self, ticker: str, days: int = 365) -> List[Dict[str, Any]]:
        """Obtiene el historial de dividendos en efectivo desde Alpaca Corporate Actions."""
        start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        url = f"{self.data_v1_base_url}/corporate-actions?types=cash_dividend&symbols={ticker}&start={start_date}"
        r = requests.get(url, headers=self.headers, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
            raw_divs = data.get("corporate_actions", {}).get("cash_dividends", [])
            dividends = []
            for d in raw_divs:
                dividends.append({
                    "id": d.get("id"),
                    "symbol": d.get("symbol"),
                    "rate": float(d.get("rate", 0.0)),
                    "ex_date": d.get("ex_date"),
                    "record_date": d.get("record_date"),
                    "payable_date": d.get("payable_date"),
                    "special": d.get("special", False)
                })
            dividends.sort(key=lambda x: x.get("payable_date") or x.get("ex_date") or "", reverse=True)
            return dividends
        return []

    # ── 4. SCREENER DE MERCADO (TOP GAINERS, LOSERS & MOST ACTIVE) ───────────

    def get_top_movers(self, top: int = 10) -> Dict[str, List[Dict[str, Any]]]:
        """Obtiene las acciones con mayores ganancias y pérdidas en vivo."""
        url = f"{self.data_v1beta1_base_url}/screener/stocks/movers?top={top}"
        r = requests.get(url, headers=self.headers, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
            gainers = [{
                "symbol": m.get("symbol"),
                "price": float(m.get("price", 0.0)),
                "change": float(m.get("change", 0.0)),
                "percent_change": float(m.get("percent_change", 0.0))
            } for m in data.get("gainers", [])]

            losers = [{
                "symbol": m.get("symbol"),
                "price": float(m.get("price", 0.0)),
                "change": float(m.get("change", 0.0)),
                "percent_change": float(m.get("percent_change", 0.0))
            } for m in data.get("losers", [])]

            return {"gainers": gainers, "losers": losers}
        return {"gainers": [], "losers": []}

    def get_most_active(self, by: str = "volume", top: int = 10) -> List[Dict[str, Any]]:
        """Obtiene las acciones con mayor volumen transaccional en la sesión."""
        url = f"{self.data_v1beta1_base_url}/screener/stocks/most-actives?by={by}&top={top}"
        r = requests.get(url, headers=self.headers, timeout=10)
        
        if r.status_code == 200:
            data = r.json()
            return [{
                "symbol": a.get("symbol"),
                "volume": int(a.get("volume", 0)),
                "trades": int(a.get("trade_count", 0))
            } for a in data.get("most_actives", [])]
        return []

    # ── 5. EJECUCIÓN PROFESIONAL DE BRACKET ORDERS ───────────────────────────

    def place_bracket_order(
        self,
        ticker: str,
        qty: float,
        side: str = "buy",
        take_profit_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        order_type: str = "market",
        time_in_force: str = "gtc",
        client_order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Envía una Bracket Order compuesta (Entrada + Take-Profit + Stop-Loss automático).

        `client_order_id` es la llave de idempotencia: Alpaca rechaza una orden
        que repita un id ya usado, lo que hace imposible el duplicado accidental
        incluso si la capa de decisión falla. Se añadió tras el incidente del
        2026-08-31 (180 órdenes en 18 segundos sobre 3 símbolos).
        """
        url = f"{self.trading_base_url}/orders"

        payload: Dict[str, Any] = {
            "symbol": ticker.upper(),
            "qty": str(qty),
            "side": side.lower(),
            "type": order_type.lower(),
            "time_in_force": time_in_force.lower(),
        }

        if client_order_id:
            payload["client_order_id"] = str(client_order_id)[:48]

        # Configurar Bracket si se proporcionan Take-Profit y Stop-Loss
        if take_profit_price and stop_loss_price:
            payload["order_class"] = "bracket"
            payload["take_profit"] = {"limit_price": str(round(take_profit_price, 2))}
            payload["stop_loss"] = {"stop_price": str(round(stop_loss_price, 2))}
        else:
            payload["order_class"] = "simple"

        r = requests.post(url, headers=self.headers, json=payload, timeout=10)
        if r.status_code in (200, 201):
            return r.json()
        raise RuntimeError(f"Error placing bracket order: {r.status_code} - {r.text}")

    def get_orders(self, status: str = "open") -> List[Dict[str, Any]]:
        """Obtiene la lista de órdenes activas o históricas."""
        url = f"{self.trading_base_url}/orders?status={status}"
        r = requests.get(url, headers=self.headers, timeout=10)
        if r.status_code == 200:
            return r.json()
        return []

    def cancel_order(self, order_id: str) -> bool:
        """Cancela una orden pendiente."""
        url = f"{self.trading_base_url}/orders/{order_id}"
        r = requests.delete(url, headers=self.headers, timeout=10)
        return r.status_code in (200, 204)

    # ── 6. OPCIONES: CADENA, QUOTES Y CONTRATOS ──────────────────────────────

    def get_options_chain(
        self,
        underlying: str,
        dte_min: Optional[int] = None,
        dte_max: Optional[int] = None,
        today: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Snapshot de cadena de opciones normalizado (feed indicativo en plan free).

        Sigue next_page_token hasta 5 paginas (hard cap anti-loop).
        Contratos con: occ_symbol, type(call/put), strike, expiry(YYYY-MM-DD),
        bid, ask, delta, theta, iv, volume.
        Con dte_min/dte_max, acota los vencimientos server-side
        (expiration_date_gte/lte) para no descargar la cadena completa.
        Fail-closed estricto: ante cualquier error HTTP o excepcion retorna [].
        """
        underlying = underlying.upper()
        window = ""
        if dte_min is not None and dte_max is not None:
            t0 = today or datetime.now().date()
            gte = (t0 + timedelta(days=int(dte_min))).strftime("%Y-%m-%d")
            lte = (t0 + timedelta(days=int(dte_max))).strftime("%Y-%m-%d")
            window = f"&expiration_date_gte={gte}&expiration_date_lte={lte}"
        contracts: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        try:
            for _page in range(5):
                url = (f"{self.data_v1beta1_base_url}/options/snapshots/{underlying}"
                       f"?limit=1000&feed=indicative{window}")
                if page_token:
                    url += f"&page_token={page_token}"
                r = requests.get(url, headers=self.headers, timeout=15)
                if r.status_code != 200:
                    print(f"[options_chain] HTTP {r.status_code} para {underlying}")
                    return []
                data = r.json() or {}
                snaps = data.get("snapshots", {}) or {}
                for occ, snap in snaps.items():
                    parsed = self._parse_option_snapshot(occ, snap)
                    if parsed:
                        contracts.append(parsed)
                page_token = data.get("next_page_token")
                if not page_token:
                    break
            if page_token:
                print(f"[options_chain] truncado en 5 paginas para {underlying}")
        except Exception as e:
            print(f"[options_chain] Error para {underlying}: {e}")
            return []
        contracts.sort(key=lambda c: (c["expiry"], c["strike"], c["type"]))
        return contracts

    @staticmethod
    def _parse_occ_symbol(occ: str):
        """Parsea un simbolo OCC estandar: ROOT + YYMMDD + C/P + strike(8 digitos, miles).

        Ej: 'AAPL260918C00220000' -> ('AAPL', '2026-09-18', 'call', 220.0)
        Retorna None si no cumple el formato de 15+ caracteres esperado.
        """
        if not occ or len(occ) < 15:
            return None
        try:
            strike = int(occ[-8:]) / 1000.0
            right = occ[-9]
            yymmdd = occ[-15:-9]
            root = occ[:-15].strip()
            if right not in ("C", "P") or len(yymmdd) != 6 or not yymmdd.isdigit():
                return None
            if not root or not strike > 0:
                return None
            expiry = "20" + yymmdd[:2] + "-" + yymmdd[2:4] + "-" + yymmdd[4:6]
            return root, datetime.strptime(expiry, "%Y-%m-%d").strftime("%Y-%m-%d"), (
                "call" if right == "C" else "put"), float(strike)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_option_snapshot(occ: str, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normaliza un snapshot individual de contrato. None si esta incompleto.

        Los hechos del contrato (type, expiry, strike) se derivan PRIMERO del simbolo
        OCC (fuente de verdad: los snapshots reales NO traen 'details'). Si hay
        'details' camelCase presente, esos valores tienen prioridad sobre el OCC;
        snake_case queda como ultimo recurso de compatibilidad.
        """
        if not isinstance(snap, dict):
            return None
        ctype = expiry = None
        strike_raw = None
        parsed_occ = AlpacaService._parse_occ_symbol(occ) if occ else None
        if parsed_occ:
            _, expiry, ctype, strike_raw = parsed_occ
        details = snap.get("details", {}) or {}
        ctype = details.get("contractType") or details.get("contract_type") \
            or details.get("type") or ctype
        expiry = details.get("expirationDate") or details.get("expiration_date") or expiry
        if details.get("strikePrice") is not None:
            strike_raw = details["strikePrice"]
        elif details.get("strike_price") is not None:
            strike_raw = details["strike_price"]
        if not occ or not ctype or not expiry or strike_raw is None:
            return None
        quote = snap.get("latestQuote", {}) or {}
        trade = snap.get("latestTrade", {}) or {}
        greeks = snap.get("greeks", {}) or {}
        try:
            strike = float(strike_raw)
            bid = float(quote.get("bp", 0.0) or 0.0)
            ask = float(quote.get("ap", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if ask <= 0 and trade.get("p"):
            try:
                ask = float(trade["p"])
            except (TypeError, ValueError):
                pass
        iv_val = snap.get("impliedVolatility")
        if iv_val is None:
            iv_val = snap.get("implied_volatility")
        volume = 0
        raw_vol = snap.get("dayVolume", 0)
        try:
            volume = int(float(raw_vol))
        except (TypeError, ValueError):
            volume = 0
        return {
            "occ_symbol": occ,
            "type": str(ctype).lower(),
            "strike": strike,
            "expiry": str(expiry)[:10],
            "bid": bid,
            "ask": ask,
            "delta": greeks.get("delta"),
            "theta": greeks.get("theta"),
            "iv": iv_val,
            "volume": volume,
            "open_interest": None,  # se enriquece aparte via /v2/options/contracts
        }

    def get_option_quote(self, occ_symbols: List[str]) -> Dict[str, float]:
        """Mid actual por contrato (multi-simbolo, 1 call). Fallback: latestTrade.p."""
        if not occ_symbols:
            return {}
        symbols_param = ",".join(s.upper() for s in occ_symbols)
        url = f"{self.data_v1beta1_base_url}/options/snapshots?symbols={symbols_param}&feed=indicative"
        out: Dict[str, float] = {}
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code != 200:
                return out
            snaps = r.json().get("snapshots", {}) or {}
            for occ, snap in snaps.items():
                if not isinstance(snap, dict):
                    continue
                quote = snap.get("latestQuote", {}) or {}
                trade = snap.get("latestTrade", {}) or {}
                bp = ap = 0.0
                try:
                    bp = float(quote.get("bp", 0.0) or 0.0)
                    ap = float(quote.get("ap", 0.0) or 0.0)
                except (TypeError, ValueError):
                    pass
                if bp > 0 and ap > 0:
                    out[occ] = round((bp + ap) / 2.0, 4)
                elif trade.get("p"):
                    try:
                        out[occ] = float(trade["p"])
                    except (TypeError, ValueError):
                        pass
            return out
        except Exception as e:
            print(f"[option_quote] Error: {e}")
            return out

    def get_option_contract(self, occ_symbol: str) -> Dict[str, Any]:
        """Detalle de contrato con open_interest (lag EOD del OCC). Fail-closed: {}."""
        url = f"{self.trading_base_url}/options/contracts/{occ_symbol.upper()}"
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code != 200:
                print(f"[option_contract] HTTP {r.status_code} para {occ_symbol}")
                return {}
            d = r.json() or {}
            raw_oi = d.get("open_interest")
            try:
                oi = int(float(raw_oi)) if raw_oi is not None else 0
            except (TypeError, ValueError):
                oi = 0
            return {
                "symbol": d.get("symbol"),
                "open_interest": oi,
                "open_interest_date": d.get("open_interest_date"),
            }
        except Exception as e:
            print(f"[option_contract] Error: {e}")
            return {}

    def get_option_contracts_oi(
        self,
        underlying: str,
        expiry: str,
        strike_min: float,
        strike_max: float,
    ) -> Dict[str, int]:
        """Open interest en lote para un vencimiento y rango de strikes.

        Una sola llamada HTTP (paginada, cap 3) en vez de una por contrato.
        Retorna {occ_symbol: open_interest}. Fail-closed: {} ante cualquier error.
        """
        url = f"{self.trading_base_url}/options/contracts"
        out: Dict[str, int] = {}
        page_token: Optional[str] = None
        try:
            for _page in range(3):
                params = {
                    "underlying_symbols": underlying.upper(),
                    "expiration_date": expiry,
                    "strike_price_gte": str(strike_min),
                    "strike_price_lte": str(strike_max),
                    "limit": "100",
                }
                if page_token:
                    params["page_token"] = page_token
                r = requests.get(url, headers=self.headers, params=params, timeout=15)
                if r.status_code != 200:
                    print(f"[options_oi] HTTP {r.status_code} para {underlying} {expiry}")
                    return {}
                d = r.json() or {}
                for c in (d.get("option_contracts") or []):
                    sym = c.get("symbol")
                    if not sym:
                        continue
                    try:
                        out[sym] = int(float(c.get("open_interest")))
                    except (TypeError, ValueError):
                        out[sym] = 0
                page_token = d.get("next_page_token")
                if not page_token:
                    break
            return out
        except Exception as e:
            print(f"[options_oi] Error para {underlying} {expiry}: {e}")
            return {}

    def enrich_chain_with_oi(
        self,
        contracts: List[Dict[str, Any]],
        spot: float,
        n_strikes: Optional[int] = 5,
        dte_min: Optional[int] = None,
        dte_max: Optional[int] = None,
        today: Optional[Any] = None,
        expiries_limit: int = 1,
        dte_sweet_min: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Enriquece con OI hasta `expiries_limit` vencimientos (los mas
        cercanos dentro de la ventana DTE dte_min/dte_max) y sus strikes mas
        proximos al spot, o el rango COMPLETO de strikes si n_strikes es None.

        Presupuesto duro: 1 llamada paginada (cap 3 paginas x 100) por
        vencimiento. El fallback contrato-por-contrato (presupuesto 2 x
        n_strikes por vencimiento) SOLO aplica cuando n_strikes es numerico;
        con n_strikes=None no hay fallback (el rango completo lo haria
        ilimitado). El resto queda open_interest None.

        Si solo uno de los dos limites DTE esta definido, no se filtra (compat).
        """
        if not contracts:
            return contracts
        try:
            spot_f = float(spot)
        except (TypeError, ValueError):
            return contracts
        if spot_f <= 0:
            return contracts
        try:
            limit_i = max(1, int(expiries_limit))
        except (TypeError, ValueError):
            limit_i = 1
        t0 = today or datetime.now().date()
        # Expirations with their DTE; ISO strings sort chronologically.
        exp_dtes = {}
        for c in contracts:
            e = str(c.get("expiry") or "")[:10]
            if not e:
                continue
            try:
                d = datetime.strptime(e, "%Y-%m-%d").date()
            except ValueError:
                continue
            exp_dtes.setdefault(e, (d - t0).days)
        if not exp_dtes:
            return contracts
        if dte_min is not None and dte_max is not None:
            in_window = {e for e, dt in exp_dtes.items() if dte_min <= dt <= dte_max}
            if in_window:
                exp_dtes = {e: dt for e, dt in exp_dtes.items() if e in in_window}
        expiries_objetivo = sorted(exp_dtes)[:limit_i]
        # Con `expiries_limit=1` solo se enriquece el vencimiento MAS CERCANO de
        # la ventana. Como `volume` viene siempre en 0 en el plan de datos
        # gratuito, el filtro de liquidez del motor (vol > 0 or oi >= 100)
        # deja pasar unicamente ese vencimiento: el selector quedaba
        # ESTRUCTURALMENTE obligado a operar ~7 DTE por mucho que preferiera
        # otro tramo. Medido en vivo el 2026-09-02: los 8 simbolos del universo
        # caian en 7 DTE con un theta de 9-10% de la prima por dia.
        # Con `dte_sweet_min` se añade ademas el primer vencimiento que alcanza
        # el tramo dulce, para que la preferencia de DTE (R1) y la estructura
        # de plazos (R5, que necesita DOS tramos) tengan donde elegir.
        # Default None => cero cambio de comportamiento para quien no lo pida.
        if dte_sweet_min is not None:
            try:
                sweet = int(dte_sweet_min)
            except (TypeError, ValueError):
                sweet = 0
            if sweet > 0:
                for e in sorted(exp_dtes):
                    if exp_dtes[e] >= sweet:
                        if e not in expiries_objetivo:
                            expiries_objetivo.append(e)
                        break
        for expiry_objetivo in expiries_objetivo:
            strikes_validos = []
            for c in contracts:
                if str(c.get("expiry") or "")[:10] != expiry_objetivo:
                    continue
                try:
                    strikes_validos.append(float(c.get("strike")))
                except (TypeError, ValueError):
                    continue
            if not strikes_validos:
                continue
            if n_strikes is None:
                strikes_cercanos = sorted(set(strikes_validos))
            else:
                n_strikes_i = max(1, int(n_strikes))
                strikes_cercanos = sorted(
                    set(strikes_validos),
                    key=lambda s: abs(s - spot_f),
                )[:n_strikes_i]
            objetivo = []
            for c in contracts:
                try:
                    strike = float(c.get("strike"))
                except (TypeError, ValueError):
                    continue
                if (str(c.get("expiry") or "")[:10] == expiry_objetivo
                        and strike in strikes_cercanos):
                    objetivo.append(c)
            if not objetivo:
                continue
            root = None
            parsed = self._parse_occ_symbol(str(objetivo[0].get("occ_symbol") or ""))
            if parsed:
                root = parsed[0]
            # Sin root parseable no hay llamada batched (paridad con el
            # comportamiento previo): mapa vacio y cae al fallback.
            oi_map: Dict[str, int] = {}
            if root:
                oi_map = self.get_option_contracts_oi(
                    root, expiry_objetivo, min(strikes_cercanos), max(strikes_cercanos))
            if oi_map:
                for c in objetivo:
                    sym = str(c.get("occ_symbol") or "").upper()
                    if sym in oi_map:
                        c["open_interest"] = oi_map[sym]
                continue
            if n_strikes is None:
                if not root:
                    print(f"[options_oi] root no parseable para {expiry_objetivo}; sin enriquecimiento")
                continue  # presupuesto duro: sin fallback en modo rango completo
            # Fallback contrato por contrato con presupuesto duro de llamadas.
            presupuesto = 2 * max(1, int(n_strikes))
            for c in objetivo[:presupuesto]:
                info = self.get_option_contract(c.get("occ_symbol", ""))
                if info:
                    c["open_interest"] = info.get("open_interest", 0)
        return contracts

    def place_option_order(
        self,
        occ_symbol: str,
        qty: int,
        action: str = "buy_to_open",
        limit_price: Optional[float] = None,
        time_in_force: str = "day",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Orden de opciones OCC (buy_to_open / sell_to_close).

        Sin brackets: Alpaca no soporta order_class=bracket para OCC.
        Lanza RuntimeError ante rechazo HTTP de la API (excepciones de red
        se propagan crudas, mismo comportamiento que place_bracket_order).

        `client_order_id` viaja tal cual lo reservó el execution ledger: Alpaca
        rechaza ordenes repetidas con la misma clave, asi que el idempotency
        key persiste mas alla del proceso.
        """
        intents = {"buy_to_open": "buy", "sell_to_close": "sell"}
        if action not in intents:
            raise ValueError(f"invalid action: {action}")
        qty_f = float(qty)
        if abs(qty_f - int(qty_f)) > 1e-9:
            raise ValueError(f"qty must be an integer: {qty}")
        qty_i = int(qty_f)
        if qty_i <= 0:
            raise ValueError(f"invalid qty: {qty}")
        payload: Dict[str, Any] = {
            "symbol": occ_symbol.upper(),
            "qty": str(qty_i),
            "side": intents[action],
            "position_intent": action,
            "type": "market",
            "time_in_force": time_in_force,
            "order_class": "simple",
        }
        if client_order_id:
            payload["client_order_id"] = str(client_order_id)[:48]
        if limit_price is not None:
            try:
                lp = float(limit_price)
            except (TypeError, ValueError):
                raise ValueError(f"invalid limit_price: {limit_price}")
            if lp <= 0:
                raise ValueError(f"limit_price must be positive: {lp}")
            payload["type"] = "limit"
            payload["limit_price"] = str(round(lp, 2))
        url = f"{self.trading_base_url}/orders"
        r = requests.post(url, headers=self.headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            return r.json()
        raise RuntimeError(f"Options order error: {r.status_code} - {r.text}")

    def close_option_position(self, occ_symbol: str, qty: int) -> Dict[str, Any]:
        """Sell-to-close a mercado, clampeado a la posicion realmente tenida.

        Evita cortos descubiertos por qty mayor a la posicion (Alpaca no lo
        bloquea para sell_to_close).
        """
        occ = occ_symbol.upper()
        held = 0
        try:
            for pos in self.get_positions():
                if str(pos.get("symbol", "")).upper() == occ:
                    held = int(float(pos.get("qty", 0) or 0))
                    break
        except Exception as e:
            print(f"[close_position] Could not verify position: {e}")
        if held <= 0:
            raise ValueError(f"No open position to close for {occ}")
        qty_final = min(int(qty), held)
        if qty_final < int(qty):
            print(f"[close_position] qty clamped {qty} -> {qty_final} (held: {held})")
        return self.place_option_order(occ, qty=qty_final, action="sell_to_close",
                                       limit_price=None, time_in_force="day")

    def get_order(self, order_id: str) -> Dict[str, Any]:
        """Estado de una orden por id. Fail-closed: {}."""
        url = f"{self.trading_base_url}/orders/{order_id}"
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
            return r.json() if r.status_code == 200 else {}
        except Exception as e:
            print(f"[get_order] Error: {e}")
            return {}
